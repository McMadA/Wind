"""
Google Drive → Google Photos Sync Script

Automatically downloads images from Google Drive (in memory) and uploads
them directly to Google Photos via official APIs.

Prerequisites:
  1. pip install google-api-python-client google-auth google-auth-oauthlib requests
  2. Enable Google Drive API and Google Photos Library API in Google Cloud Console
  3. Create OAuth client (Desktop app) and download client_secret.json

Usage:
  python drive_to_photos_sync.py                          # Browse & pick folder interactively
  python drive_to_photos_sync.py --folder FOLDER_ID       # Sync specific folder (skip browser)
  python drive_to_photos_sync.py --all                    # Sync ALL images in Drive
  python drive_to_photos_sync.py --since 2025-01-01       # Incremental sync
"""

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

import requests
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
]

CLIENT_SECRET_FILE = "client_secret.json"
TOKEN_FILE = "token.json"
UPLOADED_LOG = "uploaded_ids.json"

PHOTOS_UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
PHOTOS_BATCH_CREATE_URL = (
    "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
)

IMAGE_MIME_TYPES = [
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/heic",
    "image/heif",
    "image/bmp",
    "image/tiff",
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
]


# ---------------------------------------------------------------------------
# Auth (with token caching so you don't re-auth every run)
# ---------------------------------------------------------------------------
def authenticate():
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET_FILE):
                print(f"ERROR: {CLIENT_SECRET_FILE} not found.")
                print("Download it from Google Cloud Console → APIs & Services → Credentials")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return creds


# ---------------------------------------------------------------------------
# Uploaded tracking (avoid duplicates)
# ---------------------------------------------------------------------------
def load_uploaded_ids() -> set:
    if os.path.exists(UPLOADED_LOG):
        with open(UPLOADED_LOG) as f:
            return set(json.load(f))
    return set()


def save_uploaded_ids(ids: set):
    with open(UPLOADED_LOG, "w") as f:
        json.dump(sorted(ids), f)


# ---------------------------------------------------------------------------
# Interactive folder browser
# ---------------------------------------------------------------------------
def list_folders(drive_service, parent_id="root"):
    """List subfolders of a given parent folder."""
    query = (
        f"'{parent_id}' in parents "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    all_folders = []
    page_token = None

    while True:
        response = drive_service.files().list(
            q=query,
            pageSize=1000,
            fields="nextPageToken, files(id, name)",
            orderBy="name",
            pageToken=page_token,
        ).execute()

        all_folders.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return all_folders


def count_images_in_folder(drive_service, folder_id):
    """Quick count of image/video files directly in a folder."""
    mime_filter = " or ".join(f"mimeType='{m}'" for m in IMAGE_MIME_TYPES)
    query = f"({mime_filter}) and '{folder_id}' in parents and trashed = false"

    response = drive_service.files().list(
        q=query, pageSize=1, fields="files(id)"
    ).execute()

    # Just check if there are any — full count is expensive
    return len(response.get("files", [])) > 0


def browse_folders(drive_service):
    """Interactive folder browser. Returns selected folder ID and name."""
    current_id = "root"
    current_name = "My Drive"
    path_stack = []  # (id, name) history for going back

    while True:
        folders = list_folders(drive_service, current_id)
        has_images = count_images_in_folder(drive_service, current_id)

        print(f"\n{'='*60}")
        print(f"  📁 {current_name}")
        if path_stack:
            print(f"  Path: My Drive / {' / '.join(n for _, n in path_stack)}")
        print(f"  {'(contains images/videos)' if has_images else '(no images here)'}")
        print(f"{'='*60}")

        if path_stack:
            print(f"  [0] ← Back to {path_stack[-1][1] if len(path_stack) > 1 else 'My Drive'}")

        if not folders:
            print("  (no subfolders)")
        else:
            for i, folder in enumerate(folders, 1):
                print(f"  [{i}] 📁 {folder['name']}")

        print()
        print(f"  [S] ✅ SELECT this folder ({current_name})")
        print(f"  [Q] ❌ Cancel")
        print()

        choice = input("  Your choice: ").strip().lower()

        if choice == "q":
            print("Cancelled.")
            sys.exit(0)

        if choice == "s":
            return current_id, current_name

        if choice == "0" and path_stack:
            current_id, current_name = path_stack.pop()
            continue

        try:
            idx = int(choice)
            if 1 <= idx <= len(folders):
                path_stack.append((current_id, current_name))
                current_id = folders[idx - 1]["id"]
                current_name = folders[idx - 1]["name"]
            else:
                print("  Invalid number, try again.")
        except ValueError:
            print("  Invalid input, try again.")


def browse_and_select_multiple(drive_service):
    """Let user pick one or more folders, then confirm."""
    selected = []

    while True:
        folder_id, folder_name = browse_folders(drive_service)
        selected.append((folder_id, folder_name))

        print(f"\n  Selected so far: {', '.join(n for _, n in selected)}")
        add_more = input("  Add another folder? [y/N]: ").strip().lower()
        if add_more != "y":
            break

    return selected


# ---------------------------------------------------------------------------
# Drive operations
# ---------------------------------------------------------------------------
def list_drive_images(drive_service, folder_id=None, since=None):
    """List all image/video files from Drive, optionally filtered by folder and date."""
    mime_filter = " or ".join(f"mimeType='{m}'" for m in IMAGE_MIME_TYPES)
    query = f"({mime_filter})"

    if folder_id:
        query += f" and '{folder_id}' in parents"
    if since:
        query += f" and modifiedTime > '{since}T00:00:00'"

    query += " and trashed = false"

    all_files = []
    page_token = None

    while True:
        response = drive_service.files().list(
            q=query,
            pageSize=1000,
            fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
            pageToken=page_token,
        ).execute()

        all_files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")

        if not page_token:
            break

    return all_files


def download_file(drive_service, file_id) -> io.BytesIO:
    """Download a file from Drive into memory."""
    request = drive_service.files().get_media(fileId=file_id)
    file_stream = io.BytesIO()
    downloader = MediaIoBaseDownload(file_stream, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    file_stream.seek(0)
    return file_stream


# ---------------------------------------------------------------------------
# Photos operations
# ---------------------------------------------------------------------------
def upload_to_photos(access_token: str, file_stream: io.BytesIO, filename: str) -> bool:
    """Upload a file to Google Photos. Returns True on success."""
    # Step 1: Upload raw bytes → get upload token
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-type": "application/octet-stream",
        "X-Goog-Upload-File-Name": filename,
        "X-Goog-Upload-Protocol": "raw",
    }

    resp = requests.post(PHOTOS_UPLOAD_URL, headers=headers, data=file_stream.read())

    if resp.status_code != 200:
        print(f"  Upload bytes failed ({resp.status_code}): {resp.text}")
        return False

    upload_token = resp.text

    # Step 2: Create media item
    body = {
        "newMediaItems": [
            {"simpleMediaItem": {"uploadToken": upload_token}}
        ]
    }

    resp = requests.post(
        PHOTOS_BATCH_CREATE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-type": "application/json",
        },
        json=body,
    )

    if resp.status_code == 200:
        results = resp.json().get("newMediaItemResults", [])
        if results and results[0].get("status", {}).get("message") == "Success":
            return True
        elif results:
            print(f"  Media create issue: {results[0].get('status')}")
            return False

    print(f"  Batch create failed ({resp.status_code}): {resp.text}")
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Sync images from Google Drive to Google Photos")
    parser.add_argument("--folder", help="Google Drive folder ID to sync from (skip browser)")
    parser.add_argument("--all", action="store_true", help="Sync ALL images in Drive (no folder filter)")
    parser.add_argument("--since", help="Only sync files modified after this date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="List files without uploading")
    parser.add_argument("--limit", type=int, help="Max number of files to upload")
    args = parser.parse_args()

    print("Authenticating...")
    creds = authenticate()
    drive_service = build("drive", "v3", credentials=creds)

    # Determine which folder(s) to sync
    if args.all:
        folder_ids = [None]  # None = no folder filter = all Drive
        print("Mode: syncing ALL images in Drive")
    elif args.folder:
        folder_ids = [args.folder]
        print(f"Mode: syncing folder {args.folder}")
    else:
        # Interactive folder browser
        print("\nBrowse your Google Drive to pick folder(s) to sync:\n")
        selected = browse_and_select_multiple(drive_service)
        folder_ids = [fid for fid, _ in selected]
        print(f"\nWill sync: {', '.join(n for _, n in selected)}")

    # Collect files from all selected folders
    all_files = []
    for fid in folder_ids:
        print(f"Listing images{f' in folder {fid}' if fid else ' across entire Drive'}...")
        files = list_drive_images(drive_service, folder_id=fid, since=args.since)
        all_files.extend(files)

    # Deduplicate (in case of overlapping selections)
    seen = set()
    files = []
    for f in all_files:
        if f["id"] not in seen:
            seen.add(f["id"])
            files.append(f)

    print(f"Found {len(files)} image/video files total")

    if not files:
        print("Nothing to sync.")
        return

    # Filter already uploaded
    uploaded_ids = load_uploaded_ids()
    new_files = [f for f in files if f["id"] not in uploaded_ids]
    print(f"Skipping {len(files) - len(new_files)} already uploaded, {len(new_files)} to process")

    if args.limit:
        new_files = new_files[: args.limit]
        print(f"Limited to {args.limit} files")

    if args.dry_run:
        print("\n--- DRY RUN ---")
        for f in new_files:
            size_mb = int(f.get("size", 0)) / (1024 * 1024)
            print(f"  {f['name']} ({size_mb:.1f} MB) [{f['mimeType']}]")
        return

    # Confirm before uploading
    confirm = input(f"\nUpload {len(new_files)} files to Google Photos? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return

    # Upload loop
    success_count = 0
    fail_count = 0

    for i, f in enumerate(new_files, 1):
        print(f"[{i}/{len(new_files)}] {f['name']}...", end=" ")

        try:
            stream = download_file(drive_service, f["id"])
            ok = upload_to_photos(creds.token, stream, f["name"])

            if ok:
                print("✓")
                uploaded_ids.add(f["id"])
                success_count += 1
            else:
                print("✗")
                fail_count += 1

            # Save progress every 50 files
            if i % 50 == 0:
                save_uploaded_ids(uploaded_ids)

            # Respect rate limits (~10/sec is safe)
            time.sleep(0.1)

        except Exception as e:
            print(f"ERROR: {e}")
            fail_count += 1

    # Final save
    save_uploaded_ids(uploaded_ids)

    print(f"\nDone! Uploaded: {success_count}, Failed: {fail_count}")


if __name__ == "__main__":
    main()
