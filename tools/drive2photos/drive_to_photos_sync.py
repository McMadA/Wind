"""
Google Drive → Google Photos Sync Script  (v2)
===============================================

Features
--------
- Multi-threaded uploads (--workers N, default 10)
- Three dedup modes  : filename | hash | filename+hash
- Photos library cache: avoids re-scanning on every run (--refresh-cache to force)
- Metadata preserved : original filename + Drive timestamps stored in description
- Thread-safe state  : uploaded_ids.json and uploaded_hashes.json, atomic saves
- Graceful Ctrl+C    : saves progress before exiting
- Progress saved every N files (--save-every N, default 25)
- Dry-run mode       : shows what would be uploaded / skipped without touching Photos

Prerequisites
-------------
  pip install google-api-python-client google-auth google-auth-oauthlib requests

  1. Enable "Google Drive API" and "Google Photos Library API" in Google Cloud Console.
  2. Create an OAuth 2.0 client (Desktop app) and download client_secret.json.
  3. Place client_secret.json in the same directory as this script.

File structure (all created automatically on first run)
-------------------------------------------------------
  credentials.json.json        — OAuth credentials  (you supply this)
  token.json                — Cached OAuth token (auto-refreshed)
  uploaded_ids.json         — Drive file IDs already uploaded (progress log)
  uploaded_hashes.json      — SHA-256 hashes of uploaded content (hash dedup)
  photos_filename_cache.json— Cached list of Photos filenames   (filename dedup)

Usage examples
--------------
  python drive_to_photos_sync.py
  python drive_to_photos_sync.py --folder DRIVE_FOLDER_ID
  python drive_to_photos_sync.py --all --workers 8
  python drive_to_photos_sync.py --dedup-mode filename+hash --refresh-cache
  python drive_to_photos_sync.py --dry-run --all
  python drive_to_photos_sync.py --skip-dedup --since 2024-06-01

Dedup mode details
------------------
  none          — no dedup; fastest, but Photos may contain duplicates
  filename      — skip if the filename already exists anywhere in Photos
                  (fast pre-download check; false-positive rate depends on
                  how unique your filenames are)
  hash          — download file first, compute SHA-256, skip if this script
                  has uploaded the same content before (tracks uploaded_hashes.json)
  filename+hash — skip if EITHER a filename match OR a hash match is found;
                  most thorough option; requires download before deciding on hash
"""

# ============================================================
# Standard-library imports
# ============================================================
import argparse
import hashlib
import io
import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

# ============================================================
# Third-party imports
# ============================================================
import requests
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ============================================================
# Constants
# ============================================================

# OAuth scopes — photoslibrary.readonly is needed for the dedup cache scan,
# photoslibrary.appendonly for uploads.  Together they cover all functionality.
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly",
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
]

CLIENT_SECRET_FILE = "../../credentials.json"
TOKEN_FILE = "../../token.json"

# Persistent state files
UPLOADED_IDS_FILE = "uploaded_ids.json"
UPLOADED_HASHES_FILE = "uploaded_hashes.json"
PHOTOS_FILENAME_CACHE_FILE = "photos_filename_cache.json"

# Google Photos API endpoints
PHOTOS_UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
PHOTOS_BATCH_CREATE_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
PHOTOS_LIST_URL = "https://photoslibrary.googleapis.com/v1/mediaItems"

# Media types this script handles
SUPPORTED_MIME_TYPES = [
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "image/heic", "image/heif", "image/bmp", "image/tiff",
    "video/mp4", "video/quicktime", "video/x-msvideo",
    "video/mpeg", "video/3gpp",
]

DEFAULT_WORKERS = 10
DEFAULT_SAVE_EVERY = 25

# Retry config for transient HTTP errors (rate limits, 5xx)
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds; doubled on each attempt


# ============================================================
# Authentication
# ============================================================

def authenticate() -> Credentials:
    """
    OAuth2 flow with token caching.

    On first run opens a browser for consent.  On subsequent runs the cached
    token is reused and silently refreshed when it expires.

    If you change SCOPES you must delete token.json and re-authenticate.
    """
    creds: Optional[Credentials] = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET_FILE):
                print(f"ERROR: {CLIENT_SECRET_FILE} not found.")
                print(
                    "Download it from Google Cloud Console "
                    "→ APIs & Services → Credentials"
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRET_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())

    return creds


# ============================================================
# Persistent JSON helpers  (atomic writes to prevent corruption)
# ============================================================

def _load_json_set(path: str) -> Set[str]:
    """Load a JSON array from disk as a Python set.  Returns empty set if absent."""
    if os.path.exists(path):
        with open(path) as fh:
            return set(json.load(fh))
    return set()


def _save_json_set(data: Set[str], path: str) -> None:
    """
    Atomically save a set to a JSON array file.

    Writes to a .tmp sibling first then renames, so a crash mid-write never
    leaves a truncated file.
    """
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(sorted(data), fh)
    os.replace(tmp, path)


# ============================================================
# Thread-safe sync state
# ============================================================

class SyncState:
    """
    Central, thread-safe container for tracking all upload progress.

    Maintains two on-disk sets that persist across runs:
      uploaded_ids.json    — Drive file IDs that were successfully uploaded
      uploaded_hashes.json — SHA-256 content hashes that were successfully uploaded

    Progress is saved every `save_every` completed operations.  Calling
    flush() forces an immediate save (used on shutdown).
    """

    def __init__(self, save_every: int = DEFAULT_SAVE_EVERY) -> None:
        self._lock = threading.Lock()
        self.save_every = save_every
        self._ops_since_save = 0

        # Human-readable counters (reads outside lock are fine; worst case
        # the display is slightly stale, which is acceptable)
        self.success_count = 0
        self.fail_count = 0
        self.skip_count = 0

        # Loaded from disk; updated in memory and periodically flushed
        self.uploaded_ids: Set[str] = _load_json_set(UPLOADED_IDS_FILE)
        self.uploaded_hashes: Set[str] = _load_json_set(UPLOADED_HASHES_FILE)

        # Set this to ask all worker threads to wind down gracefully
        self.shutdown = threading.Event()

    # ---- Queries ----

    def is_uploaded_id(self, file_id: str) -> bool:
        with self._lock:
            return file_id in self.uploaded_ids

    def is_uploaded_hash(self, file_hash: str) -> bool:
        with self._lock:
            return file_hash in self.uploaded_hashes

    # ---- Recording outcomes ----

    def record_success(
        self, file_id: str, file_hash: Optional[str] = None
    ) -> None:
        with self._lock:
            self.uploaded_ids.add(file_id)
            if file_hash:
                self.uploaded_hashes.add(file_hash)
            self.success_count += 1
            self._ops_since_save += 1
            if self._ops_since_save >= self.save_every:
                self._persist()

    def record_failure(self) -> None:
        with self._lock:
            self.fail_count += 1

    def record_skip(self) -> None:
        with self._lock:
            self.skip_count += 1

    # ---- Persistence ----

    def _persist(self) -> None:
        """Write state to disk.  MUST be called with self._lock held."""
        _save_json_set(self.uploaded_ids, UPLOADED_IDS_FILE)
        _save_json_set(self.uploaded_hashes, UPLOADED_HASHES_FILE)
        self._ops_since_save = 0

    def flush(self) -> None:
        """Force-save to disk — call this on exit / Ctrl+C."""
        with self._lock:
            self._persist()


# ============================================================
# Google Photos filename cache  (speeds up repeated runs)
# ============================================================

class PhotosFilenameCache:
    """
    Local cache of every filename present in a user's Google Photos library.

    Why a cache?  The Photos API has no search-by-filename endpoint; the only
    option is to page through every media item.  For large libraries that is
    slow (minutes) so we persist the result in photos_filename_cache.json and
    re-use it on subsequent runs.

    Use --refresh-cache to force a full rescan (e.g. after bulk deletions or
    uploads from outside this script).

    Thread safety: add() uses a lock so worker threads can update the in-memory
    set after successful uploads without races.
    """

    def __init__(self, get_token: Callable[[], str]) -> None:
        """
        Args:
            get_token: Callable that returns a valid access token string.
                       Called lazily so token refresh is always up to date.
        """
        self._get_token = get_token
        self._lock = threading.Lock()
        self.filenames: Set[str] = set()
        self.loaded = False

    def ensure_loaded(self, force_refresh: bool = False) -> None:
        """Load from disk cache, or rebuild from the Photos API if needed."""
        if self.loaded and not force_refresh:
            return

        if not force_refresh and os.path.exists(PHOTOS_FILENAME_CACHE_FILE):
            print("Loading Photos filename cache from disk...")
            with open(PHOTOS_FILENAME_CACHE_FILE) as fh:
                data = json.load(fh)
            self.filenames = set(data.get("filenames", []))
            cached_at = data.get("last_updated", "unknown date")
            print(
                f"  {len(self.filenames):,} unique filenames loaded "
                f"(cached {cached_at})"
            )
            print("  Tip: run with --refresh-cache to force a fresh scan.")
            self.loaded = True
            return

        self._rebuild()

    def _rebuild(self) -> None:
        """Page through the entire Photos library and cache all filenames."""
        print("Scanning your Google Photos library for existing filenames...")
        print(
            "  (This is a one-time operation; results are saved locally.)"
        )

        filenames: List[str] = []
        page_token: Optional[str] = None
        total_items = 0

        while True:
            params: Dict = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token

            resp = requests.get(
                PHOTOS_LIST_URL,
                headers={"Authorization": f"Bearer {self._get_token()}"},
                params=params,
            )

            if resp.status_code == 429:
                print("  Rate-limited by Photos API — waiting 60 s …")
                time.sleep(60)
                continue

            if resp.status_code != 200:
                print(
                    f"  WARNING: Photos API returned {resp.status_code} "
                    f"({resp.text[:120]}) — cache may be incomplete."
                )
                break

            body = resp.json()
            for item in body.get("mediaItems", []):
                fn = item.get("filename", "")
                if fn:
                    filenames.append(fn)

            total_items += len(body.get("mediaItems", []))
            page_token = body.get("nextPageToken")

            if total_items > 0 and total_items % 1_000 == 0:
                print(f"  Scanned {total_items:,} Photos items …")

            if not page_token:
                break

        self.filenames = set(filenames)
        print(
            f"  Done. {len(self.filenames):,} unique filenames "
            f"across {total_items:,} total Photos items."
        )

        tmp_data = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "filenames": sorted(self.filenames),
            "item_count": total_items,
        }
        with open(PHOTOS_FILENAME_CACHE_FILE, "w") as fh:
            json.dump(tmp_data, fh, indent=2)

        self.loaded = True

    def contains(self, filename: str) -> bool:
        with self._lock:
            return filename in self.filenames

    def add(self, filename: str) -> None:
        """Update in-memory cache after a successful upload (thread-safe)."""
        with self._lock:
            self.filenames.add(filename)


# ============================================================
# Drive helpers
# ============================================================

# Each worker thread gets its own Drive API service object (not thread-safe)
_thread_local = threading.local()

# Serialise OAuth token refresh across threads so we never issue two concurrent
# refresh requests (which would race on writing token.json)
_creds_refresh_lock = threading.Lock()


def _get_drive_service(creds: Credentials):
    """Return the thread-local Drive service, creating it on first access."""
    if not hasattr(_thread_local, "drive"):
        _thread_local.drive = build("drive", "v3", credentials=creds)
    return _thread_local.drive


def _get_session() -> requests.Session:
    """Return the thread-local requests.Session, creating it on first access.

    Reusing a Session keeps the underlying TCP/TLS connection alive across the
    two Photos API calls per file (upload bytes + batchCreate), eliminating one
    TLS handshake per upload on average.
    """
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
    return _thread_local.session


def _refresh_token_if_needed(creds: Credentials) -> None:
    """Thread-safely refresh the OAuth access token when it has expired."""
    with _creds_refresh_lock:
        if not creds.valid:
            creds.refresh(Request())


def list_folders(drive_service, parent_id: str = "root") -> List[Dict]:
    """Return immediate child folders of *parent_id*, sorted by name."""
    query = (
        f"'{parent_id}' in parents "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    folders: List[Dict] = []
    page_token: Optional[str] = None

    while True:
        resp = (
            drive_service.files()
            .list(
                q=query,
                pageSize=1000,
                fields="nextPageToken, files(id, name)",
                orderBy="name",
                pageToken=page_token,
            )
            .execute()
        )
        folders.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return folders


def folder_has_media(drive_service, folder_id: str) -> bool:
    """Quick check: does this folder contain at least one supported media file?"""
    mime_filter = " or ".join(f"mimeType='{m}'" for m in SUPPORTED_MIME_TYPES)
    q = f"({mime_filter}) and '{folder_id}' in parents and trashed = false"
    resp = (
        drive_service.files()
        .list(q=q, pageSize=1, fields="files(id)")
        .execute()
    )
    return bool(resp.get("files"))


def browse_folders(drive_service) -> Tuple[str, str]:
    """
    Interactive folder browser.

    Lets the user navigate their Drive hierarchy and returns
    (folder_id, folder_name) for the chosen folder.
    """
    current_id = "root"
    current_name = "My Drive"
    stack: List[Tuple[str, str]] = []

    while True:
        folders = list_folders(drive_service, current_id)
        has_media = folder_has_media(drive_service, current_id)

        print(f"\n{'=' * 62}")
        print(f"  {current_name}")
        if stack:
            path = " / ".join(n for _, n in stack)
            print(f"  Path: My Drive / {path}")
        print(f"  {'(contains images/videos)' if has_media else '(no media here)'}")
        print("=" * 62)

        if stack:
            back = stack[-1][1] if len(stack) > 1 else "My Drive"
            print(f"  [0] Back to {back}")

        if not folders:
            print("  (no subfolders)")
        else:
            for i, f in enumerate(folders, 1):
                print(f"  [{i:>3}] {f['name']}")

        print(f"\n  [S] Select this folder   [Q] Cancel\n")
        choice = input("  > ").strip().lower()

        if choice == "q":
            print("Cancelled.")
            sys.exit(0)
        elif choice == "s":
            return current_id, current_name
        elif choice == "0" and stack:
            current_id, current_name = stack.pop()
        else:
            try:
                idx = int(choice)
                if 1 <= idx <= len(folders):
                    stack.append((current_id, current_name))
                    current_id = folders[idx - 1]["id"]
                    current_name = folders[idx - 1]["name"]
                else:
                    print("  Invalid number — try again.")
            except ValueError:
                print("  Invalid input — try again.")


def browse_and_select_multiple(
    drive_service,
) -> List[Tuple[str, str]]:
    """Repeat the folder browser until the user stops adding folders."""
    selected: List[Tuple[str, str]] = []

    while True:
        fid, fname = browse_folders(drive_service)
        selected.append((fid, fname))
        print(f"\n  Selected so far: {', '.join(n for _, n in selected)}")
        if input("  Add another folder? [y/N]: ").strip().lower() != "y":
            break

    return selected


def collect_all_folder_ids(drive_service, parent_id: str) -> List[str]:
    """
    Recursively collect the IDs of *parent_id* and all its descendants.

    Depth-first traversal; prints each subfolder name as it goes so the
    user can see progress in large trees.
    """
    ids = [parent_id]
    query = (
        f"'{parent_id}' in parents "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    page_token: Optional[str] = None

    while True:
        resp = (
            drive_service.files()
            .list(
                q=query,
                pageSize=1000,
                fields="nextPageToken, files(id, name)",
                pageToken=page_token,
            )
            .execute()
        )
        for folder in resp.get("files", []):
            print(f"      Subfolder: {folder['name']}")
            ids.extend(collect_all_folder_ids(drive_service, folder["id"]))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return ids


def list_drive_media(
    drive_service,
    folder_id: Optional[str],
    since: Optional[str],
    recursive: bool = True,
) -> List[Dict]:
    """
    Return all supported media files in Drive, optionally scoped to a folder.

    Args:
        folder_id: Restrict results to this folder (and its descendants if
                   recursive=True).  None = search the entire Drive.
        since:     ISO date string (YYYY-MM-DD).  Only files with
                   modifiedTime after this date are returned.
        recursive: When True and folder_id is set, recurse into subfolders.

    Returns a list of dicts with fields:
        id, name, mimeType, size, createdTime, modifiedTime
    """
    mime_filter = " or ".join(f"mimeType='{m}'" for m in SUPPORTED_MIME_TYPES)

    if folder_id and recursive:
        print("  Scanning folder tree recursively …")
        folder_ids = collect_all_folder_ids(drive_service, folder_id)
        print(f"  Found {len(folder_ids)} folder(s) total")
    elif folder_id:
        folder_ids = [folder_id]
    else:
        folder_ids = [None]  # None → no parent filter → entire Drive

    all_files: List[Dict] = []

    for fid in folder_ids:
        q = f"({mime_filter})"
        if fid:
            q += f" and '{fid}' in parents"
        if since:
            q += f" and modifiedTime > '{since}T00:00:00'"
        q += " and trashed = false"

        page_token: Optional[str] = None
        while True:
            resp = (
                drive_service.files()
                .list(
                    q=q,
                    pageSize=1000,
                    fields=(
                        "nextPageToken, "
                        "files(id, name, mimeType, size, createdTime, modifiedTime)"
                    ),
                    pageToken=page_token,
                )
                .execute()
            )
            all_files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    return all_files


def download_file(drive_service, file_id: str) -> bytes:
    """Download a Drive file completely into memory and return its bytes."""
    request = drive_service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


# ============================================================
# Google Photos upload helpers
# ============================================================

def photos_upload_bytes(
    token: str, data: bytes, filename: str
) -> Optional[str]:
    """
    Upload raw file bytes to the Photos resumable-upload endpoint.

    Returns the upload token string on success, or None on failure.
    Retries up to MAX_RETRIES times on rate-limit (HTTP 429) responses.
    """
    for attempt in range(MAX_RETRIES):
        resp = _get_session().post(
            PHOTOS_UPLOAD_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-type": "application/octet-stream",
                "X-Goog-Upload-File-Name": filename,
                "X-Goog-Upload-Protocol": "raw",
            },
            data=data,
        )

        if resp.status_code == 200:
            return resp.text  # upload token

        if resp.status_code == 429:
            wait = RETRY_BASE_DELAY * (2 ** attempt)
            _tlog(f"  Rate-limited (upload bytes) — retrying in {wait:.0f} s …")
            time.sleep(wait)
            continue

        # Non-retryable failure
        _tlog(
            f"  Upload bytes failed (HTTP {resp.status_code}): "
            f"{resp.text[:120]}"
        )
        break

    return None


def photos_create_item(
    token: str,
    upload_token: str,
    filename: str,
    description: Optional[str],
) -> bool:
    """
    Create a Photos media item from a previously obtained upload token.

    *filename* sets the display filename in Photos; it is passed both in
    the upload-bytes step and here to make sure it round-trips correctly.

    *description* stores original Drive metadata (creation date, etc.).
    The Photos API silently truncates descriptions to 1 000 characters.

    Returns True on success, False otherwise.
    """
    item: Dict = {
        "simpleMediaItem": {
            "uploadToken": upload_token,
            "fileName": filename,
        }
    }
    if description:
        item["description"] = description[:1000]

    for attempt in range(MAX_RETRIES):
        resp = _get_session().post(
            PHOTOS_BATCH_CREATE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-type": "application/json",
            },
            json={"newMediaItems": [item]},
        )

        if resp.status_code == 429:
            wait = RETRY_BASE_DELAY * (2 ** attempt)
            _tlog(f"  Rate-limited (create item) — retrying in {wait:.0f} s …")
            time.sleep(wait)
            continue

        if resp.status_code != 200:
            _tlog(
                f"  batchCreate failed (HTTP {resp.status_code}): "
                f"{resp.text[:120]}"
            )
            break

        results = resp.json().get("newMediaItemResults", [])
        if not results:
            return False

        status = results[0].get("status", {})
        # gRPC status: code 0 = OK.  The message field may say "Success".
        return status.get("code", -1) == 0 or "success" in status.get(
            "message", ""
        ).lower()

    return False


# ============================================================
# Thread-safe logging
# ============================================================

_print_lock = threading.Lock()


def _tlog(msg: str) -> None:
    """Print *msg* without interleaving from concurrent threads."""
    with _print_lock:
        print(msg)


# ============================================================
# Batch collector  (coalesces batchCreate calls across workers)
# ============================================================

class BatchCollector:
    """
    Collects upload tokens from worker threads and fires them to the Photos
    API in batches of up to 50, which is the maximum the batchCreate endpoint
    accepts per request.

    A dedicated daemon thread drives flushing:
      - Immediately when the buffer reaches 50 items.
      - After at most 3 seconds of inactivity (so the tail of a run is never
        stuck waiting).

    Workers call enqueue() — which is non-blocking — and return "uploaded"
    optimistically.  Per-item success/failure is recorded asynchronously by
    _do_flush() once the API responds.

    Call drain() after the thread pool has finished to flush any remaining
    items before printing the summary.
    """

    _BATCH_SIZE = 50
    _FLUSH_INTERVAL = 3.0  # seconds

    def __init__(
        self,
        get_token: Callable[[], str],
        state: "SyncState",
        photos_cache: Optional["PhotosFilenameCache"],
    ) -> None:
        self._get_token = get_token
        self._state = state
        self._photos_cache = photos_cache

        self._lock = threading.Lock()
        self._buffer: List[Dict] = []
        self._flush_event = threading.Event()
        self._stopped = False

        self._thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._thread.start()

    # ---- Public API ----

    def enqueue(
        self,
        upload_token: str,
        filename: str,
        description: Optional[str],
        file_id: str,
        file_hash: Optional[str],
        size_mb: float,
        prefix: str,
    ) -> None:
        """Add one item to the batch buffer (thread-safe, non-blocking)."""
        with self._lock:
            self._buffer.append({
                "upload_token": upload_token,
                "filename": filename,
                "description": description,
                "file_id": file_id,
                "file_hash": file_hash,
                "size_mb": size_mb,
                "prefix": prefix,
            })
            if len(self._buffer) >= self._BATCH_SIZE:
                self._flush_event.set()

    def drain(self) -> None:
        """Flush all remaining items; blocks until complete.  Call once, after
        the thread pool exits and before printing the final summary."""
        self._stopped = True
        self._flush_event.set()
        self._thread.join(timeout=60)
        # Safety net: flush anything left if the thread exited early
        self._do_flush()

    # ---- Internal ----

    def _flush_loop(self) -> None:
        """Daemon thread: flush on signal or every _FLUSH_INTERVAL seconds."""
        while not self._stopped:
            self._flush_event.wait(timeout=self._FLUSH_INTERVAL)
            self._flush_event.clear()
            self._do_flush()
        # One final flush after stop is requested
        self._do_flush()

    def _do_flush(self) -> None:
        """Pop up to _BATCH_SIZE items and send them to batchCreate."""
        with self._lock:
            if not self._buffer:
                return
            batch = self._buffer[: self._BATCH_SIZE]
            self._buffer = self._buffer[self._BATCH_SIZE :]

        # Build the request body
        new_media_items = []
        for item in batch:
            media_item: Dict = {
                "simpleMediaItem": {
                    "uploadToken": item["upload_token"],
                    "fileName": item["filename"],
                }
            }
            if item["description"]:
                media_item["description"] = item["description"][:1000]
            new_media_items.append(media_item)

        token = self._get_token()

        for attempt in range(MAX_RETRIES):
            resp = _get_session().post(
                PHOTOS_BATCH_CREATE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-type": "application/json",
                },
                json={"newMediaItems": new_media_items},
            )

            if resp.status_code == 429:
                wait = RETRY_BASE_DELAY * (2 ** attempt)
                _tlog(
                    f"  Rate-limited (batch create, {len(batch)} items) "
                    f"— retrying in {wait:.0f} s …"
                )
                time.sleep(wait)
                continue

            if resp.status_code != 200:
                _tlog(
                    f"  batchCreate failed (HTTP {resp.status_code}): "
                    f"{resp.text[:120]}"
                )
                for _ in batch:
                    self._state.record_failure()
                return

            results = resp.json().get("newMediaItemResults", [])
            for i, result in enumerate(results):
                if i >= len(batch):
                    break
                item = batch[i]
                status = result.get("status", {})
                ok = status.get("code", -1) == 0 or "success" in status.get(
                    "message", ""
                ).lower()
                if ok:
                    self._state.record_success(item["file_id"], item["file_hash"])
                    if self._photos_cache:
                        self._photos_cache.add(item["filename"])
                    _tlog(
                        f"{item['prefix']} OK  {item['filename']}"
                        f"  ({item['size_mb']:.1f} MB)"
                    )
                else:
                    _tlog(
                        f"{item['prefix']} FAIL (create item)  {item['filename']}"
                        f": {status.get('message', '')}"
                    )
                    self._state.record_failure()
            return

        # All retries exhausted
        for _ in batch:
            self._state.record_failure()


# ============================================================
# Per-file worker  (runs in a thread-pool thread)
# ============================================================

def process_one_file(
    idx: int,
    total: int,
    file: Dict,
    creds: Credentials,
    state: SyncState,
    photos_cache: Optional[PhotosFilenameCache],
    dedup_mode: str,
    batch_collector: Optional[BatchCollector] = None,
) -> str:
    """
    Download one file from Drive and upload it to Google Photos.

    Dedup logic (controlled by *dedup_mode*):
      "none"          — always upload
      "filename"      — skip if *photos_cache* contains the filename
      "hash"          — download first, compute SHA-256, skip if hash was
                        previously uploaded by this script
      "filename+hash" — skip if EITHER the filename cache OR the hash set
                        contains a match

    Returns one of: "uploaded" | "skipped" | "failed"
    """
    # Honour shutdown signal — return immediately so the thread-pool drains fast
    if state.shutdown.is_set():
        return "skipped"

    file_id: str = file["id"]
    filename: str = file["name"]
    size_mb: float = int(file.get("size", 0)) / (1024 ** 2)
    prefix = f"[{idx}/{total}]"

    # ------------------------------------------------------------------
    # Pre-download dedup: filename check  (cheap — no network download)
    # ------------------------------------------------------------------
    if dedup_mode in ("filename", "filename+hash") and photos_cache:
        if photos_cache.contains(filename):
            _tlog(f"{prefix} SKIP (filename match)  {filename}")
            state.record_skip()
            return "skipped"

    # ------------------------------------------------------------------
    # Download file from Drive
    # ------------------------------------------------------------------
    try:
        drive = _get_drive_service(creds)
        data: bytes = download_file(drive, file_id)
    except Exception as exc:
        _tlog(f"{prefix} FAIL (download)  {filename}: {exc}")
        state.record_failure()
        return "failed"

    # ------------------------------------------------------------------
    # Post-download dedup: content hash check
    # ------------------------------------------------------------------
    file_hash: Optional[str] = None
    if dedup_mode in ("hash", "filename+hash"):
        file_hash = hashlib.sha256(data).hexdigest()
        if state.is_uploaded_hash(file_hash):
            _tlog(f"{prefix} SKIP (hash match)  {filename}")
            state.record_skip()
            return "skipped"

    # ------------------------------------------------------------------
    # Upload to Google Photos
    # ------------------------------------------------------------------
    _refresh_token_if_needed(creds)
    token: str = creds.token

    # Build a description that preserves original Drive metadata.
    # Google Photos itself reads EXIF data from the file for dates shown
    # in the timeline; this description is a human-readable fallback.
    desc_parts = [f"Drive ID: {file_id}"]
    if file.get("createdTime"):
        desc_parts.append(f"Created: {file['createdTime']}")
    if file.get("modifiedTime"):
        desc_parts.append(f"Modified: {file['modifiedTime']}")
    description = " | ".join(desc_parts)

    upload_token = photos_upload_bytes(token, data, filename)
    if not upload_token:
        _tlog(f"{prefix} FAIL (upload bytes)  {filename}")
        state.record_failure()
        return "failed"

    if batch_collector is not None:
        # Hand off to the batch collector — actual success/failure is recorded
        # asynchronously once the batch fires.
        batch_collector.enqueue(
            upload_token, filename, description, file_id, file_hash, size_mb, prefix
        )
        return "uploaded"

    ok = photos_create_item(token, upload_token, filename, description)
    if ok:
        state.record_success(file_id, file_hash)
        if photos_cache:
            photos_cache.add(filename)
        _tlog(f"{prefix} OK  {filename}  ({size_mb:.1f} MB)")
        return "uploaded"
    else:
        _tlog(f"{prefix} FAIL (create item)  {filename}")
        state.record_failure()
        return "failed"


# ============================================================
# Main
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync images/videos from Google Drive to Google Photos",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Dedup modes
-----------
  none          Upload everything (fastest; Photos may contain duplicates)
  filename      Skip files whose filename already exists in Photos     [default]
  hash          Download first, compute SHA-256, skip if same content
                was uploaded before by this script
  filename+hash Skip if EITHER filename OR hash matches (most thorough)

Examples
--------
  %(prog)s                               # interactive folder picker
  %(prog)s --folder DRIVE_FOLDER_ID     # specific folder
  %(prog)s --all --workers 8            # entire Drive, 8 parallel threads
  %(prog)s --dedup-mode filename+hash   # high-confidence dedup
  %(prog)s --refresh-cache              # force Photos library rescan
  %(prog)s --dry-run --all              # preview without uploading
  %(prog)s --skip-dedup --since 2024-06-01
""",
    )

    # --- Folder / scope selection ---
    scope_group = parser.add_mutually_exclusive_group()
    scope_group.add_argument(
        "--folder", metavar="ID",
        help="Drive folder ID to sync (skip interactive browser)",
    )
    scope_group.add_argument(
        "--all", action="store_true",
        help="Sync ALL supported media in Drive (no folder filter)",
    )

    parser.add_argument(
        "--since", metavar="YYYY-MM-DD",
        help="Only include files modified after this date",
    )

    # --- Upload behaviour ---
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS, metavar="N",
        help=f"Parallel upload threads (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--limit", type=int, metavar="N",
        help="Process at most N files (useful for testing)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List what would be uploaded without actually uploading",
    )

    # --- Deduplication ---
    parser.add_argument(
        "--dedup-mode",
        default="filename",
        choices=["none", "filename", "hash", "filename+hash"],
        help="Deduplication strategy (default: filename)",
    )
    parser.add_argument(
        "--skip-dedup", action="store_true",
        help="Skip all dedup checks — alias for --dedup-mode none",
    )
    parser.add_argument(
        "--refresh-cache", action="store_true",
        help="Force a full rescan of your Photos library (rebuilds cache)",
    )

    # --- Progress ---
    parser.add_argument(
        "--save-every", type=int, default=DEFAULT_SAVE_EVERY, metavar="N",
        help=f"Save progress to disk every N files (default: {DEFAULT_SAVE_EVERY})",
    )

    args = parser.parse_args()

    # --skip-dedup is a convenience alias
    if args.skip_dedup:
        args.dedup_mode = "none"

    # ----------------------------------------------------------------
    # Authentication
    # ----------------------------------------------------------------
    print("Authenticating with Google …")
    creds = authenticate()
    drive = build("drive", "v3", credentials=creds)

    # ----------------------------------------------------------------
    # Folder / scope selection
    # ----------------------------------------------------------------
    if args.all:
        folder_specs: List[Tuple[Optional[str], str]] = [(None, "entire Drive")]
        print("Mode: sync ALL media in Drive")
    elif args.folder:
        folder_specs = [(args.folder, args.folder)]
        print(f"Mode: sync folder {args.folder}")
    else:
        print("\nBrowse your Google Drive to pick folder(s) to sync:")
        raw = browse_and_select_multiple(drive)
        folder_specs = [(fid, fname) for fid, fname in raw]
        print(f"\nWill sync: {', '.join(n for _, n in folder_specs)}")

    # ----------------------------------------------------------------
    # Collect file list from Drive
    # ----------------------------------------------------------------
    all_files: List[Dict] = []
    for fid, fname in folder_specs:
        print(f"\nListing media in: {fname}")
        files = list_drive_media(drive, fid, args.since)
        print(f"  {len(files):,} file(s) found")
        all_files.extend(files)

    # Deduplicate across folder selections (handles overlapping trees)
    seen_ids: Set[str] = set()
    unique_files: List[Dict] = []
    for f in all_files:
        if f["id"] not in seen_ids:
            seen_ids.add(f["id"])
            unique_files.append(f)
    all_files = unique_files

    print(f"\nTotal media files in scope: {len(all_files):,}")
    if not all_files:
        print("Nothing to sync.")
        return

    # ----------------------------------------------------------------
    # Filter files already recorded in uploaded_ids.json
    # ----------------------------------------------------------------
    state = SyncState(save_every=args.save_every)
    pending = [f for f in all_files if not state.is_uploaded_id(f["id"])]
    already_done = len(all_files) - len(pending)
    print(f"Already uploaded (Drive ID match): {already_done:,}")
    print(f"Pending: {len(pending):,}")

    if args.limit:
        pending = pending[: args.limit]
        print(f"Capped at {args.limit} files (--limit)")

    if not pending:
        print("Nothing new to upload.")
        return

    # ----------------------------------------------------------------
    # Load Photos filename cache (if needed for chosen dedup mode)
    # ----------------------------------------------------------------
    photos_cache: Optional[PhotosFilenameCache] = None
    if args.dedup_mode in ("filename", "filename+hash"):
        photos_cache = PhotosFilenameCache(lambda: creds.token)
        photos_cache.ensure_loaded(force_refresh=args.refresh_cache)

    # ----------------------------------------------------------------
    # Dry run — show what would happen, then exit
    # ----------------------------------------------------------------
    if args.dry_run:
        print(f"\n{'=' * 62}")
        print(f"  DRY RUN — {len(pending):,} pending file(s)")
        print("=" * 62)

        would_upload = 0
        would_skip = 0
        for f in pending:
            size_mb = int(f.get("size", 0)) / (1024 ** 2)
            skip_reason = ""

            if photos_cache and photos_cache.contains(f["name"]):
                skip_reason = "  [would skip: filename match]"
                would_skip += 1
            else:
                would_upload += 1

            print(
                f"  {f['name']}  ({size_mb:.1f} MB)"
                f"  [{f['mimeType']}]{skip_reason}"
            )

        print(f"\n  Would upload : {would_upload:,}")
        print(f"  Would skip   : {would_skip:,}")
        print(
            "  Note: hash dedup can only be evaluated after downloading "
            "each file; those are shown as 'would upload' above."
        )
        return

    # ----------------------------------------------------------------
    # Upload confirmation
    # ----------------------------------------------------------------
    total = len(pending)
    confirm = input(
        f"\nUpload {total:,} file(s) to Google Photos "
        f"using {args.workers} thread(s)? [y/N]: "
    ).strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return

    # ----------------------------------------------------------------
    # Ctrl+C handler — set shutdown event, let running tasks finish
    # ----------------------------------------------------------------
    def _handle_interrupt(sig, frame):  # noqa: ANN001
        if not state.shutdown.is_set():
            print(
                "\n\nInterrupt received — finishing current uploads "
                "and saving progress …"
            )
            state.shutdown.set()

    signal.signal(signal.SIGINT, _handle_interrupt)

    # ----------------------------------------------------------------
    # Thread pool execution
    # ----------------------------------------------------------------
    print(f"\nStarting sync with {args.workers} worker thread(s) …\n")
    start_time = time.monotonic()

    # BatchCollector coalesces individual batchCreate calls into groups of 50,
    # reducing API round-trips by up to 50×.
    batch_collector = BatchCollector(lambda: creds.token, state, photos_cache)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        # Submit all tasks upfront.  ThreadPoolExecutor queues them internally
        # and dispatches up to max_workers at a time — no manual chunking needed.
        futures = {
            executor.submit(
                process_one_file,
                idx,
                total,
                file,
                creds,
                state,
                photos_cache,
                args.dedup_mode,
                batch_collector,
            ): file["name"]
            for idx, file in enumerate(pending, 1)
        }

        # Consume results as they complete
        for future in as_completed(futures):
            if state.shutdown.is_set():
                # Cancel futures that haven't started yet
                for f in futures:
                    f.cancel()
                break

            try:
                future.result()
            except Exception as exc:
                name = futures[future]
                _tlog(f"  Unhandled exception for {name}: {exc}")
                state.record_failure()

    # Flush any upload tokens that haven't been sent yet
    batch_collector.drain()

    # ----------------------------------------------------------------
    # Final save and summary
    # ----------------------------------------------------------------
    state.flush()

    elapsed = time.monotonic() - start_time
    mins, secs = divmod(int(elapsed), 60)

    print(f"\n{'=' * 62}")
    print(f"  Finished in {mins}m {secs}s")
    print(f"  Uploaded : {state.success_count:,}")
    print(f"  Skipped  : {state.skip_count:,}")
    print(f"  Failed   : {state.fail_count:,}")
    if state.fail_count:
        print(
            "  Tip: re-run the same command to retry failed files "
            "(already-uploaded ones are skipped automatically)."
        )
    print("=" * 62)


if __name__ == "__main__":
    main()
