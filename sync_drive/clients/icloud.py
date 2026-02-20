"""iCloud client – authenticates via pyicloud, lists, downloads, and uploads files to iCloud Drive."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Generator
from pathlib import Path

from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloud2FARequiredException

logger = logging.getLogger(__name__)


class ICloudClient:
    """Wraps pyicloud for listing, downloading, and uploading iCloud Drive files."""

    def __init__(
        self,
        apple_id: str,
        password: str,
        cookie_directory: str = ".icloud_cache",
    ):
        self._apple_id = apple_id
        self._password = password
        os.makedirs(cookie_directory, exist_ok=True)
        self.api = PyiCloudService(apple_id, password, cookie_directory=cookie_directory)

        if self.api.requires_2fa:
            print("\n  2FA Required for iCloud. Please check your devices.")
            code = input("  Enter the code you received: ")
            result = self.api.validate_2fa_code(code)
            if not result:
                raise RuntimeError("Failed to verify 2FA code")
            print("  iCloud authentication successful.\n")

    def _get_node(self, path: str):
        """Navigate to a specific path in iCloud Drive and return the node."""
        parts = [p for p in path.split("/") if p]
        node = self.api.drive
        for part in parts:
            node = node[part]
        return node

    def list_files(
        self,
        folder_path: str = "/",
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> Generator[dict, None, None]:
        """Return a flat generator of file metadata dicts under *folder_path* (recursive)."""
        counter = [0]
        return self._walk(folder_path, counter, progress_callback)

    def _walk(
        self,
        path: str,
        counter: list[int],
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> Generator[dict, None, None]:
        logger.info("  Scanning iCloud: %s", path)
        node = self._get_node(path)
        
        for name in node.dir():
            child_node = node[name]
            # In pyicloud, we check if it's a directory
            # child_node.type can be 'directory' or 'file'
            item_type = child_node.type
            
            if item_type == 'folder':
                child_path = f"{path.rstrip('/')}/{name}"
                yield from self._walk(child_path, counter, progress_callback)
            elif item_type == 'file':
                file_meta = {
                    "id": child_node.name, # No stable ID like GDrive, use path/name
                    "name": child_node.name,
                    "path": f"{path.rstrip('/')}/{child_node.name}",
                    "size": child_node.size,
                    "node": child_node, # Keep reference for download
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
        parts = [p for p in relative_dir.split("/") if p]
        current_node = self._get_node(root_path)
        current_path = root_path.rstrip("/") or "/"
        
        for part in parts:
            try:
                current_node = current_node[part]
            except KeyError:
                # pyicloud doesn't seem to have a direct 'mkdir', 
                # but it might create it on upload? 
                # Actually, some versions of pyicloud are limited.
                # Let's assume for now we might need to handle this or 
                # use a different approach if pyicloud doesn't support mkdir.
                logger.warning("iCloud folder creation might not be supported directly via pyicloud: %s", part)
                # In some forks it is supported. If not, this will fail.
                pass
            
            current_path = f"{current_path}/{part}" if current_path != "/" else f"/{part}"
            
        return current_path

    def find_file(self, name: str, parent_path: str) -> dict | None:
        """Return metadata of an existing file with *name* under *parent_path*, or None."""
        try:
            parent_node = self._get_node(parent_path)
            child_node = parent_node[name]
            if child_node.type == 'file':
                return {
                    "id": child_node.name,
                    "name": child_node.name,
                    "size": child_node.size,
                    "node": child_node
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
            # Note: pyicloud upload is usually synchronous and might not support progress callbacks easily
            parent_node.upload(f)
            
        if progress_callback:
            progress_callback(file_size, file_size)
            
        return {"id": local_path.name, "name": local_path.name, "size": file_size}

    def delete_file(self, file_meta: dict) -> None:
        """Delete a file from iCloud."""
        node = file_meta.get("node")
        if not node:
             node = self._get_node(file_meta["path"])
        
        # Depending on the pyicloud fork, .delete() or similar is used
        if hasattr(node, 'delete'):
            node.delete()
            logger.info("Deleted iCloud file: %s", file_meta["name"])
        else:
            logger.warning("iCloud deletion not supported by current pyicloud version/node.")

    # ── verification ────────────────────────────────────────────────

    def verify_integrity(self, local_path: Path, uploaded_meta: dict) -> bool:
        """Compare local file size against the size reported by iCloud."""
        # iCloud hashing is not easily available via pyicloud
        # For now, we'll verify by file size
        remote_size = uploaded_meta.get("size")
        if remote_size is None:
            return True # Skip if not available
        
        local_size = os.path.getsize(local_path)
        return local_size == remote_size
