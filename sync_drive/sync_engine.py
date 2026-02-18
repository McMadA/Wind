"""Sync engine – orchestrates downloading from OneDrive, uploading to Google Drive, and verifying integrity."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from sync_drive.gdrive_client import GDriveClient
from sync_drive.onedrive_client import OneDriveClient

logger = logging.getLogger(__name__)


def format_size(num_bytes: int) -> str:
    """Return a human-readable file size string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes} {unit}"
        num_bytes /= 1024  # type: ignore[assignment]
    return f"{num_bytes:.1f} PB"


@dataclass
class SyncResult:
    """Aggregated result of a sync run."""

    transferred: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    total_bytes: int = 0

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
        if self.total_bytes:
            lines.append(f"Total data  : {format_size(self.total_bytes)}")
        if self.failed:
            lines.append("\nFailed files:")
            for f in self.failed:
                lines.append(f"  - {f}")
        return "\n".join(lines)


class SyncEngine:
    """Downloads files from OneDrive, uploads to Google Drive, and verifies checksums."""

    DUPLICATE_MODES = ("skip", "overwrite", "duplicate")

    def __init__(
        self,
        onedrive: OneDriveClient,
        gdrive: GDriveClient,
        temp_dir: str = ".sync_temp",
        target_folder_id: str = "root",
        on_duplicate: str = "skip",
        console: Console | None = None,
    ):
        if on_duplicate not in self.DUPLICATE_MODES:
            raise ValueError(f"on_duplicate must be one of {self.DUPLICATE_MODES}")
        self._onedrive = onedrive
        self._gdrive = gdrive
        self._temp_dir = temp_dir
        self._target_folder_id = target_folder_id
        self._on_duplicate = on_duplicate
        self._console = console

    # ── public API ───────────────────────────────────────────────────

    def run(self, onedrive_folder: str = "/") -> SyncResult:
        """Execute a full sync cycle and return the result."""
        result = SyncResult()
        temp = Path(self._temp_dir)
        temp.mkdir(parents=True, exist_ok=True)

        try:
            logger.info("Listing files in OneDrive folder: %s", onedrive_folder)
            files = self._scan_with_progress(onedrive_folder)
            logger.info("Found %d file(s) to sync.", len(files))

            if self._console:
                self._run_with_progress(files, temp, result)
            else:
                self._run_plain(files, temp, result)
        finally:
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)

        return result

    # ── scanning with progress ───────────────────────────────────────

    def _scan_with_progress(self, onedrive_folder: str) -> list[dict]:
        """List OneDrive files while showing a live scanning indicator."""
        if not self._console:
            return list(self._onedrive.list_files(onedrive_folder))

        scan_progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            TextColumn("[cyan]{task.fields[folder]}"),
            console=self._console,
        )
        with scan_progress:
            task = scan_progress.add_task(
                "Scanning OneDrive — 0 files found", folder="", total=None
            )

            def on_file_found(count: int, folder: str) -> None:
                short_folder = folder if len(folder) <= 60 else "..." + folder[-57:]
                scan_progress.update(
                    task,
                    description=f"Scanning OneDrive — {count} files found",
                    folder=short_folder,
                )

            files = list(self._onedrive.list_files(
                onedrive_folder, progress_callback=on_file_found
            ))

        return files

    # ── plain mode (no progress bars) ────────────────────────────────

    def _run_plain(self, files: list[dict], temp: Path, result: SyncResult) -> None:
        for file_meta in files:
            rel_path = file_meta["path"]
            try:
                self._sync_one(file_meta, temp, result)
            except Exception:
                logger.exception("Failed to sync %s", rel_path)
                result.failed.append(rel_path)

    # ── progress bar mode ────────────────────────────────────────────

    def _run_with_progress(self, files: list[dict], temp: Path, result: SyncResult) -> None:
        overall_progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            console=self._console,
        )
        file_progress = Progress(
            TextColumn("  {task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=self._console,
        )

        with overall_progress:
            overall_task = overall_progress.add_task("Syncing files", total=len(files))

            for file_meta in files:
                rel_path = file_meta["path"]
                file_size = file_meta.get("size", 0)

                try:
                    self._sync_one(
                        file_meta,
                        temp,
                        result,
                        overall_progress=overall_progress,
                        file_progress=file_progress,
                        file_size=file_size,
                    )
                except Exception as exc:
                    # Use logger.error (not .exception) during progress display
                    # to avoid RichHandler traceback rendering conflicts.
                    # Full tracebacks still go to the log file via the plain FileHandler.
                    logger.error("Failed to sync %s: %s", rel_path, exc)
                    logger.debug("Traceback for %s", rel_path, exc_info=True)
                    result.failed.append(rel_path)

                overall_progress.advance(overall_task)

    # ── single file sync ─────────────────────────────────────────────

    def _sync_one(
        self,
        file_meta: dict,
        temp: Path,
        result: SyncResult,
        overall_progress: Progress | None = None,
        file_progress: Progress | None = None,
        file_size: int = 0,
    ) -> None:
        rel_path = file_meta["path"]
        size_str = format_size(file_size) if file_size else ""

        # 1. Resolve destination folder in Google Drive
        # Use PurePosixPath because OneDrive paths always use forward slashes,
        # but pathlib.Path converts to backslashes on Windows.
        parent_dir = str(PurePosixPath(rel_path).parent).lstrip("/")
        gdrive_parent = self._gdrive.ensure_path(parent_dir, self._target_folder_id)

        # 2. Check if file already exists in Google Drive
        existing = self._gdrive.find_file(file_meta["name"], gdrive_parent)
        if existing:
            if self._on_duplicate == "skip":
                logger.info("SKIP (already exists): %s", rel_path)
                result.skipped.append(rel_path)
                return
            elif self._on_duplicate == "overwrite":
                logger.info("File exists, will overwrite: %s", rel_path)

        # 3. Download from OneDrive
        dl_callback = None
        file_task = None
        if file_progress and file_size:
            file_task = file_progress.add_task(
                f"Downloading {file_meta['name']}", total=file_size
            )
            def dl_callback(downloaded: int, total: int, _task=file_task) -> None:
                file_progress.update(_task, completed=downloaded)
        logger.info("[1/3] Downloading: %s (%s)", rel_path, size_str)

        local_path = self._onedrive.download_file(
            file_meta, str(temp), progress_callback=dl_callback
        )
        result.transferred.append(rel_path)
        result.total_bytes += file_size

        if file_progress and file_task is not None:
            file_progress.remove_task(file_task)

        # 4. Upload (or overwrite) to Google Drive
        ul_callback = None
        if file_progress and file_size:
            file_task = file_progress.add_task(
                f"Uploading {file_meta['name']}", total=file_size
            )
            def ul_callback(uploaded: int, total: int, _task=file_task) -> None:
                file_progress.update(_task, completed=uploaded)

        if existing and self._on_duplicate == "overwrite":
            logger.info("[2/3] Overwriting: %s (%s)", rel_path, size_str)
            uploaded = self._gdrive.update_file(
                existing["id"], local_path, progress_callback=ul_callback
            )
        else:
            logger.info("[2/3] Uploading : %s (%s)", rel_path, size_str)
            uploaded = self._gdrive.upload_file(
                local_path, gdrive_parent, progress_callback=ul_callback
            )

        if file_progress and file_task is not None:
            file_progress.remove_task(file_task)

        # 5. Verify integrity
        logger.info("[3/3] Verifying : %s", rel_path)
        if self._verify(local_path, uploaded):
            result.verified.append(rel_path)
            logger.info("  OK  %s", rel_path)
        else:
            result.failed.append(rel_path)
            logger.error("  FAIL checksum mismatch: %s", rel_path)

    # ── verification ─────────────────────────────────────────────────

    def _verify(self, local_path: Path, uploaded_meta: dict) -> bool:
        """Compare local MD5 against the MD5 Google Drive computed on upload."""
        gdrive_md5 = uploaded_meta.get("md5Checksum")
        if not gdrive_md5:
            gdrive_md5 = self._gdrive.get_file_md5(uploaded_meta["id"])
        if not gdrive_md5:
            logger.warning(
                "Google Drive did not return an MD5 for %s – skipping verification.",
                local_path.name,
            )
            return True

        local_md5 = GDriveClient.compute_local_md5(local_path)
        return local_md5 == gdrive_md5
