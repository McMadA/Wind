"""OneDrive Delta Scan & Migration.

Scans the user's Microsoft OneDrive account, cross-checks every item against
local iCloud Drive and Apple Photos, and reports / migrates any delta items
that are not yet in iCloud.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sync_drive.clients.apple_photos import ApplePhotosClient
from sync_drive.clients.icloud import MACOS_ICLOUD_DRIVE_PATH
from sync_drive.clients.onedrive import OneDriveClient
from sync_drive.engine import format_size
from sync_drive.metadata import apply_metadata_to_file, is_media_file

console = Console()
logger = logging.getLogger("sync_drive.onedrive_delta")


def setup_logging(log_filename: str = "logging.txt") -> None:
    os.makedirs("logs", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_log = f"logs/onedrive_delta_{ts}.txt"

    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    fh_main = logging.FileHandler(log_filename, mode="a", encoding="utf-8")
    fh_main.setFormatter(formatter)
    root_logger.addHandler(fh_main)

    fh_ts = logging.FileHandler(timestamped_log, mode="w", encoding="utf-8")
    fh_ts.setFormatter(formatter)
    root_logger.addHandler(fh_ts)


def index_icloud_drive(icloud_root: Path) -> tuple[set[str], set[str]]:
    """Scan local iCloud Drive and return (rel_paths_lower, filename_sizes_lower)."""
    rel_paths: set[str] = set()
    fn_sizes: set[str] = set()

    if not icloud_root.exists():
        return rel_paths, fn_sizes

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

    console.print(f"[green]Indexed {len(rel_paths)} files in local iCloud Drive.[/green]")
    return rel_paths, fn_sizes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m sync_drive.onedrive_delta",
        description="Scan OneDrive for any items not yet in iCloud Drive / Apple Photos.",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Actually migrate new delta items to iCloud (default: simulation/report only).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Worker threads (default: 8).",
    )
    parser.add_argument(
        "--icloud-drive-dir",
        default=str(MACOS_ICLOUD_DRIVE_PATH),
        help="Local macOS iCloud Drive root folder.",
    )
    parser.add_argument(
        "--delta-folder",
        default="OneDrive_Delta",
        help="Subfolder in iCloud Drive for new documents (default: OneDrive_Delta).",
    )
    args = parser.parse_args(argv)

    setup_logging()

    console.print(
        Panel(
            "[bold cyan]Wind Microsoft OneDrive Delta Scan[/bold cyan]\n\n"
            f"• Mode: {'[bold red]LIVE MIGRATION (Download & Import Missing Items)[/bold red]' if args.sync else '[bold yellow]SCAN & REPORT ONLY (Dry-run)[/bold yellow]'}\n"
            "• Deduplication: Cross-checked against local iCloud Drive & Apple Photos index.",
            border_style="red" if args.sync else "cyan",
        )
    )

    # 1. Initialize OneDrive client (triggers login flow if not authenticated)
    console.print("[cyan]Connecting to Microsoft OneDrive...[/cyan]")
    try:
        onedrive = OneDriveClient()
        token = onedrive._get_token()
        if not token:
            console.print("[bold red]Failed to get OneDrive token.[/bold red]")
            return 1
    except Exception as exc:
        console.print(f"[bold red]OneDrive authentication error: {exc}[/bold red]")
        return 1

    # 2. Index local iCloud Drive & Apple Photos
    icloud_root = Path(args.icloud_drive_dir).expanduser().resolve()
    icloud_paths, icloud_fn_sizes = index_icloud_drive(icloud_root)

    console.print("[cyan]Loading Apple Photos index...[/cyan]")
    photos_client = ApplePhotosClient()
    photos_filenames = set(photos_client.filenames)
    photos_fn_sizes = set(photos_client.filename_sizes)
    photos_sha256 = set(photos_client.sha256_hashes)

    # 3. Scan OneDrive recursively
    console.print("[cyan]Scanning OneDrive files (skipping node_modules, .git, venv, etc.)...[/cyan]")
    onedrive_items = []
    last_reported = [0]

    def on_progress(count: int, current_path: str) -> None:
        if count - last_reported[0] >= 250:
            last_reported[0] = count
            short_path = current_path if len(current_path) <= 50 else f"...{current_path[-47:]}"
            console.print(f"  Discovered {count} items so far (in {short_path})")

    try:
        for item in onedrive.list_files("/", progress_callback=on_progress):
            onedrive_items.append(item)
    except Exception as exc:
        console.print(f"[bold red]Failed scanning OneDrive: {exc}[/bold red]")
        return 1

    console.print(f"[bold green]Total items found in OneDrive: {len(onedrive_items)}[/bold green]\n")

    # 4. Classify each item: already in iCloud vs new delta
    delta_documents = []
    delta_media = []
    skipped_count = 0
    total_delta_bytes = 0

    for item in onedrive_items:
        name = item["name"]
        fn_lower = name.strip().lower()
        sz = int(item.get("size") or 0)
        path_lower = item["path"].lower()
        sha256 = (item.get("sha256") or "").lower()

        # Normalize relative path (e.g. strip "/onedrive manual export" or root)
        clean_rel = path_lower.lstrip("/")

        if is_media_file(name, item.get("mimeType")):
            is_dup = (
                fn_lower in photos_filenames
                or (sz > 0 and f"{fn_lower}:{sz}" in photos_fn_sizes)
                or (sha256 and sha256 in photos_sha256)
                or f"{fn_lower}:{sz}" in icloud_fn_sizes
                or f"/{clean_rel}" in icloud_paths
            )
            if is_dup:
                skipped_count += 1
            else:
                delta_media.append(item)
                total_delta_bytes += sz
        else:
            is_dup = (
                f"/{clean_rel}" in icloud_paths
                or f"/onedrive manual export/{clean_rel}" in icloud_paths
                or f"{fn_lower}:{sz}" in icloud_fn_sizes
            )
            if is_dup:
                skipped_count += 1
            else:
                delta_documents.append(item)
                total_delta_bytes += sz

    # 5. Display results table
    table = Table(title="OneDrive Delta Scan Summary", border_style="cyan")
    table.add_column("Category", style="bold")
    table.add_column("Count", justify="right")
    table.add_column("Total Size", justify="right")

    table.add_row("Total OneDrive Items", str(len(onedrive_items)), "-")
    table.add_row(
        "Already in iCloud / Photos (Skipped)",
        f"[green]{skipped_count}[/green]",
        "-",
    )
    table.add_row(
        "New Delta Documents (Not in iCloud)",
        f"[bold yellow]{len(delta_documents)}[/bold yellow]" if delta_documents else "[green]0[/green]",
        format_size(sum(int(x.get("size", 0)) for x in delta_documents)),
    )
    table.add_row(
        "New Delta Media (Not in Apple Photos)",
        f"[bold yellow]{len(delta_media)}[/bold yellow]" if delta_media else "[green]0[/green]",
        format_size(sum(int(x.get("size", 0)) for x in delta_media)),
    )
    console.print(table)
    console.print()

    delta_total = len(delta_documents) + len(delta_media)
    if delta_total == 0:
        console.print(
            "[bold green]✔ All OneDrive items are ALREADY present in your iCloud Drive or Apple Photos![/bold green]\n"
            "There are zero missing files to transfer."
        )
        return 0

    console.print(
        f"[bold cyan]Found {delta_total} new delta file(s) ({format_size(total_delta_bytes)}) not present in iCloud.[/bold cyan]"
    )

    # Write list of delta files to text file for inspection
    delta_report_path = Path("onedrive_delta_items.txt").resolve()
    with open(delta_report_path, "w", encoding="utf-8") as fh:
        fh.write(f"# OneDrive Delta Items ({delta_total} files, {format_size(total_delta_bytes)})\n")
        fh.write("# Documents:\n")
        for d in delta_documents:
            fh.write(f"DOC: {d['path']} ({format_size(d.get('size', 0))})\n")
        fh.write("\n# Media:\n")
        for m in delta_media:
            fh.write(f"MEDIA: {m['path']} ({format_size(m.get('size', 0))})\n")

    console.print(f"[green]Saved detailed delta list to: {delta_report_path}[/green]\n")

    if not args.sync:
        console.print(
            "[bold yellow]DRY-RUN COMPLETE.[/bold yellow] Run with [bold green]--sync[/bold green] to automatically download new documents into iCloud Drive and import new photos/videos into Apple Photos."
        )
        return 0

    # 6. Execute sync if requested
    console.print(f"[bold green]Starting Delta Migration to iCloud ({args.workers} workers)...[/bold green]")
    temp_dir = Path(".sync_temp_onedrive").resolve()
    temp_dir.mkdir(parents=True, exist_ok=True)

    dest_doc_dir = icloud_root / args.delta_folder
    dest_doc_dir.mkdir(parents=True, exist_ok=True)

    imported_docs = 0
    imported_media = 0

    try:
        # Sync Documents in parallel
        if delta_documents:
            console.print(f"\n[cyan]Downloading {len(delta_documents)} documents with {args.workers} workers...[/cyan]")

            def sync_single_doc(doc: dict) -> Path | None:
                try:
                    local_path = onedrive.download_file(doc, str(temp_dir))
                    rel_target = doc["path"].lstrip("/")
                    final_target = dest_doc_dir / rel_target
                    final_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(local_path), str(final_target))
                    logger.info("Migrated OneDrive doc -> iCloud Drive: %s", final_target)
                    return final_target
                except Exception as exc:
                    logger.error("Failed to migrate doc %s: %s", doc["path"], exc)
                    return None

            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(sync_single_doc, doc): doc for doc in delta_documents}
                for fut in as_completed(futures):
                    res = fut.result()
                    if res:
                        imported_docs += 1
                        if imported_docs % 25 == 0 or imported_docs == len(delta_documents):
                            console.print(f"  Migrated {imported_docs}/{len(delta_documents)} documents...")

        # Sync Media: download files in parallel, then import into Apple Photos sequentially
        if delta_media:
            console.print(f"\n[cyan]Downloading {len(delta_media)} media files with {args.workers} workers...[/cyan]")
            downloaded_media = []

            def download_single_media(m: dict) -> tuple[dict, Path | None]:
                try:
                    local_path = onedrive.download_file(m, str(temp_dir))
                    return (m, local_path)
                except Exception as exc:
                    logger.error("Failed downloading media %s: %s", m["path"], exc)
                    return (m, None)

            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(download_single_media, m): m for m in delta_media}
                for fut in as_completed(futures):
                    meta, local_file = fut.result()
                    if local_file and local_file.exists():
                        downloaded_media.append((meta, local_file))

            console.print(f"[cyan]Importing {len(downloaded_media)} media files into Apple Photos...[/cyan]")
            fallback_dir = icloud_root / "Foto_Import_Fallback"
            for idx, (m, local_file) in enumerate(downloaded_media, 1):
                try:
                    photos_client.upload_file(local_file, parent_path="OneDrive Delta", file_meta=m)
                    imported_media += 1
                    logger.info("Imported OneDrive media -> Apple Photos: %s", m["name"])
                except Exception as exc:
                    logger.warning("Could not import %s into Apple Photos (%s); copying to Foto_Import_Fallback", m["name"], exc)
                    try:
                        fallback_dir.mkdir(parents=True, exist_ok=True)
                        fallback_target = fallback_dir / local_file.name
                        shutil.copy2(str(local_file), str(fallback_target))
                        imported_media += 1
                        logger.info("Saved fallback to iCloud Drive: %s", fallback_target)
                    except Exception as fb_exc:
                        logger.error("Failed fallback copy for %s: %s", m["name"], fb_exc)
                finally:
                    if local_file.exists():
                        local_file.unlink()
                if idx % 10 == 0 or idx == len(downloaded_media):
                    console.print(f"  Imported {idx}/{len(downloaded_media)} media items...")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    console.print(
        Panel(
            f"[bold green]✔ OneDrive Delta Migration Complete![/bold green]\n\n"
            f"• Documents migrated: {imported_docs}\n"
            f"• Media items imported: {imported_media}",
            border_style="green",
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
