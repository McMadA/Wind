"""Sync engine – orchestrates downloading and uploading between OneDrive and Google Drive with integrity verification."""

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
    """Syncs files between cloud storage services with checksum verification."""

    DUPLICATE_MODES = ("skip", "overwrite", "duplicate")

    def __init__(
        self,
        source_client: any,
        dest_client: any,
        source_name: str,
        dest_name: str,
        temp_dir: str = ".sync_temp",
        target_folder: str = "root",
        on_duplicate: str = "skip",
        console: Console | None = None,
    ):
        if on_duplicate not in self.DUPLICATE_MODES:
            raise ValueError(f"on_duplicate must be one of {self.DUPLICATE_MODES}")
        self._source_client = source_client
        self._dest_client = dest_client
        self._source_name = source_name
        self._dest_name = dest_name
        self._temp_dir = temp_dir
        self._target_folder = target_folder
        self._on_duplicate = on_duplicate
        self._console = console

    # ── generic helpers ──────────────────────────────────────────────

    def _list_source(self, source_folder, progress_callback=None):
        return self._source_client.list_files(source_folder, progress_callback=progress_callback)

    def _download(self, file_meta, dest_dir, progress_callback=None):
        return self._source_client.download_file(file_meta, dest_dir, progress_callback=progress_callback)

    def _ensure_dest_path(self, parent_dir):
        return self._dest_client.ensure_path(parent_dir, self._target_folder)

    def _find_existing(self, name, dest_parent):
        return self._dest_client.find_file(name, dest_parent)

    def _upload(self, local_path, dest_parent, progress_callback=None):
        return self._dest_client.upload_file(local_path, dest_parent, progress_callback=progress_callback)

    def _update(self, file_id, local_path, progress_callback=None):
        return self._dest_client.update_file(file_id, local_path, progress_callback=progress_callback)

    # ── public API ───────────────────────────────────────────────────

    def run(self, source_folder: str = "/") -> SyncResult:
        """Execute a full sync cycle and return the result."""
        result = SyncResult()
        temp = Path(self._temp_dir)
        temp.mkdir(parents=True, exist_ok=True)

        try:
            logger.info("Listing files in %s folder: %s", self._source_name, source_folder)
            files = self._scan_with_progress(source_folder)
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

    def _scan_with_progress(self, source_folder: str) -> list[dict]:
        """List source files while showing a live scanning indicator."""
        source_name = self._source_name

        if not self._console:
            return list(self._list_source(source_folder))

        scan_progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            TextColumn("[cyan]{task.fields[folder]}"),
            console=self._console,
        )
        with scan_progress:
            task = scan_progress.add_task(
                f"Scanning {source_name} \u2014 0 files found", folder="", total=None
            )

            def on_file_found(count: int, folder: str) -> None:
                short_folder = folder if len(folder) <= 60 else "..." + folder[-57:]
                scan_progress.update(
                    task,
                    description=f"Scanning {source_name} \u2014 {count} files found",
                    folder=short_folder,
                )

            files = list(self._list_source(
                source_folder, progress_callback=on_file_found
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

        # 1. Resolve destination folder
        # Use PurePosixPath because source paths always use forward slashes,
        # but pathlib.Path converts to backslashes on Windows.
        parent_dir = str(PurePosixPath(rel_path).parent).lstrip("/")
        dest_parent = self._ensure_dest_path(parent_dir)

        # 2. Check if file already exists at destination
        existing = self._find_existing(file_meta["name"], dest_parent)
        if existing:
            if self._on_duplicate == "skip":
                logger.info("SKIP (already exists): %s", rel_path)
                result.skipped.append(rel_path)
                return
            elif self._on_duplicate == "overwrite":
                logger.info("File exists, will overwrite: %s", rel_path)

        # 3. Download from source
        dl_callback = None
        file_task = None
        if file_progress and file_size:
            file_task = file_progress.add_task(
                f"Downloading {file_meta['name']}", total=file_size
            )
            def dl_callback(downloaded: int, total: int, _task=file_task) -> None:
                file_progress.update(_task, completed=downloaded)
        logger.info("[1/3] Downloading: %s (%s)", rel_path, size_str)

        local_path = self._download(
            file_meta, str(temp), progress_callback=dl_callback
        )
        result.transferred.append(rel_path)
        result.total_bytes += file_size

        if file_progress and file_task is not None:
            file_progress.remove_task(file_task)

        # 4. Upload (or overwrite) to destination
        ul_callback = None
        if file_progress and file_size:
            file_task = file_progress.add_task(
                f"Uploading {file_meta['name']}", total=file_size
            )
            def ul_callback(uploaded: int, total: int, _task=file_task) -> None:
                file_progress.update(_task, completed=uploaded)

        if existing and self._on_duplicate == "overwrite":
            logger.info("[2/3] Overwriting: %s (%s)", rel_path, size_str)
            uploaded = self._update(
                existing["id"], local_path, progress_callback=ul_callback
            )
        else:
            logger.info("[2/3] Uploading : %s (%s)", rel_path, size_str)
            uploaded = self._upload(
                local_path, dest_parent, progress_callback=ul_callback
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
        """Compare local hash against the hash reported by the destination service."""
        return self._dest_client.verify_integrity(local_path, uploaded_meta)
