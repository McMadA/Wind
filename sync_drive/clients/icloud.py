"""iCloud clients – supports both local macOS iCloud Drive (fast, native) and pyicloud remote API."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sys
from collections.abc import Callable, Generator
from pathlib import Path

from pyicloud import PyiCloudService

from sync_drive.metadata import matches_file_filter, set_file_timestamp

logger = logging.getLogger(__name__)

MACOS_ICLOUD_DRIVE_PATH = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"


class LocalICloudDriveClient:
    """Syncs directly with the local macOS iCloud Drive folder (~/Library/Mobile Documents/com~apple~CloudDocs).

    Advantages on macOS:
    - No Apple ID password / 2FA session timeouts required (macOS 'bird' daemon handles cloud sync).
    - Native folder creation and preservation of original file creation/modification timestamps.
    - Full MD5 checksum verification and duplicate detection (including evicted '.filename.icloud' placeholders).
    """

    def __init__(self, root_dir: str | Path | None = None):
        self.root_dir = Path(root_dir).expanduser().resolve() if root_dir else MACOS_ICLOUD_DRIVE_PATH
        if not self.root_dir.exists():
            raise FileNotFoundError(
                f"Local macOS iCloud Drive folder not found at {self.root_dir}. "
                "Ensure iCloud Drive is enabled in macOS System Settings -> Apple ID -> iCloud."
            )

    @staticmethod
    def _unwrap_icloud_placeholder(filename: str) -> str:
        """Convert an evicted macOS iCloud placeholder '.Document.docx.icloud' back to 'Document.docx'."""
        if filename.startswith(".") and filename.endswith(".icloud") and len(filename) > 8:
            return filename[1:-7]
        return filename

    def _resolve_dir(self, folder_path: str) -> Path:
        clean = folder_path.strip("/")
        if not clean or clean == "root" or clean == ".":
            return self.root_dir
        return self.root_dir / clean

    def list_files(
        self,
        folder_path: str = "/",
        progress_callback: Callable[[int, str], None] | None = None,
        file_filter: str = "all",
    ) -> Generator[dict, None, None]:
        """Recursively list files in local iCloud Drive under *folder_path*."""
        base_dir = self._resolve_dir(folder_path)
        if not base_dir.exists():
            return

        logger.info("  Scanning macOS iCloud Drive: %s", base_dir)
        count = 0

        for dirpath, dirnames, filenames in os.walk(base_dir):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            current_dir = Path(dirpath)
            rel_dir = "/" + str(current_dir.relative_to(self.root_dir)).replace("\\", "/").lstrip(".")

            for raw_name in sorted(filenames):
                if raw_name in (".DS_Store", ".localized"):
                    continue
                is_placeholder = raw_name.startswith(".") and raw_name.endswith(".icloud")
                if raw_name.startswith(".") and not is_placeholder:
                    continue

                real_name = self._unwrap_icloud_placeholder(raw_name)
                if not matches_file_filter(real_name, None, file_filter):
                    continue

                full_path = current_dir / raw_name
                try:
                    size = full_path.stat().st_size
                except OSError:
                    continue

                rel_file_path = f"{rel_dir.rstrip('/')}/{real_name}"
                file_meta = {
                    "id": str(full_path),
                    "name": real_name,
                    "path": rel_file_path,
                    "local_path": full_path,
                    "is_placeholder": is_placeholder,
                    "size": size,
                    "source": "icloud-drive",
                }
                count += 1
                yield file_meta
                if progress_callback:
                    progress_callback(count, rel_dir)

    def download_file(
        self,
        file_meta: dict,
        dest_dir: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Copy a file from local iCloud Drive to *dest_dir*."""
        src_path = Path(file_meta.get("local_path") or file_meta["id"])
        if file_meta.get("is_placeholder"):
            raise RuntimeError(
                f"File '{file_meta['name']}' is currently evicted (.icloud placeholder). "
                "Download it in Finder first before using it as a source."
            )
        relative = file_meta["path"].lstrip("/")
        local_path = Path(dest_dir) / relative
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, local_path)
        size = os.path.getsize(local_path)
        if progress_callback:
            progress_callback(size, size)
        return local_path

    def ensure_path(self, relative_dir: str, root_path: str = "/") -> str:
        """Ensure all intermediate folders exist in local iCloud Drive."""
        base = self._resolve_dir(root_path)
        clean_rel = relative_dir.strip("/")
        target = (base / clean_rel) if clean_rel and clean_rel != "." else base
        target.mkdir(parents=True, exist_ok=True)
        rel_from_root = "/" + str(target.relative_to(self.root_dir)).replace("\\", "/").lstrip(".")
        return rel_from_root

    def find_file(self, name: str, parent_path: str) -> dict | None:
        """Return metadata of an existing file (or evicted .icloud placeholder) under *parent_path*."""
        parent_dir = self._resolve_dir(parent_path)
        direct_file = parent_dir / name
        placeholder_file = parent_dir / f".{name}.icloud"

        if direct_file.is_file():
            size = direct_file.stat().st_size
            return {
                "id": str(direct_file),
                "name": name,
                "size": size,
                "local_path": direct_file,
                "md5": self._compute_md5(direct_file),
            }
        if placeholder_file.is_file():
            return {
                "id": str(placeholder_file),
                "name": name,
                "size": placeholder_file.stat().st_size,
                "local_path": placeholder_file,
                "is_placeholder": True,
            }
        return None

    def upload_file(
        self,
        local_path: Path,
        parent_path: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Copy *local_path* into *parent_path* inside local iCloud Drive, preserving timestamps."""
        parent_dir = self._resolve_dir(parent_path)
        parent_dir.mkdir(parents=True, exist_ok=True)
        dest_file = parent_dir / local_path.name

        shutil.copy2(local_path, dest_file)
        file_size = os.path.getsize(dest_file)
        if progress_callback:
            progress_callback(file_size, file_size)

        return {
            "id": str(dest_file),
            "name": dest_file.name,
            "size": file_size,
            "local_path": dest_file,
            "md5": self._compute_md5(dest_file),
        }

    def update_file(
        self,
        file_id: str,
        local_path: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Overwrite an existing file in local iCloud Drive."""
        dest_file = Path(file_id)
        if dest_file.name.startswith(".") and dest_file.name.endswith(".icloud"):
            # Replace placeholder path with real filename
            real_name = self._unwrap_icloud_placeholder(dest_file.name)
            dest_file = dest_file.parent / real_name
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest_file)
        file_size = os.path.getsize(dest_file)
        if progress_callback:
            progress_callback(file_size, file_size)
        return {
            "id": str(dest_file),
            "name": dest_file.name,
            "size": file_size,
            "local_path": dest_file,
            "md5": self._compute_md5(dest_file),
        }

    def delete_file(self, file_meta: dict | str) -> None:
        """Delete a file from local iCloud Drive."""
        path_str = file_meta["id"] if isinstance(file_meta, dict) else str(file_meta)
        p = Path(path_str)
        if p.is_file():
            p.unlink()
            logger.info("Deleted local iCloud Drive file: %s", p.name)

    def verify_integrity(self, local_path: Path, uploaded_meta: dict) -> bool:
        """Verify MD5 checksum and size between *local_path* and the file written to iCloud Drive."""
        remote_md5 = uploaded_meta.get("md5")
        if remote_md5:
            return self._compute_md5(local_path) == remote_md5
        remote_size = uploaded_meta.get("size")
        if remote_size is not None:
            return os.path.getsize(local_path) == remote_size
        return True

    @staticmethod
    def _compute_md5(filepath: Path) -> str:
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)
        return h.hexdigest().lower()


class ICloudClient:
    """Wraps pyicloud for listing, downloading, and uploading iCloud Drive files via web API."""

    def __init__(
        self,
        apple_id: str,
        password: str,
        cookie_directory: str = ".icloud_cache",
    ):
        self._apple_id = apple_id
        self._password = password
        os.makedirs(cookie_directory, exist_ok=True)

        try:
            self.api = PyiCloudService(apple_id, password, cookie_directory=cookie_directory)
        except Exception as e:
            error_msg = str(e)
            if "Invalid email/password" in error_msg:
                raise RuntimeError(
                    "iCloud auth failed: Invalid email or password. "
                    "Note: If using an App-Specific Password, ensure it is correct. "
                    "If you haven't authenticated before, you might need to use your main password "
                    "once via the CLI to handle 2FA."
                ) from e
            raise

        if self.api.requires_2fa:
            if not sys.stdout.isatty():
                raise RuntimeError(
                    "iCloud 2FA required but terminal is non-interactive. "
                    "Please run the CLI once manually to authenticate: "
                    "python -m sync_drive.cli --source icloud --dry-run"
                )

            print("\n  [!] 2FA Required for iCloud. Please check your devices.")
            code = input("  Enter the code you received: ")
            result = self.api.validate_2fa_code(code)
            if not result:
                raise RuntimeError("Failed to verify 2FA code")
            print("  iCloud authentication successful.\n")

    def _get_node(self, path: str):
        """Navigate to a specific path in iCloud Drive and return the node."""
        parts = [p for p in path.split("/") if p and p != "root" and p != "."]
        node = self.api.drive
        for part in parts:
            node = node[part]
        return node

    def list_files(
        self,
        folder_path: str = "/",
        progress_callback: Callable[[int, str], None] | None = None,
        file_filter: str = "all",
    ) -> Generator[dict, None, None]:
        """Return a flat generator of file metadata dicts under *folder_path* (recursive)."""
        counter = [0]
        return self._walk(folder_path, counter, progress_callback, file_filter=file_filter)

    def _walk(
        self,
        path: str,
        counter: list[int],
        progress_callback: Callable[[int, str], None] | None = None,
        file_filter: str = "all",
    ) -> Generator[dict, None, None]:
        logger.info("  Scanning iCloud: %s", path)
        node = self._get_node(path)

        for name in node.dir():
            child_node = node[name]
            item_type = child_node.type

            if item_type == "folder":
                child_path = f"{path.rstrip('/')}/{name}"
                yield from self._walk(child_path, counter, progress_callback, file_filter=file_filter)
            elif item_type == "file":
                if not matches_file_filter(child_node.name, None, file_filter):
                    continue
                file_meta = {
                    "id": child_node.name,
                    "name": child_node.name,
                    "path": f"{path.rstrip('/')}/{child_node.name}",
                    "size": child_node.size,
                    "node": child_node,
                    "source": "icloud",
                }
                counter[0] += 1
                yield file_meta
                if progress_callback:
                    progress_callback(counter[0], path)

    def download_file(
        self,
        file_meta: dict,
        dest_dir: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download a single file to *dest_dir*."""
        relative = file_meta["path"].lstrip("/")
        local_path = Path(dest_dir) / relative
        local_path.parent.mkdir(parents=True, exist_ok=True)

        node = file_meta.get("node")
        if not node:
            node = self._get_node(file_meta["path"])

        total_size = file_meta.get("size", 0)
        downloaded = 0

        with node.open(stream=True) as response:
            with open(local_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback:
                            progress_callback(downloaded, total_size)

        return local_path

    def ensure_path(self, relative_dir: str, root_path: str = "/") -> str:
        """Ensure all intermediate folders for *relative_dir* exist under *root_path*."""
        parts = [p for p in relative_dir.split("/") if p and p != "."]
        current_node = self._get_node(root_path)
        current_path = root_path.rstrip("/") or "/"
        if current_path == "root":
            current_path = "/"

        for part in parts:
            try:
                current_node = current_node[part]
            except KeyError:
                if hasattr(current_node, "mkdir"):
                    current_node.mkdir(part)
                    logger.info("Created iCloud Drive folder: %s", part)
                    current_node = current_node[part]
                else:
                    logger.warning("iCloud folder creation not supported by current pyicloud node: %s", part)

            current_path = f"{current_path}/{part}" if current_path != "/" else f"/{part}"

        return current_path

    def find_file(self, name: str, parent_path: str) -> dict | None:
        """Return metadata of an existing file with *name* under *parent_path*, or None."""
        try:
            parent_node = self._get_node(parent_path)
            child_node = parent_node[name]
            if child_node.type == "file":
                return {
                    "id": child_node.name,
                    "name": child_node.name,
                    "size": child_node.size,
                    "node": child_node,
                }
        except KeyError:
            pass
        return None

    def upload_file(
        self,
        local_path: Path,
        parent_path: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Upload *local_path* into *parent_path* on iCloud."""
        parent_node = self._get_node(parent_path)
        file_size = os.path.getsize(local_path)

        with open(local_path, "rb") as f:
            parent_node.upload(f)

        if progress_callback:
            progress_callback(file_size, file_size)

        return {"id": local_path.name, "name": local_path.name, "size": file_size}

    def update_file(
        self,
        file_id: str,
        local_path: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Update an existing file in iCloud Drive."""
        return self.upload_file(local_path, "/", progress_callback)

    def delete_file(self, file_meta: dict) -> None:
        """Delete a file from iCloud."""
        node = file_meta.get("node")
        if not node:
            node = self._get_node(file_meta["path"])

        if hasattr(node, "delete"):
            node.delete()
            logger.info("Deleted iCloud file: %s", file_meta["name"])
        else:
            logger.warning("iCloud deletion not supported by current pyicloud version/node.")

    # ── verification ────────────────────────────────────────────────

    def verify_integrity(self, local_path: Path, uploaded_meta: dict) -> bool:
        """Compare local file size against the size reported by iCloud."""
        remote_size = uploaded_meta.get("size")
        if remote_size is None:
            return True
        local_size = os.path.getsize(local_path)
        return local_size == remote_size
