"""CLI entry point for Wind Cloud Storage & Apple Photos sync."""

from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")

import argparse
import inspect
import logging
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sync_drive.clients import (
    ApplePhotosClient,
    GDriveClient,
    GooglePhotosClient,
    ICloudClient,
    LocalICloudDriveClient,
    OneDriveClient,
    TakeoutClient,
)
from sync_drive. clients.icloud import MACOS_ICLOUD_DRIVE_PATH
from sync_drive.engine import SyncEngine, format_size

LOG_DIR = "logs"

SOURCE_CHOICES = ("onedrive", "gdrive", "icloud", "icloud-drive", "gphotos", "takeout")
DEST_CHOICES = ("onedrive", "gdrive", "icloud", "icloud-drive", "icloud-photos", "gphotos")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sync files & photos between cloud storage services "
            "(Google Drive, Google Photos, Google Takeout, iCloud Drive, iCloud Photos, OneDrive) "
            "with deduplication, EXIF/GPS metadata preservation, and verification."
        )
    )
    parser.add_argument(
        "--source",
        choices=SOURCE_CHOICES,
        default=os.getenv("SOURCE_SERVICE", "gdrive"),
        help="Source service (default: gdrive)",
    )
    parser.add_argument(
        "--dest",
        choices=DEST_CHOICES,
        default=os.getenv("DEST_SERVICE", "icloud-drive"),
        help="Destination service (default: icloud-drive)",
    )
    parser.add_argument(
        "--source-path",
        default=os.getenv("SOURCE_PATH", "/"),
        help="Source folder path, local Takeout path, or Google Drive folder ID (default: / or root)",
    )
    parser.add_argument(
        "--dest-path",
        default=os.getenv("DEST_PATH", "/"),
        help="Destination folder path, Album name (for icloud-photos), or Drive folder ID (default: /)",
    )
    parser.add_argument(
        "--filter",
        dest="file_filter",
        choices=("all", "documents", "media"),
        default=os.getenv("FILE_FILTER", "all"),
        help="Filter files by type: 'all' (default), 'documents' (exclude photos/videos), or 'media' (only photos/videos)",
    )
    parser.add_argument(
        "--dedup-mode",
        choices=("filename+hash", "hash", "filename", "path", "none"),
        default=os.getenv("DEDUP_MODE", "filename+hash"),
        help="Deduplication strategy: filename+hash (default), hash, filename, path, or none",
    )
    parser.add_argument(
        "--album",
        default=os.getenv("PHOTOS_ALBUM", None),
        help="Optional Apple Photos album name when syncing to icloud-photos",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Force rescan of existing Apple Photos / Google Photos library cache",
    )
    parser.add_argument(
        "--temp-dir",
        default=os.getenv("TEMP_DIR", ".sync_temp"),
        help="Local temp directory for downloads",
    )
    parser.add_argument(
        "--on-duplicate",
        choices=("skip", "overwrite", "duplicate"),
        default="skip",
        help="How to handle files that already exist at the destination: skip (default), overwrite, or duplicate",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose (DEBUG) logging",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output and progress bars",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be synced without transferring",
    )
    parser.add_argument(
        "--move",
        action="store_true",
        help="Delete files from source after successful verification at destination",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("SYNC_WORKERS", "12")),
        help="Number of parallel download/sync worker threads (default: 12)",
    )
    parser.add_argument(
        "--log-file",
        default=os.getenv("LOG_FILE", "logging.txt"),
        help="Path to the .txt log file (default: logging.txt in project root + timestamped .txt in logs/)",
    )
    return parser


class _FlushingFileHandler(logging.FileHandler):
    """FileHandler that flushes after every log record so the .txt file is always up-to-date."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def _setup_logging(
    verbose: bool,
    console: Console,
    log_filename: str,
    mirror_txt_filename: str | None = "logging.txt",
) -> None:
    """Configure logging to Rich console + timestamped .txt file in logs/ + logging.txt in project root."""
    log_level = logging.DEBUG if verbose else logging.INFO
    plain_format = "%(asctime)s  %(levelname)-8s  %(message)s"
    formatter = logging.Formatter(plain_format, datefmt="%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()

    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=False,
        rich_tracebacks=True,
    )
    rich_handler.setLevel(log_level)
    root.addHandler(rich_handler)

    # 1. Timestamped .txt archive file in logs/
    file_handler = _FlushingFileHandler(log_filename, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 2. Direct logging.txt file in project root (overwritten per run for easy access)
    if mirror_txt_filename and os.path.abspath(mirror_txt_filename) != os.path.abspath(log_filename):
        mirror_handler = _FlushingFileHandler(mirror_txt_filename, mode="w", encoding="utf-8")
        mirror_handler.setLevel(logging.DEBUG)
        mirror_handler.setFormatter(formatter)
        root.addHandler(mirror_handler)


def _print_summary(
    console: Console,
    result,
    elapsed: float,
    log_filename: str,
    mirror_txt_filename: str | None = "logging.txt",
) -> None:
    """Print a rich summary panel at the end of a sync run and write it to the .txt log."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Transferred", f"[green]{len(result.transferred)}[/green]")
    table.add_row("Verified OK", f"[green]{len(result.verified)}[/green]")
    failed_style = "red bold" if result.failed else "green"
    table.add_row("Failed", f"[{failed_style}]{len(result.failed)}[/{failed_style}]")
    table.add_row("Skipped (Duplicates)", f"[yellow]{len(result.skipped)}[/yellow]")
    if result.total_bytes:
        table.add_row("Total data", format_size(result.total_bytes))
    table.add_row("Elapsed", f"{elapsed:.1f}s")

    panel_style = "green" if result.all_ok else "red"
    title = "Sync Complete" if result.all_ok else "Sync Complete (with errors)"
    panel = Panel(table, title=title, border_style=panel_style, padding=(1, 2))
    console.print()
    console.print(panel)

    # Also log summary to the .txt files
    logging.info("=" * 60)
    logging.info("%s", title)
    logging.info("  Transferred          : %d", len(result.transferred))
    logging.info("  Verified OK          : %d", len(result.verified))
    logging.info("  Skipped (Duplicates) : %d", len(result.skipped))
    logging.info("  Failed               : %d", len(result.failed))
    if result.total_bytes:
        logging.info("  Total data           : %s", format_size(result.total_bytes))
    logging.info("  Elapsed              : %.1fs", elapsed)
    logging.info("=" * 60)

    if result.failed:
        console.print()
        failed_text = Text("Failed files:", style="red bold")
        console.print(failed_text)
        for f in result.failed:
            console.print(f"  - {f}", style="red")
            logging.error("  FAILED FILE: %s", f)

    if mirror_txt_filename:
        console.print(f"\nFull log saved to: [bold]{mirror_txt_filename}[/bold] (and {log_filename})", style="dim")
    else:
        console.print(f"\nFull log saved to: {log_filename}", style="dim")


def _print_dry_run(console: Console, files: list[dict]) -> None:
    """Print a table of files that would be synced and write every row to the .txt log."""
    table = Table(title="Files to sync (dry run)", show_lines=False)
    table.add_column("#", style="dim", justify="right")
    table.add_column("File", style="cyan")
    table.add_column("Size", justify="right")
    table.add_column("Capture Date", style="green")
    table.add_column("GPS", style="magenta")
    table.add_column("Path", style="dim")

    total_bytes = 0
    logging.info("--- DRY RUN FILE LIST (%d files) ---", len(files))
    for i, f in enumerate(files, 1):
        size = f.get("size", 0)
        total_bytes += size
        meta = f.get("metadata")
        date_str = (
            meta.taken_at.strftime("%Y-%m-%d %H:%M")
            if meta and meta.taken_at
            else "-"
        )
        gps_str = (
            f"{meta.latitude:.3f},{meta.longitude:.3f}"
            if meta and meta.has_location
            else "-"
        )
        table.add_row(str(i), f["name"], format_size(size), date_str, gps_str, f["path"])
        logging.info(
            "  [%d] %s | Size: %s | Date: %s | GPS: %s | Path: %s",
            i,
            f["name"],
            format_size(size),
            date_str,
            gps_str,
            f["path"],
        )

    console.print()
    console.print(table)
    console.print(
        f"\n[bold]{len(files)}[/bold] file(s), "
        f"[bold]{format_size(total_bytes)}[/bold] total"
    )
    logging.info("Total dry-run files: %d (%s)", len(files), format_size(total_bytes))


def build_client(
    service_name: str,
    source_or_dest_path: str = "/",
    album_name: str | None = None,
    refresh_cache: bool = False,
):
    """Instantiate the requested cloud or local client."""
    if service_name == "onedrive":
        client_id = os.getenv("ONEDRIVE_CLIENT_ID")
        client_secret = os.getenv("ONEDRIVE_CLIENT_SECRET")
        tenant_id = os.getenv("ONEDRIVE_TENANT_ID", "common")
        redirect_uri = os.getenv("ONEDRIVE_REDIRECT_URI", "http://localhost:8400")
        if not client_id or not client_secret:
            raise ValueError("ONEDRIVE_CLIENT_ID and ONEDRIVE_CLIENT_SECRET must be set.")
        return OneDriveClient(client_id, client_secret, tenant_id, redirect_uri)

    elif service_name == "gdrive":
        credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        return GDriveClient(credentials_file=credentials_file, refresh_cache=refresh_cache)

    elif service_name == "icloud-drive":
        return LocalICloudDriveClient()

    elif service_name == "icloud":
        # On macOS, automatically prefer the fast, native local iCloud Drive folder unless remote web API is forced
        if MACOS_ICLOUD_DRIVE_PATH.exists() and not os.getenv("ICLOUD_FORCE_WEB_API"):
            logging.info(
                "Using native macOS iCloud Drive folder (%s).", MACOS_ICLOUD_DRIVE_PATH
            )
            return LocalICloudDriveClient()
        apple_id = os.getenv("APPLE_ID")
        password = os.getenv("APPLE_PASSWORD")
        if not apple_id or not password:
            raise ValueError(
                "APPLE_ID and APPLE_PASSWORD must be set for remote iCloud API (or enable iCloud Drive on macOS)."
            )
        return ICloudClient(apple_id, password)

    elif service_name == "icloud-photos":
        effective_album = album_name or (
            source_or_dest_path
            if source_or_dest_path and source_or_dest_path not in ("/", "root", ".")
            else None
        )
        return ApplePhotosClient(
            album_name=effective_album,
            refresh_cache=refresh_cache,
        )

    elif service_name == "gphotos":
        credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        return GooglePhotosClient(credentials_file=credentials_file)

    elif service_name == "takeout":
        return TakeoutClient(root_dir=source_or_dest_path)

    else:
        raise ValueError(f"Unknown service: {service_name}")


def main() -> int:
    load_dotenv()
    args = _build_parser().parse_args()

    # Auto-set filter to 'media' when syncing to icloud-photos or from gphotos if left at default 'all'
    file_filter = args.file_filter
    if file_filter == "all" and (args.dest == "icloud-photos" or args.source == "gphotos"):
        file_filter = "media"

    # ── console setup ────────────────────────────────────────────────
    use_color = sys.stdout.isatty() and not args.no_color and not os.getenv("NO_COLOR")
    console = Console(force_terminal=use_color, no_color=not use_color)

    # ── logging setup ────────────────────────────────────────────────
    os.makedirs(LOG_DIR, exist_ok=True)
    log_filename = os.path.join(
        LOG_DIR, f"sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    )
    _setup_logging(args.verbose, console, log_filename, mirror_txt_filename=args.log_file)
    logging.info("Log files: %s and %s", args.log_file, log_filename)

    try:
        source_client = build_client(
            args.source,
            source_or_dest_path=args.source_path,
            refresh_cache=args.refresh_cache,
        )
        dest_client = build_client(
            args.dest,
            source_or_dest_path=args.dest_path,
            album_name=args.album,
            refresh_cache=args.refresh_cache,
        )
    except Exception as e:
        logging.error("Failed to initialize clients: %s", e)
        return 1

    source_folder = args.source_path
    target_folder = args.album or args.dest_path
    panel_text = f"{args.source} -> {args.dest} Sync (filter={file_filter}, dedup={args.dedup_mode})"

    # ── dry-run mode ─────────────────────────────────────────────────
    if args.dry_run:
        logging.info(
            "Dry-run mode (%s -> %s, filter=%s): listing files without transferring.",
            args.source,
            args.dest,
            file_filter,
        )
        sig = inspect.signature(source_client.list_files)
        if "file_filter" in sig.parameters:
            files = list(source_client.list_files(source_folder, file_filter=file_filter))
        else:
            files = list(source_client.list_files(source_folder))

        if hasattr(dest_client, "check_duplicate_pre_download"):
            filtered_files = []
            skipped_count = 0
            for f in files:
                is_dup, _ = dest_client.check_duplicate_pre_download(f, dedup_mode=args.dedup_mode)
                if is_dup:
                    skipped_count += 1
                else:
                    filtered_files.append(f)
            logging.info(
                "Pre-download deduplication would skip %d already-known duplicate(s), leaving %d file(s).",
                skipped_count,
                len(filtered_files),
            )
            files = filtered_files

        _print_dry_run(console, files)
        return 0

    # ── run sync ─────────────────────────────────────────────────────
    console.print(Panel(panel_text, style="bold blue", padding=(0, 2)))
    logging.info("Source      : %s (%s)", args.source, source_folder)
    logging.info("Destination : %s (%s)", args.dest, target_folder)
    logging.info("File filter : %s", file_filter)
    logging.info("Dedup mode  : %s (on-duplicate=%s)", args.dedup_mode, args.on_duplicate)

    engine = SyncEngine(
        source_client=source_client,
        dest_client=dest_client,
        source_name=args.source,
        dest_name=args.dest,
        temp_dir=args.temp_dir,
        target_folder=target_folder,
        on_duplicate=args.on_duplicate,
        file_filter=file_filter,
        dedup_mode=args.dedup_mode,
        workers=args.workers,
        console=console if use_color else None,
        move=args.move,
    )

    start = time.monotonic()
    result = engine.run(source_folder=source_folder)
    elapsed = time.monotonic() - start

    _print_summary(console, result, elapsed, log_filename, mirror_txt_filename=args.log_file)

    return 0 if result.all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
