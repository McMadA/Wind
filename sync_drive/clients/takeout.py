"""Google Takeout client – reads exported Google Photos / Google Drive folders and restores .json metadata."""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable, Generator
from pathlib import Path

from sync_drive.metadata import (
    apply_metadata_to_file,
    extract_from_takeout_sidecar,
    matches_file_filter,
)

logger = logging.getLogger(__name__)

IGNORED_EXTENSIONS = {".json", ".html", ".htm"}


class TakeoutClient:
    """Scans a local Google Takeout folder, pairs media files with their .json sidecars, and restores EXIF/GPS."""

    def __init__(self, root_dir: str | Path = "."):
        self._root_dir = Path(root_dir).expanduser().resolve()

    def list_files(
        self,
        folder_path: str = "/",
        progress_callback: Callable[[int, str], None] | None = None,
        file_filter: str = "media",
    ) -> Generator[dict, None, None]:
        """Recursively scan *folder_path* (or self._root_dir) for media/document files, skipping .json sidecars."""
        if folder_path and folder_path not in ("/", "root", "."):
            candidate = Path(folder_path).expanduser()
            base_dir = candidate.resolve() if candidate.is_absolute() else (self._root_dir / folder_path.lstrip("/")).resolve()
        else:
            base_dir = self._root_dir

        if not base_dir.exists():
            raise FileNotFoundError(f"Google Takeout folder not found: {base_dir}")

        logger.info("Scanning Google Takeout directory: %s", base_dir)
        count = 0

        for dirpath, dirnames, filenames in os.walk(base_dir):
            # Skip hidden directories
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            current_dir = Path(dirpath)
            rel_dir = "/" + str(current_dir.relative_to(base_dir)).replace("\\", "/").lstrip(".")

            for fname in sorted(filenames):
                if fname.startswith("."):
                    continue
                ext = Path(fname).suffix.lower()
                if ext in IGNORED_EXTENSIONS:
                    continue
                if fname.lower() in ("archive_browser.html", "metadata.json", "print-subscriptions.json", "shared_album_comments.json", "user-generated-memory-titles.json"):
                    continue

                if not matches_file_filter(fname, None, file_filter):
                    continue

                full_path = current_dir / fname
                try:
                    size = full_path.stat().st_size
                except OSError:
                    continue

                if size == 0:
                    continue

                rel_file_path = f"{rel_dir.rstrip('/')}/{fname}"
                photo_meta = extract_from_takeout_sidecar(full_path)

                file_meta = {
                    "id": str(full_path),
                    "name": fname,
                    "path": rel_file_path,
                    "local_source_path": full_path,
                    "size": size,
                    "metadata": photo_meta,
                    "source": "takeout",
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
        """Copy the file to *dest_dir* and inject .json sidecar EXIF (capture date + GPS) and timestamps."""
        src_path = Path(file_meta.get("local_source_path") or file_meta["id"])
        relative = file_meta["path"].lstrip("/")
        local_path = Path(dest_dir) / relative
        local_path.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(src_path, local_path)
        size = file_meta.get("size") or os.path.getsize(local_path)
        if progress_callback:
            progress_callback(size, size)

        # Inject EXIF DateTimeOriginal + GPSInfo from sidecar and set macOS creation/modification dates
        photo_meta = file_meta.get("metadata") or extract_from_takeout_sidecar(src_path)
        merged_meta = apply_metadata_to_file(local_path, photo_meta)
        file_meta["metadata"] = merged_meta

        return local_path

    def delete_file(self, file_id: str) -> None:
        """Delete a file from the local Takeout directory."""
        p = Path(file_id)
        if p.is_file():
            p.unlink()
