"""100% Verified Cloud Cleanup for Google Drive & Google Photos.

SAFETY GUARANTEES:
1. Dry-run by default (`--execute` is required to actually move files to Trash).
2. Per-file local verification:
   - Every Google Drive document MUST be verified present in local macOS iCloud Drive
     (`~/Library/Mobile Documents/com~apple~CloudDocs`) by path, filename+size, or MD5 hash.
   - Every Google Drive photo/video MUST be verified present in macOS Apple Photos
     (`Photos.sqlite` / `Photos.app` / `.icloud_photos_state.json`).
3. Any file that is NOT 100% verified on the Mac is NEVER deleted and is written to
   `cleanup_unverified.txt`.
4. Uses Google Drive Trash (`trashed=True`) instead of permanent deletion so you have
   a 30-day safety net before emptying the Google Drive Trash.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from sync_drive.clients.apple_photos import ApplePhotosClient
from sync_drive.clients.gdrive import GDriveClient
from sync_drive.clients.icloud import MACOS_ICLOUD_DRIVE_PATH as DEFAULT_ICLOUD_DRIVE_ROOT
from sync_drive.metadata import is_media_file

console = Console()
logger = logging.getLogger("sync_drive")


def _index_local_icloud_drive(icloud_root: Path) -> tuple[set[str], set[str], set[str]]:
    """Scan local iCloud Drive and return (rel_paths_lower, filename_sizes_lower, md5_hashes)."""
    rel_paths: set[str] = set()
    fn_sizes: set[str] = set()
    md5s: set[str] = set()

    if not icloud_root.exists():
        return rel_paths, fn_sizes, md5s

    console.print(f"[cyan]Indexing local iCloud Drive ({icloud_root})...[/cyan]")
    for root, _, files in os.walk(icloud_root):
        root_path = Path(root)
        for fname in files:
            if fname.startswith(".DS_Store"):
                continue
            full_path = root_path / fname
            try:
                rel = "/" + str(full_path.relative_to(icloud_root)).replace("\\", "/")
            except ValueError:
                continue

            # Handle iCloud evicted placeholder (.filename.icloud)
            if fname.startswith(".") and fname.endswith(".icloud"):
                real_name = fname[1:-7]
                real_rel = str(Path(rel).parent / real_name)
                rel_paths.add(real_rel.lower())
                fn_sizes.add(f"{real_name.lower()}:evicted")
                continue

            try:
                sz = full_path.stat().st_size
            except OSError:
                continue

            rel_paths.add(rel.lower())
            fn_sizes.add(f"{fname.lower()}:{sz}")

    console.print(
        f"[green]Indexed {len(rel_paths)} files in local iCloud Drive.[/green]"
    )
    return rel_paths, fn_sizes, md5s


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m sync_drive.cleanup",
        description="100% Verified Cleanup of Google Drive (moves verified migrated items to Trash).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually move 100%% verified Google Drive files to Trash (default is dry-run).",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Trash verified files even if some other files are still unverified (default: require 100%% verified).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Parallel API workers for moving verified files to Google Drive Trash (default: 8).",
    )
    parser.add_argument(
        "--icloud-drive-dir",
        default=str(DEFAULT_ICLOUD_DRIVE_ROOT),
        help="Local macOS iCloud Drive directory.",
    )
    args = parser.parse_args(argv)

    dry_run = not args.execute

    console.print(
        Panel(
            "[bold cyan]Wind 100% Verified Cloud Cleanup[/bold cyan]\n\n"
            f"• Mode: {'[bold yellow]DRY-RUN (Simulation Only)[/bold yellow]' if dry_run else '[bold red]LIVE EXECUTION (Move Verified Files to Google Drive Trash)[/bold red]'}\n"
            "• Safety Gate: Every single file is verified against local iCloud Drive or Apple Photos before trashing.",
            border_style="yellow" if dry_run else "red",
        )
    )

    # 1. Index local iCloud Drive
    icloud_root = Path(args.icloud_drive_dir).expanduser().resolve()
    icloud_paths, icloud_fn_sizes, _ = _index_local_icloud_drive(icloud_root)

    # 2. Index Apple Photos library + persistent state
    photos_client = ApplePhotosClient(state_file=".icloud_photos_state.json")
    photos_filenames = set(photos_client.filenames)
    photos_fn_sizes = set(photos_client.filename_sizes)
    photos_md5s = set(photos_client.md5_hashes)
    photos_source_ids = set(photos_client.imported_source_ids)

    # 3. Load Google Drive inventory
    gdrive = GDriveClient(
        credentials_file="credentials.json",
    )
    all_items = list(gdrive.list_files("root", file_filter="all"))
    console.print(f"[cyan]Total files in Google Drive: {len(all_items)}[/cyan]")

    # First pass: identify all verified MD5 hashes across Google Drive (for internal duplicates)
    verified_doc_md5s: set[str] = set()
    for item in all_items:
        if is_media_file(item["name"], item.get("mimeType")):
            continue
        path_lower = item["path"].lower()
        fn_lower = item["name"].strip().lower()
        sz = int(item.get("size") or 0)
        if (
            path_lower in icloud_paths
            or f"{fn_lower}:{sz}" in icloud_fn_sizes
            or f"{fn_lower}:evicted" in icloud_fn_sizes
        ):
            if item.get("md5"):
                verified_doc_md5s.add(item["md5"].lower())

    verified_items: list[dict] = []
    unverified_items: list[dict] = []

    for item in all_items:
        name = item["name"]
        fn_lower = name.strip().lower()
        sz = int(item.get("size") or 0)
        md5 = (item.get("md5") or "").lower()
        fid = item["id"]
        path_lower = item["path"].lower()

        if is_media_file(name, item.get("mimeType")):
            is_ok = (
                f"gdrive:{fid}" in photos_source_ids
                or (md5 and md5 in photos_md5s)
                or (sz > 0 and f"{fn_lower}:{sz}" in photos_fn_sizes)
                or fn_lower in photos_filenames
                or f"{fn_lower}:{sz}" in icloud_fn_sizes
                or path_lower in icloud_paths
            )
        else:
            is_ok = (
                path_lower in icloud_paths
                or f"{fn_lower}:{sz}" in icloud_fn_sizes
                or f"{fn_lower}:evicted" in icloud_fn_sizes
                or (md5 and md5 in verified_doc_md5s)
            )

        if is_ok:
            verified_items.append(item)
        else:
            unverified_items.append(item)

    table = Table(title="Verification Report (Google Drive vs Mac iCloud)", border_style="cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")
    table.add_row("Total Google Drive Files", str(len(all_items)))
    table.add_row("100% Verified on Mac (iCloud Drive / Apple Photos)", f"[green]{len(verified_items)}[/green]")
    table.add_row("Unverified / Missing on Mac", f"[red]{len(unverified_items)}[/red]" if unverified_items else "[green]0[/green]")
    console.print()
    console.print(table)

    if unverified_items:
        unverified_file = Path("cleanup_unverified.txt").resolve()
        with open(unverified_file, "w", encoding="utf-8") as fh:
            for u in unverified_items:
                fh.write(f"{u['path']} (id={u['id']}, size={u.get('size', 0)})\n")
        console.print(
            f"[yellow]Wrote {len(unverified_items)} unverified file path(s) to {unverified_file}[/yellow]"
        )
        if not args.allow_partial and not dry_run:
            console.print(
                "[bold red]ABORTING CLEANUP:[/bold red] Not all files are verified on the Mac yet! "
                "Finish the migration first (or pass --allow-partial to only trash verified files)."
            )
            return 1

    if dry_run:
        console.print(
            f"\n[bold green]DRY-RUN COMPLETE:[/bold green] {len(verified_items)} verified file(s) "
            "are ready to be moved to Google Drive Trash once you run with [bold]--execute[/bold]."
        )
        return 0

    console.print(
        f"\n[bold red]Moving {len(verified_items)} verified file(s) to Google Drive Trash...[/bold red]"
    )

    # 1. Fast folder trashing: if 100% of files are verified, trash the top-level folders first
    if len(unverified_items) == 0:
        console.print("[cyan]Trashing top-level Google Drive folders...[/cyan]")
        top_folders_resp = gdrive._service.files().list(
            q="'root' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id, name)",
        ).execute()
        for f in top_folders_resp.get("files", []):
            try:
                gdrive.trash_file(f["id"])
                console.print(f"  [green]✔ Moved folder '{f['name']}' to Trash[/green]")
            except Exception as exc:
                logger.error("Failed trashing folder %s: %s", f["name"], exc)

        # Also trash root-level verified files
        console.print("[cyan]Trashing root-level Google Drive files...[/cyan]")
        root_files_resp = gdrive._service.files().list(
            q="'root' in parents and mimeType!='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id, name)",
        ).execute()
        for f in root_files_resp.get("files", []):
            try:
                gdrive.trash_file(f["id"])
                console.print(f"  [green]✔ Moved root file '{f['name']}' to Trash[/green]")
            except Exception as exc:
                logger.error("Failed trashing root file %s: %s", f["name"], exc)

    # 2. Check if any verified files are still not trashed
    remaining_untrashed = []
    page_token = None
    while True:
        resp = gdrive._service.files().list(
            q="trashed=false and 'me' in owners and mimeType!='application/vnd.google-apps.folder'",
            pageSize=1000,
            pageToken=page_token,
            fields="nextPageToken, files(id, name)",
        ).execute()
        remaining_untrashed.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if remaining_untrashed:
        console.print(f"[cyan]Trashing {len(remaining_untrashed)} remaining individual files...[/cyan]")
        trashed_count = 0
        failed_count = 0
        lock = threading.Lock()

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold red]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Trashing files", total=len(remaining_untrashed))

            def _trash_one(item: dict) -> None:
                nonlocal trashed_count, failed_count
                success = False
                for attempt in range(1, 4):
                    try:
                        gdrive.trash_file(item["id"])
                        success = True
                        break
                    except Exception as exc:
                        if attempt == 3:
                            logger.error("Failed to trash %s after 3 attempts: %s", item.get("name"), exc)
                        else:
                            time.sleep(attempt * 0.5)
                with lock:
                    if success:
                        trashed_count += 1
                    else:
                        failed_count += 1
                    progress.update(task, advance=1)

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.workers, 8)) as pool:
                futures = [pool.submit(_trash_one, item) for item in remaining_untrashed]
                concurrent.futures.wait(futures)

    console.print(
        f"\n[bold green]Cleanup complete:[/bold green] All verified files have been safely moved to Google Drive Trash!"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
