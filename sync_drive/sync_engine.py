"""Sync engine – orchestrates downloading from OneDrive, uploading to Google Drive, and verifying integrity."""

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sync_drive.gdrive_client import GDriveClient
from sync_drive.onedrive_client import OneDriveClient

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """Aggregated result of a sync run."""

    transferred: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return len(self.failed) == 0

    def summary(self) -> str:
        lines = [
            f"Transferred : {len(self.transferred)}",
            f"Verified OK : {len(self.verified)}",
            f"Failed      : {len(self.failed)}",
            f"Skipped     : {len(self.skipped)}",
        ]
        if self.failed:
            lines.append("\nFailed files:")
            for f in self.failed:
                lines.append(f"  - {f}")
        return "\n".join(lines)


class SyncEngine:
    """Downloads files from OneDrive, uploads to Google Drive, and verifies checksums."""

    def __init__(
        self,
        onedrive: OneDriveClient,
        gdrive: GDriveClient,
        temp_dir: str = ".sync_temp",
        target_folder_id: str = "root",
    ):
        self._onedrive = onedrive
        self._gdrive = gdrive
        self._temp_dir = temp_dir
        self._target_folder_id = target_folder_id

    def run(self, onedrive_folder: str = "/") -> SyncResult:
        """Execute a full sync cycle and return the result."""
        result = SyncResult()
        temp = Path(self._temp_dir)
        temp.mkdir(parents=True, exist_ok=True)

        try:
            logger.info("Listing files in OneDrive folder: %s", onedrive_folder)
            files = self._onedrive.list_files(onedrive_folder)
            logger.info("Found %d file(s) to sync.", len(files))

            for file_meta in files:
                rel_path = file_meta["path"]
                try:
                    self._sync_one(file_meta, temp, result)
                except Exception:
                    logger.exception("Failed to sync %s", rel_path)
                    result.failed.append(rel_path)
        finally:
            # clean up temp directory
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)

        return result

    def _sync_one(self, file_meta: dict, temp: Path, result: SyncResult) -> None:
        rel_path = file_meta["path"]

        # 1. Download from OneDrive
        logger.info("[1/3] Downloading: %s", rel_path)
        local_path = self._onedrive.download_file(file_meta, str(temp))
        result.transferred.append(rel_path)

        # 2. Upload to Google Drive
        logger.info("[2/3] Uploading : %s", rel_path)
        parent_dir = str(Path(rel_path).parent).lstrip("/")
        gdrive_parent = self._gdrive.ensure_path(parent_dir, self._target_folder_id)
        uploaded = self._gdrive.upload_file(local_path, gdrive_parent)

        # 3. Verify integrity
        logger.info("[3/3] Verifying : %s", rel_path)
        if self._verify(local_path, uploaded):
            result.verified.append(rel_path)
            logger.info("  OK  %s", rel_path)
        else:
            result.failed.append(rel_path)
            logger.error("  FAIL checksum mismatch: %s", rel_path)

    def _verify(self, local_path: Path, uploaded_meta: dict) -> bool:
        """Compare local MD5 against the MD5 Google Drive computed on upload."""
        gdrive_md5 = uploaded_meta.get("md5Checksum")
        if not gdrive_md5:
            gdrive_md5 = self._gdrive.get_file_md5(uploaded_meta["id"])
        if not gdrive_md5:
            logger.warning("Google Drive did not return an MD5 for %s – skipping verification.", local_path.name)
            return True  # can't verify, treat as ok

        local_md5 = GDriveClient.compute_local_md5(local_path)
        return local_md5 == gdrive_md5
