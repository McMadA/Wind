"""All-in-one Google (Drive + Photos / Takeout) -> iCloud (Drive + Photos) migration pipeline."""

from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")

import argparse
import logging
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sync_drive.cli import _setup_logging
from sync_drive.clients import (
    ApplePhotosClient,
    GDriveClient,
    GooglePhotosClient,
    LocalICloudDriveClient,
    TakeoutClient,
)
from sync_drive.engine import SyncEngine, SyncResult, format_size

LOG_DIR = "logs"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Complete migration from Google Drive + Google Photos/Takeout to iCloud Drive + iCloud Photos "
            "without duplicates and with full EXIF capture date & GPS map metadata preservation."
        )
    )
    parser.add_argument(
        "--gdrive-folder",
        default="root",
        help="Google Drive source folder ID (default: root = entire Drive)",
    )
    parser.add_argument(
        "--icloud-drive-folder",
        default="/",
        help="Destination subfolder inside iCloud Drive for documents (default: /)",
    )
    parser.add_argument(
        "--takeout-dir",
        default=None,
        help="Optional path to unpacked Google Takeout folder for Google Photos (preserves 100%% GPS & JSON metadata)",
    )
    parser.add_argument(
        "--use-gphotos-api",
        action="store_true",
        help="Also scan Google Photos directly via the Google Photos API",
    )
    parser.add_argument(
        "--album",
        default=None,
        help="Optional album name in Apple Photos for imported photos (e.g. 'Google Import')",
    )
    parser.add_argument(
        "--dedup-mode",
        choices=("filename+hash", "hash", "filename", "path", "none"),
        default="filename+hash",
        help="Deduplication strategy (default: filename+hash)",
    )
    parser.add_argument(
        "--skip-documents",
        action="store_true",
        help="Skip Phase 1 (Google Drive documents -> iCloud Drive)",
    )
    parser.add_argument(
        "--skip-gdrive-photos",
        action="store_true",
        help="Skip Phase 2 (Google Drive photos/videos -> iCloud Photos)",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Force rescan of existing Apple Photos library before starting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be migrated without transferring or importing files",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output and progress bars",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose debug logging",
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


def _print_phase_summary(
    console: Console,
    phase_results: list[tuple[str, SyncResult]],
    elapsed: float,
    log_filename: str,
    mirror_txt_filename: str | None = "logging.txt",
) -> None:
    table = Table(title="Google -> iCloud Migration Summary", show_lines=True)
    table.add_column("Phase", style="bold cyan")
    table.add_column("Transferred", justify="right", style="green")
    table.add_column("Verified", justify="right", style="green")
    table.add_column("Skipped (Dedup)", justify="right", style="yellow")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("Data", justify="right")

    logging.info("=" * 72)
    logging.info("GOOGLE -> ICLOUD MIGRATION SUMMARY")
    logging.info("=" * 72)

    total_tx = total_ok = total_skip = total_fail = total_bytes = 0
    for title, res in phase_results:
        total_tx += len(res.transferred)
        total_ok += len(res.verified)
        total_skip += len(res.skipped)
        total_fail += len(res.failed)
        total_bytes += res.total_bytes
        table.add_row(
            title,
            str(len(res.transferred)),
            str(len(res.verified)),
            str(len(res.skipped)),
            str(len(res.failed)),
            format_size(res.total_bytes),
        )
        logging.info(
            "  %-32s | Transferred: %5d | Verified: %5d | Skipped: %5d | Failed: %3d | Data: %s",
            title,
            len(res.transferred),
            len(res.verified),
            len(res.skipped),
            len(res.failed),
            format_size(res.total_bytes),
        )

    table.add_row(
        "[bold]TOTAL[/bold]",
        f"[bold]{total_tx}[/bold]",
        f"[bold]{total_ok}[/bold]",
        f"[bold]{total_skip}[/bold]",
        f"[bold]{total_fail}[/bold]",
        f"[bold]{format_size(total_bytes)}[/bold]",
    )
    logging.info("-" * 72)
    logging.info(
        "  %-32s | Transferred: %5d | Verified: %5d | Skipped: %5d | Failed: %3d | Data: %s",
        "TOTAL",
        total_tx,
        total_ok,
        total_skip,
        total_fail,
        format_size(total_bytes),
    )
    logging.info("  Total elapsed time: %.1fs", elapsed)
    logging.info("=" * 72)

    console.print()
    console.print(table)
    console.print(f"\nTotal elapsed time: [bold]{elapsed:.1f}s[/bold]")
    if mirror_txt_filename:
        console.print(
            f"Full log saved to: [bold]{mirror_txt_filename}[/bold] (and {log_filename})",
            style="dim",
        )
    else:
        console.print(f"Full log saved to: {log_filename}", style="dim")


def main() -> int:
    load_dotenv()
    args = _build_parser().parse_args()

    use_color = sys.stdout.isatty() and not args.no_color and not os.getenv("NO_COLOR")
    console = Console(force_terminal=use_color, no_color=not use_color)

    os.makedirs(LOG_DIR, exist_ok=True)
    log_filename = os.path.join(
        LOG_DIR, f"migrate_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    )
    _setup_logging(args.verbose, console, log_filename, mirror_txt_filename=args.log_file)
    logging.info("Started Wind Smart Migration. Log files: %s and %s", args.log_file, log_filename)

    credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    start_time = time.monotonic()
    phase_results: list[tuple[str, SyncResult]] = []

    console.print(
        Panel(
            "[bold]Google Drive & Google Photos -> iCloud Drive & iCloud Photos[/bold]\n"
            "• Deduplication: Cross-source MD5 + SHA-256 + JPEG Pixel Scan + Apple Photos Index\n"
            "• Metadata: EXIF DateTimeOriginal + GPS Map Coordinates + macOS Creation Date",
            title="Wind Smart Migration",
            border_style="blue",
            padding=(1, 2),
        )
    )

    # Initialize Google Drive client if needed for Phase 1 or Phase 2
    gdrive_client = None
    if not args.skip_documents or not args.skip_gdrive_photos:
        try:
            gdrive_client = GDriveClient(
                credentials_file=credentials_file,
                refresh_cache=args.refresh_cache,
            )
        except Exception as exc:
            logging.error("Failed to initialize Google Drive client: %s", exc)
            return 1

    # ── PHASE 1: Google Drive Documents -> iCloud Drive ──────────────
    if not args.skip_documents and gdrive_client is not None:
        logging.info("--- PHASE 1: Google Drive Documents -> macOS iCloud Drive ---")
        console.print(
            Panel(
                "Phase 1: Syncing Documents from Google Drive -> macOS iCloud Drive (excluding photos/videos)",
                style="bold cyan",
            )
        )
        try:
            icloud_drive_client = LocalICloudDriveClient()
            if args.dry_run:
                docs = list(
                    gdrive_client.list_files(args.gdrive_folder, file_filter="documents")
                )
                for idx, d in enumerate(docs, 1):
                    logging.info(
                        "  [DRY RUN][Phase 1][%d] Document: %s (%s)",
                        idx,
                        d["path"],
                        format_size(d.get("size", 0)),
                    )
                logging.info("[DRY RUN] Phase 1 found %d document(s) in Google Drive.", len(docs))
                res = SyncResult(transferred=[d["path"] for d in docs])
            else:
                engine_docs = SyncEngine(
                    source_client=gdrive_client,
                    dest_client=icloud_drive_client,
                    source_name="gdrive",
                    dest_name="icloud-drive",
                    target_folder=args.icloud_drive_folder,
                    on_duplicate="skip",
                    file_filter="documents",
                    dedup_mode=args.dedup_mode,
                    workers=args.workers,
                    console=console if use_color else None,
                )
                res = engine_docs.run(source_folder=args.gdrive_folder)
            phase_results.append(("1. GDrive Docs -> iCloud Drive", res))
        except Exception as exc:
            logging.error("Phase 1 failed: %s", exc)
            return 1

    # Initialize shared Apple Photos client for Phase 2 & Phase 3
    apple_photos_client = ApplePhotosClient(
        album_name=args.album,
        refresh_cache=args.refresh_cache,
    )

    # ── PHASE 2: Google Drive Photos/Videos -> iCloud Photos ─────────
    if not args.skip_gdrive_photos and gdrive_client is not None:
        logging.info("--- PHASE 2: Google Drive Photos/Videos -> iCloud Photos ---")
        console.print(
            Panel(
                "Phase 2: Migrating Photos & Videos from Google Drive -> iCloud Photos (100% EXIF + GPS)",
                style="bold green",
            )
        )
        try:
            if args.dry_run:
                media_files = list(
                    gdrive_client.list_files(args.gdrive_folder, file_filter="media")
                )
                to_import = []
                skipped = []
                for m in media_files:
                    is_dup, reason = apple_photos_client.check_duplicate_pre_download(
                        m, dedup_mode=args.dedup_mode
                    )
                    meta = m.get("metadata")
                    date_str = meta.taken_at.strftime("%Y-%m-%d %H:%M") if meta and meta.taken_at else "-"
                    gps_str = f"{meta.latitude:.4f},{meta.longitude:.4f}" if meta and meta.has_location else "-"
                    if is_dup:
                        skipped.append(m["path"])
                        logging.info(
                            "  [DRY RUN][Phase 2] SKIP (%s): %s", reason, m["path"]
                        )
                    else:
                        to_import.append(m["path"])
                        logging.info(
                            "  [DRY RUN][Phase 2] IMPORT: %s | Size: %s | Date: %s | GPS: %s",
                            m["path"],
                            format_size(m.get("size", 0)),
                            date_str,
                            gps_str,
                        )
                        # Register in memory so Phase 3 dry-run also detects cross-source duplicates from GDrive
                        if m.get("name"):
                            apple_photos_client.filenames.add(m["name"].strip().lower())
                        if m.get("md5"):
                            apple_photos_client.md5_hashes.add(m["md5"].lower())
                logging.info(
                    "[DRY RUN] Phase 2: %d new media file(s) to import, %d duplicate(s) skipped.",
                    len(to_import),
                    len(skipped),
                )
                res = SyncResult(transferred=to_import, skipped=skipped)
            else:
                engine_gdrive_photos = SyncEngine(
                    source_client=gdrive_client,
                    dest_client=apple_photos_client,
                    source_name="gdrive",
                    dest_name="icloud-photos",
                    target_folder=args.album or "root",
                    on_duplicate="skip",
                    file_filter="media",
                    dedup_mode=args.dedup_mode,
                    workers=args.workers,
                    console=console if use_color else None,
                )
                res = engine_gdrive_photos.run(source_folder=args.gdrive_folder)
            phase_results.append(("2. GDrive Media -> iCloud Photos", res))
        except Exception as exc:
            logging.error("Phase 2 failed: %s", exc)
            return 1

    # ── PHASE 3A: Google Takeout Folder -> iCloud Photos ─────────────
    if args.takeout_dir:
        logging.info("--- PHASE 3A: Google Takeout (%s) -> iCloud Photos ---", args.takeout_dir)
        console.print(
            Panel(
                f"Phase 3A: Migrating Google Photos Takeout ({args.takeout_dir}) -> iCloud Photos (with JSON EXIF/GPS)",
                style="bold magenta",
            )
        )
        try:
            takeout_client = TakeoutClient(root_dir=args.takeout_dir)
            if args.dry_run:
                t_files = list(takeout_client.list_files(args.takeout_dir, file_filter="media"))
                to_import = []
                skipped = []
                for m in t_files:
                    is_dup, reason = apple_photos_client.check_duplicate_pre_download(
                        m, dedup_mode=args.dedup_mode
                    )
                    meta = m.get("metadata")
                    date_str = meta.taken_at.strftime("%Y-%m-%d %H:%M") if meta and meta.taken_at else "-"
                    gps_str = f"{meta.latitude:.4f},{meta.longitude:.4f}" if meta and meta.has_location else "-"
                    if is_dup:
                        skipped.append(m["path"])
                        logging.info(
                            "  [DRY RUN][Phase 3A] SKIP (%s): %s", reason, m["path"]
                        )
                    else:
                        to_import.append(m["path"])
                        logging.info(
                            "  [DRY RUN][Phase 3A] IMPORT: %s | Size: %s | Date: %s | GPS: %s",
                            m["path"],
                            format_size(m.get("size", 0)),
                            date_str,
                            gps_str,
                        )
                        if m.get("name"):
                            apple_photos_client.filenames.add(m["name"].strip().lower())
                logging.info(
                    "[DRY RUN] Phase 3A (Takeout): %d new media file(s) to import, %d duplicate(s) skipped.",
                    len(to_import),
                    len(skipped),
                )
                res = SyncResult(transferred=to_import, skipped=skipped)
            else:
                engine_takeout = SyncEngine(
                    source_client=takeout_client,
                    dest_client=apple_photos_client,
                    source_name="takeout",
                    dest_name="icloud-photos",
                    target_folder=args.album or "root",
                    on_duplicate="skip",
                    file_filter="media",
                    dedup_mode=args.dedup_mode,
                    workers=args.workers,
                    console=console if use_color else None,
                )
                res = engine_takeout.run(source_folder=args.takeout_dir)
            phase_results.append(("3A. Takeout -> iCloud Photos", res))
        except Exception as exc:
            logging.error("Phase 3A (Takeout) failed: %s", exc)
            return 1

    # ── PHASE 3B: Google Photos API -> iCloud Photos ─────────────────
    if args.use_gphotos_api or not args.takeout_dir:
        logging.info("--- PHASE 3B: Google Photos API -> iCloud Photos ---")
        console.print(
            Panel(
                "Phase 3B: Migrating Google Photos API -> iCloud Photos (skipping items already in Drive/Takeout/iCloud)",
                style="bold magenta",
            )
        )
        try:
            gphotos_client = GooglePhotosClient(credentials_file=credentials_file)
            if args.dry_run:
                gp_files = list(gphotos_client.list_files(file_filter="media"))
                to_import = []
                skipped = []
                for m in gp_files:
                    is_dup, reason = apple_photos_client.check_duplicate_pre_download(
                        m, dedup_mode=args.dedup_mode
                    )
                    meta = m.get("metadata")
                    date_str = meta.taken_at.strftime("%Y-%m-%d %H:%M") if meta and meta.taken_at else "-"
                    if is_dup:
                        skipped.append(m["path"])
                        logging.info(
                            "  [DRY RUN][Phase 3B] SKIP (%s): %s", reason, m["path"]
                        )
                    else:
                        to_import.append(m["path"])
                        logging.info(
                            "  [DRY RUN][Phase 3B] IMPORT: %s | Date: %s",
                            m["path"],
                            date_str,
                        )
                        if m.get("name"):
                            apple_photos_client.filenames.add(m["name"].strip().lower())
                logging.info(
                    "[DRY RUN] Phase 3B (GPhotos API): %d new media file(s) to import, %d duplicate(s) skipped.",
                    len(to_import),
                    len(skipped),
                )
                res = SyncResult(transferred=to_import, skipped=skipped)
            else:
                engine_gphotos = SyncEngine(
                    source_client=gphotos_client,
                    dest_client=apple_photos_client,
                    source_name="gphotos",
                    dest_name="icloud-photos",
                    target_folder=args.album or "root",
                    on_duplicate="skip",
                    file_filter="media",
                    dedup_mode=args.dedup_mode,
                    workers=args.workers,
                    console=console if use_color else None,
                )
                res = engine_gphotos.run(source_folder="/")
            phase_results.append(("3B. GPhotos API -> iCloud Photos", res))
        except Exception as exc:
            logging.error("Phase 3B (Google Photos API) failed: %s", exc)
            return 1

    elapsed = time.monotonic() - start_time
    _print_phase_summary(
        console,
        phase_results,
        elapsed,
        log_filename,
        mirror_txt_filename=args.log_file,
    )

    all_ok = all(r.all_ok for _, r in phase_results)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
