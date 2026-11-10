"""Sync engine – orchestrates multi-threaded downloading and uploading between cloud storage services with deduplication, metadata preservation, and integrity verification."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import hashlib
import inspect
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import threading

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)

logger = logging.getLogger(__name__)

DEFAULT_WORKERS = 12


def format_size(num_bytes: int | float) -> str:
    """Return a human-readable file size string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{int(num_bytes)} {unit}"
        num_bytes /= 1024
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
    """Multi-threaded sync engine with cross-source deduplication and checksum verification."""

    DUPLICATE_MODES = ("skip", "overwrite", "duplicate")
    FILE_FILTERS = ("all", "documents", "media")
    DEDUP_MODES = ("filename+hash", "hash", "filename", "path", "none")

    def __init__(
        self,
        source_client: any,
        dest_client: any,
        source_name: str,
        dest_name: str,
        temp_dir: str = ".sync_temp",
        target_folder: str = "root",
        on_duplicate: str = "skip",
        file_filter: str = "all",
        dedup_mode: str = "filename+hash",
        workers: int = DEFAULT_WORKERS,
        console: Console | None = None,
        move: bool = False,
    ):
        if on_duplicate not in self.DUPLICATE_MODES:
            raise ValueError(f"on_duplicate must be one of {self.DUPLICATE_MODES}")
        if file_filter not in self.FILE_FILTERS:
            raise ValueError(f"file_filter must be one of {self.FILE_FILTERS}")
        if dedup_mode not in self.DEDUP_MODES:
            raise ValueError(f"dedup_mode must be one of {self.DEDUP_MODES}")

        self._source_client = source_client
        self._dest_client = dest_client
        self._source_name = source_name
        self._dest_name = dest_name
        self._temp_dir = temp_dir
        self._target_folder = target_folder
        self._on_duplicate = on_duplicate
        self._file_filter = file_filter
        self._dedup_mode = dedup_mode
        self._workers = max(1, int(workers))
        self._console = console
        self._move = move

        self._state_lock = threading.Lock()
        self._seen_md5: set[str] = set()
        self._seen_filename_sizes: set[str] = set()
        self._dest_indexed = False

    # ── generic helpers ──────────────────────────────────────────────

    def _list_source(self, source_folder, progress_callback=None):
        sig = inspect.signature(self._source_client.list_files)
        if "file_filter" in sig.parameters:
            return self._source_client.list_files(
                source_folder,
                progress_callback=progress_callback,
                file_filter=self._file_filter,
            )
        return self._source_client.list_files(
            source_folder, progress_callback=progress_callback
        )

    def _download(self, file_meta, dest_dir, progress_callback=None):
        return self._source_client.download_file(
            file_meta, dest_dir, progress_callback=progress_callback
        )

    def _ensure_dest_path(self, parent_dir):
        return self._dest_client.ensure_path(parent_dir, self._target_folder)

    def _find_existing(self, name, dest_parent):
        return self._dest_client.find_file(name, dest_parent)

    def _upload(self, local_path, dest_parent, progress_callback=None, file_meta=None):
        sig = inspect.signature(self._dest_client.upload_file)
        if "file_meta" in sig.parameters:
            return self._dest_client.upload_file(
                local_path,
                dest_parent,
                progress_callback=progress_callback,
                file_meta=file_meta,
            )
        return self._dest_client.upload_file(
            local_path, dest_parent, progress_callback=progress_callback
        )

    def _update(self, file_id, local_path, progress_callback=None):
        return self._dest_client.update_file(
            file_id, local_path, progress_callback=progress_callback
        )

    def _delete_source(self, file_meta):
        """Delete the file from source storage."""
        try:
            if hasattr(self._source_client, "delete_file"):
                if self._source_name.lower() == "icloud":
                    self._source_client.delete_file(file_meta)
                else:
                    self._source_client.delete_file(file_meta["id"])
            else:
                logger.warning("Source client for %s does not support deletion.", self._source_name)
        except Exception as exc:
            logger.error("Failed to delete source file %s: %s", file_meta["name"], exc)

    def _index_dest_if_needed(self) -> None:
        """Pre-index existing files in local iCloud Drive when global deduplication is enabled."""
        if self._dest_indexed:
            return
        self._dest_indexed = True

        if self._on_duplicate != "skip" or self._dedup_mode in ("path", "none"):
            return

        # ApplePhotosClient manages its own index
        if hasattr(self._dest_client, "check_duplicate_pre_download"):
            return

        # If destination is LocalICloudDriveClient, scan existing destination files for global dedup
        if hasattr(self._dest_client, "root_dir") and hasattr(self._dest_client, "list_files"):
            try:
                logger.info("Indexing existing destination files in %s for duplicate prevention...", self._dest_name)
                count = 0
                for item in self._dest_client.list_files(
                    self._target_folder, file_filter=self._file_filter
                ):
                    name_lower = (item.get("name") or "").strip().lower()
                    size = int(item.get("size") or 0)
                    if name_lower and size > 0:
                        self._seen_filename_sizes.add(f"{name_lower}:{size}")
                    local_p = item.get("local_path")
                    if (
                        local_p
                        and not item.get("is_placeholder")
                        and self._dedup_mode in ("hash", "filename+hash")
                        and size < 200 * 1024 * 1024
                    ):
                        try:
                            h = hashlib.md5()
                            with open(local_p, "rb") as fh:
                                for blk in iter(lambda: fh.read(65536), b""):
                                    h.update(blk)
                            self._seen_md5.add(h.hexdigest().lower())
                        except OSError:
                            pass
                    count += 1
                logger.info(
                    "Indexed %d existing file(s) at destination (%d MD5 hashes).",
                    count,
                    len(self._seen_md5),
                )
            except Exception as exc:
                logger.debug("Destination pre-index skipped: %s", exc)

    # ── public API ───────────────────────────────────────────────────

    def run(self, source_folder: str = "/") -> SyncResult:
        """Execute a full sync cycle across worker threads and return the result."""
        result = SyncResult()
        temp = Path(self._temp_dir)
        temp.mkdir(parents=True, exist_ok=True)

        try:
            self._index_dest_if_needed()
            logger.info(
                "Listing files in %s folder: %s (filter=%s, workers=%d)",
                self._source_name,
                source_folder,
                self._file_filter,
                self._workers,
            )
            files = self._scan_with_progress(source_folder)
            logger.info("Found %d file(s) matching filter '%s'.", len(files), self._file_filter)

            if self._console:
                self._run_with_progress(files, temp, result)
            else:
                self._run_plain(files, temp, result)
        finally:
            if hasattr(self._dest_client, "flush"):
                try:
                    self._dest_client.flush()
                except Exception:
                    pass
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

    # ── plain mode (no progress bars, multi-threaded) ────────────────

    def _run_plain(self, files: list[dict], temp: Path, result: SyncResult) -> None:
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = {
                pool.submit(self._sync_one, file_meta, temp / f"w_{idx}", result): file_meta["path"]
                for idx, file_meta in enumerate(files)
            }
            for fut in as_completed(futures):
                rel_path = futures[fut]
                try:
                    fut.result()
                except Exception:
                    logger.exception("Failed to sync %s", rel_path)
                    with self._state_lock:
                        result.failed.append(rel_path)

    # ── progress bar mode (multi-threaded) ───────────────────────────

    def _run_with_progress(self, files: list[dict], temp: Path, result: SyncResult) -> None:
        overall_progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            console=self._console,
        )

        with overall_progress:
            overall_task = overall_progress.add_task(
                f"Syncing files ({self._workers} workers)", total=len(files)
            )

            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                futures = {
                    pool.submit(
                        self._sync_one,
                        file_meta,
                        temp / f"w_{idx}",
                        result,
                    ): file_meta["path"]
                    for idx, file_meta in enumerate(files)
                }
                try:
                    for fut in as_completed(futures):
                        rel_path = futures[fut]
                        try:
                            fut.result()
                        except Exception as exc:
                            logger.error("Failed to sync %s: %s", rel_path, exc)
                            logger.debug("Traceback for %s", rel_path, exc_info=True)
                            with self._state_lock:
                                result.failed.append(rel_path)
                        overall_progress.advance(overall_task)
                except KeyboardInterrupt:
                    logger.warning("Interrupted by user (Ctrl+C); shutting down workers...")
                    for f in futures:
                        f.cancel()
                    raise

    # ── single file sync (worker thread) ─────────────────────────────

    def _sync_one(
        self,
        file_meta: dict,
        worker_temp: Path,
        result: SyncResult,
    ) -> None:
        rel_path = file_meta["path"]
        file_size = int(file_meta.get("size") or 0)
        size_str = format_size(file_size) if file_size else ""
        name_lower = (file_meta.get("name") or "").strip().lower()
        source_md5 = (file_meta.get("md5") or "").lower()

        # 0. Pre-download duplicate check (thread-safe, reserves MD5 so parallel workers never download same duplicate)
        reserved_md5 = False
        if self._on_duplicate == "skip":
            if hasattr(self._dest_client, "check_duplicate_pre_download"):
                is_dup, reason = self._dest_client.check_duplicate_pre_download(
                    file_meta, dedup_mode=self._dedup_mode
                )
                if is_dup:
                    logger.info("SKIP (%s): %s", reason, rel_path)
                    with self._state_lock:
                        result.skipped.append(rel_path)
                    return

            with self._state_lock:
                if source_md5 and self._dedup_mode in ("hash", "filename+hash"):
                    if source_md5 in self._seen_md5:
                        logger.info("SKIP (duplicate MD5 %s): %s", source_md5[:10], rel_path)
                        result.skipped.append(rel_path)
                        return
                    self._seen_md5.add(source_md5)
                    reserved_md5 = True

                if (
                    self._dedup_mode in ("filename", "filename+hash")
                    and name_lower
                    and file_size > 0
                    and f"{name_lower}:{file_size}" in self._seen_filename_sizes
                ):
                    if reserved_md5:
                        self._seen_md5.discard(source_md5)
                    logger.info("SKIP (filename+size already exists in destination): %s", rel_path)
                    result.skipped.append(rel_path)
                    return

        try:
            # 1. Resolve destination folder
            parent_dir = str(PurePosixPath(rel_path).parent).lstrip("/")
            dest_parent = self._ensure_dest_path(parent_dir)

            # 2. Check if file already exists at destination path
            existing = self._find_existing(file_meta["name"], dest_parent)
            if existing:
                if self._on_duplicate == "skip":
                    logger.info("SKIP (already exists at path): %s", rel_path)
                    with self._state_lock:
                        result.skipped.append(rel_path)
                        if name_lower and file_size > 0:
                            self._seen_filename_sizes.add(f"{name_lower}:{file_size}")
                    return
                elif self._on_duplicate == "overwrite":
                    logger.info("File exists, will overwrite: %s", rel_path)

            # 3. Download from source into isolated worker_temp directory
            logger.info("[1/3] Downloading: %s (%s)", rel_path, size_str or "size unknown")
            local_path = self._download(file_meta, str(worker_temp))
            actual_size = os.path.getsize(local_path) if local_path.exists() else file_size

            try:
                # 3b. Post-download hash check if MD5 wasn't known pre-download
                if (
                    self._on_duplicate == "skip"
                    and not hasattr(self._dest_client, "check_duplicate_pre_download")
                    and self._dedup_mode in ("hash", "filename+hash")
                    and not source_md5
                    and local_path.exists()
                ):
                    h = hashlib.md5()
                    with open(local_path, "rb") as fh:
                        for blk in iter(lambda: fh.read(65536), b""):
                            h.update(blk)
                    computed_md5 = h.hexdigest().lower()
                    with self._state_lock:
                        if computed_md5 in self._seen_md5:
                            logger.info("SKIP (post-download MD5 match %s): %s", computed_md5[:10], rel_path)
                            result.skipped.append(rel_path)
                            return
                        self._seen_md5.add(computed_md5)
                        source_md5 = computed_md5
                        reserved_md5 = True

                # 4. Upload (or overwrite) to destination
                if existing and self._on_duplicate == "overwrite":
                    logger.info("[2/3] Overwriting: %s (%s)", rel_path, format_size(actual_size))
                    uploaded = self._update(existing["id"], local_path)
                else:
                    logger.info("[2/3] Uploading : %s (%s)", rel_path, format_size(actual_size))
                    uploaded = self._upload(local_path, dest_parent, file_meta=file_meta)

                # Check if destination detected a post-download duplicate (e.g. ApplePhotosClient hash/pixel check)
                if isinstance(uploaded, dict) and uploaded.get("skipped_duplicate"):
                    with self._state_lock:
                        result.skipped.append(rel_path)
                    return

                # 5. Verify integrity
                logger.info("[3/3] Verifying : %s", rel_path)
                if self._verify(local_path, uploaded):
                    with self._state_lock:
                        result.transferred.append(rel_path)
                        result.verified.append(rel_path)
                        result.total_bytes += actual_size
                        if name_lower and actual_size > 0:
                            self._seen_filename_sizes.add(f"{name_lower}:{actual_size}")
                    logger.info("  OK  %s", rel_path)

                    # 6. Delete from source if move is enabled
                    if self._move:
                        logger.info("[4/4] Moving (Delete source): %s", rel_path)
                        self._delete_source(file_meta)
                else:
                    if reserved_md5 and source_md5:
                        with self._state_lock:
                            self._seen_md5.discard(source_md5)
                    with self._state_lock:
                        result.failed.append(rel_path)
                    logger.error("  FAIL checksum mismatch: %s", rel_path)
            finally:
                if worker_temp.exists():
                    shutil.rmtree(worker_temp, ignore_errors=True)
        except Exception:
            if reserved_md5 and source_md5:
                with self._state_lock:
                    self._seen_md5.discard(source_md5)
            raise

    # ── verification ─────────────────────────────────────────────────

    def _verify(self, local_path: Path, uploaded_meta: dict) -> bool:
        """Compare local hash against the hash reported by the destination service."""
        return self._dest_client.verify_integrity(local_path, uploaded_meta)
