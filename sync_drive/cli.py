"""CLI entry point for OneDrive -> Google Drive sync."""

import argparse
import logging
import sys
from datetime import datetime

from dotenv import load_dotenv
import os

from sync_drive.gdrive_client import GDriveClient
from sync_drive.onedrive_client import OneDriveClient
from sync_drive.sync_engine import SyncEngine

LOG_DIR = "logs"


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Sync files from OneDrive to Google Drive with verification."
    )
    parser.add_argument(
        "--onedrive-folder",
        default=os.getenv("ONEDRIVE_SYNC_FOLDER", "/"),
        help="OneDrive folder to sync (default: root /)",
    )
    parser.add_argument(
        "--gdrive-folder-id",
        default=os.getenv("GOOGLE_DRIVE_TARGET_FOLDER", "root"),
        help="Google Drive destination folder ID (default: root)",
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
        help="How to handle files that already exist in Google Drive: "
             "skip (default), overwrite, or duplicate",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose (DEBUG) logging",
    )
    args = parser.parse_args()

    # ── logging: console + log file ─────────────────────────────────
    os.makedirs(LOG_DIR, exist_ok=True)
    log_filename = os.path.join(
        LOG_DIR, f"sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )

    log_level = logging.DEBUG if args.verbose else logging.INFO
    log_format = "%(asctime)s  %(levelname)-8s  %(message)s"

    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_filename, encoding="utf-8"),
        ],
    )
    logging.info("Log file: %s", log_filename)

    # ── build clients ───────────────────────────────────────────────
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

    # ── run sync ────────────────────────────────────────────────────
    engine = SyncEngine(
        onedrive=onedrive,
        gdrive=gdrive,
        temp_dir=args.temp_dir,
        target_folder_id=args.gdrive_folder_id,
        on_duplicate=args.on_duplicate,
    )

    print("\n=== OneDrive -> Google Drive Sync ===\n")
    logging.info("Duplicate mode: %s", args.on_duplicate)
    result = engine.run(onedrive_folder=args.onedrive_folder)

    summary = result.summary()
    print(f"\n{'='*40}")
    print(summary)
    print(f"{'='*40}\n")
    logging.info("Sync complete.\n%s", summary)

    if result.all_ok:
        print("All files synced and verified successfully.")
        print(f"Full log saved to: {log_filename}")
        return 0
    else:
        print("Some files failed — see details above.")
        print(f"Full log saved to: {log_filename}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
