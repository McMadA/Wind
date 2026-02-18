"""CLI entry point for OneDrive <-> Google Drive sync."""

from __future__ import annotations

import argparse
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

from sync_drive.gdrive_client import GDriveClient
from sync_drive.onedrive_client import OneDriveClient
from sync_drive.sync_engine import SyncDirection, SyncEngine, format_size

LOG_DIR = "logs"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync files between OneDrive and Google Drive with verification."
    )
    parser.add_argument(
        "--direction",
        choices=("onedrive-to-gdrive", "gdrive-to-onedrive"),
        default=os.getenv("SYNC_DIRECTION", "onedrive-to-gdrive"),
        help="Sync direction (default: onedrive-to-gdrive)",
    )
    parser.add_argument(
        "--onedrive-folder",
        default=os.getenv("ONEDRIVE_SYNC_FOLDER", "/"),
        help="OneDrive folder path "
             "(source for onedrive-to-gdrive, destination for gdrive-to-onedrive; default: /)",
    )
    parser.add_argument(
        "--gdrive-folder-id",
        default=os.getenv("GOOGLE_DRIVE_TARGET_FOLDER", "root"),
        help="Google Drive folder ID "
             "(destination for onedrive-to-gdrive, source for gdrive-to-onedrive; default: root)",
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
        help="How to handle files that already exist at the destination: "
             "skip (default), overwrite, or duplicate",
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
    return parser


def _setup_logging(
    verbose: bool,
    console: Console,
    log_filename: str,
) -> None:
    """Configure dual logging: rich console + plain-text log file."""
    log_level = logging.DEBUG if verbose else logging.INFO
    plain_format = "%(asctime)s  %(levelname)-8s  %(message)s"

    root = logging.getLogger()
    root.setLevel(log_level)

    # Rich console handler (colored, structured)
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=False,
        rich_tracebacks=True,
    )
    rich_handler.setLevel(log_level)
    root.addHandler(rich_handler)

    # Plain-text file handler (no ANSI in log files)
    file_handler = logging.FileHandler(log_filename, encoding="utf-8")
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(plain_format, datefmt="%H:%M:%S"))
    root.addHandler(file_handler)


def _print_summary(console: Console, result, elapsed: float, log_filename: str) -> None:
    """Print a rich summary panel at the end of a sync run."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Transferred", f"[green]{len(result.transferred)}[/green]")
    table.add_row("Verified OK", f"[green]{len(result.verified)}[/green]")
    failed_style = "red bold" if result.failed else "green"
    table.add_row("Failed", f"[{failed_style}]{len(result.failed)}[/{failed_style}]")
    table.add_row("Skipped", str(len(result.skipped)))
    if result.total_bytes:
        table.add_row("Total data", format_size(result.total_bytes))
    table.add_row("Elapsed", f"{elapsed:.1f}s")

    panel_style = "green" if result.all_ok else "red"
    title = "Sync Complete" if result.all_ok else "Sync Complete (with errors)"
    panel = Panel(table, title=title, border_style=panel_style, padding=(1, 2))
    console.print()
    console.print(panel)

    if result.failed:
        console.print()
        failed_text = Text("Failed files:", style="red bold")
        console.print(failed_text)
        for f in result.failed:
            console.print(f"  - {f}", style="red")

    console.print(f"\nFull log saved to: {log_filename}", style="dim")


def _print_dry_run(console: Console, files: list[dict]) -> None:
    """Print a table of files that would be synced."""
    table = Table(title="Files to sync (dry run)", show_lines=False)
    table.add_column("#", style="dim", justify="right")
    table.add_column("File", style="cyan")
    table.add_column("Size", justify="right")
    table.add_column("Path", style="dim")

    total_bytes = 0
    for i, f in enumerate(files, 1):
        size = f.get("size", 0)
        total_bytes += size
        table.add_row(str(i), f["name"], format_size(size), f["path"])

    console.print()
    console.print(table)
    console.print(
        f"\n[bold]{len(files)}[/bold] file(s), "
        f"[bold]{format_size(total_bytes)}[/bold] total"
    )


def main() -> int:
    load_dotenv()
    args = _build_parser().parse_args()
    direction = SyncDirection(args.direction)

    # ── console setup ────────────────────────────────────────────────
    use_color = sys.stdout.isatty() and not args.no_color and not os.getenv("NO_COLOR")
    console = Console(force_terminal=use_color, no_color=not use_color)

    # ── logging setup ────────────────────────────────────────────────
    os.makedirs(LOG_DIR, exist_ok=True)
    log_filename = os.path.join(
        LOG_DIR, f"sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    _setup_logging(args.verbose, console, log_filename)
    logging.info("Log file: %s", log_filename)

    # ── build clients ────────────────────────────────────────────────
    client_id = os.getenv("ONEDRIVE_CLIENT_ID")
    client_secret = os.getenv("ONEDRIVE_CLIENT_SECRET")
    tenant_id = os.getenv("ONEDRIVE_TENANT_ID", "common")
    redirect_uri = os.getenv("ONEDRIVE_REDIRECT_URI", "http://localhost:8400")
    credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")

    if not client_id or not client_secret:
        logging.error(
            "ONEDRIVE_CLIENT_ID and ONEDRIVE_CLIENT_SECRET must be set. "
            "Copy .env.example to .env and fill in your credentials."
        )
        return 1

    onedrive = OneDriveClient(
        client_id=client_id,
        client_secret=client_secret,
        tenant_id=tenant_id,
        redirect_uri=redirect_uri,
    )
    gdrive = GDriveClient(credentials_file=credentials_file)

    # ── determine source / destination based on direction ────────────
    if direction == SyncDirection.ONEDRIVE_TO_GDRIVE:
        source_folder = args.onedrive_folder
        target_folder = args.gdrive_folder_id
        panel_text = "OneDrive -> Google Drive Sync"
    else:
        source_folder = args.gdrive_folder_id
        target_folder = args.onedrive_folder
        panel_text = "Google Drive -> OneDrive Sync"

    # ── dry-run mode ─────────────────────────────────────────────────
    if args.dry_run:
        logging.info("Dry-run mode (%s): listing files without transferring.", args.direction)
        if direction == SyncDirection.ONEDRIVE_TO_GDRIVE:
            files = list(onedrive.list_files(source_folder))
        else:
            files = list(gdrive.list_files(source_folder))
        _print_dry_run(console, files)
        return 0

    # ── run sync ─────────────────────────────────────────────────────
    console.print(Panel(panel_text, style="bold blue", padding=(0, 2)))
    logging.info("Direction: %s", args.direction)
    logging.info("Duplicate mode: %s", args.on_duplicate)

    engine = SyncEngine(
        onedrive=onedrive,
        gdrive=gdrive,
        temp_dir=args.temp_dir,
        target_folder=target_folder,
        on_duplicate=args.on_duplicate,
        console=console if use_color else None,
        direction=direction,
    )

    start = time.monotonic()
    result = engine.run(source_folder=source_folder)
    elapsed = time.monotonic() - start

    _print_summary(console, result, elapsed, log_filename)

    return 0 if result.all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
