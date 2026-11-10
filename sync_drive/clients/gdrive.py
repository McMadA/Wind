"""Google Drive client – authenticates via OAuth 2.0, lists (with fast bulk scan + caching), downloads, and uploads files with automatic connection-reset retry."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Generator
import hashlib
import json
import logging
import os
from pathlib import Path
import ssl
import threading
import time

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from sync_drive.metadata import (
    extract_from_gdrive_meta,
    matches_file_filter,
    parse_datetime_any,
    set_file_timestamp,
)

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
TOKEN_FILE = "token.json"
DEFAULT_SCAN_CACHE_FILE = ".gdrive_scan_cache.json"
MAX_API_RETRIES = 6


class GDriveClient:
    """Wraps the Google Drive v3 API with automatic socket-reset recovery and fast bulk tree scanning."""

    def __init__(
        self,
        credentials_file: str = "credentials.json",
        scan_cache_file: str = DEFAULT_SCAN_CACHE_FILE,
        refresh_cache: bool = False,
    ):
        self._credentials_file = credentials_file
        self._scan_cache_file = scan_cache_file
        self._refresh_cache = refresh_cache
        self._creds_lock = threading.Lock()
        self._thread_local = threading.local()
        self._creds = self._authenticate(credentials_file)
        self._service = self._build_service()
        self._folder_cache: dict[str, str] = {}
        self._scan_memory_cache: dict[str, list[dict]] = {}

    # ── authentication & resilient connection management ────────────

    @staticmethod
    def _authenticate(credentials_file: str) -> Credentials:
        creds = None
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "w") as token:
                token.write(creds.to_json())
        return creds

    def _get_token(self) -> str:
        with self._creds_lock:
            if not self._creds.valid and self._creds.refresh_token:
                self._creds.refresh(Request())
            return self._creds.token

    def _get_session(self) -> requests.Session:
        """Return a thread-local requests.Session with keep-alive connection pooling."""
        if not hasattr(self._thread_local, "session"):
            self._thread_local.session = requests.Session()
        return self._thread_local.session

    def _build_service(self):
        with self._creds_lock:
            if not self._creds.valid and self._creds.refresh_token:
                try:
                    self._creds.refresh(Request())
                except Exception:
                    pass
        return build("drive", "v3", credentials=self._creds, cache_discovery=False)

    def _reconnect(self) -> None:
        """Rebuild the Drive API service and thread-local session with fresh TCP/TLS sockets."""
        with self._creds_lock:
            try:
                if self._creds and self._creds.expired and self._creds.refresh_token:
                    self._creds.refresh(Request())
            except Exception:
                pass
        self._service = self._build_service()
        self._thread_local.session = requests.Session()

    def _execute(self, request_builder: Callable[[], any]):
        """Execute a Google Drive API request with automatic reconnect & exponential backoff on socket resets."""
        last_exc = None
        for attempt in range(1, MAX_API_RETRIES + 1):
            try:
                req = request_builder()
                return req.execute(num_retries=2)
            except HttpError as exc:
                status = getattr(exc.resp, "status", 0)
                if status in (429, 500, 502, 503, 504) and attempt < MAX_API_RETRIES:
                    delay = min(60.0, 2.0 ** attempt)
                    logger.warning(
                        "Google Drive HTTP %s (attempt %d/%d); retrying in %.0fs...",
                        status,
                        attempt,
                        MAX_API_RETRIES,
                        delay,
                    )
                    time.sleep(delay)
                    self._reconnect()
                    last_exc = exc
                    continue
                raise
            except (
                ConnectionError,
                ConnectionResetError,
                BrokenPipeError,
                TimeoutError,
                ssl.SSLError,
                OSError,
            ) as exc:
                last_exc = exc
                if attempt >= MAX_API_RETRIES:
                    raise
                delay = min(30.0, 1.5 ** attempt)
                logger.warning(
                    "Google Drive connection interrupted (%s) [attempt %d/%d]; reconnecting in %.1fs...",
                    exc,
                    attempt,
                    MAX_API_RETRIES,
                    delay,
                )
                time.sleep(delay)
                self._reconnect()
        if last_exc:
            raise last_exc

    # ── folder helpers ──────────────────────────────────────────────

    @staticmethod
    def _escape_query(value: str) -> str:
        """Escape a value for use in a Google Drive API query string."""
        return value.replace("\\", "\\\\").replace("'", "\\'")

    def _ensure_folder(self, name: str, parent_id: str) -> str:
        """Return the ID of *name* inside *parent_id*, creating it if needed."""
        cache_key = f"{parent_id}/{name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        safe_name = self._escape_query(name)
        query = (
            f"name='{safe_name}' and '{parent_id}' in parents "
            f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        results = self._execute(
            lambda: self._service.files().list(q=query, fields="files(id)")
        )
        files = results.get("files", [])
        if files:
            folder_id = files[0]["id"]
        else:
            meta = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }
            folder = self._execute(
                lambda: self._service.files().create(body=meta, fields="id")
            )
            folder_id = folder["id"]
            logger.info("Created Google Drive folder: %s", name)

        self._folder_cache[cache_key] = folder_id
        return folder_id

    def ensure_path(self, relative_dir: str, root_folder_id: str = "root") -> str:
        """Ensure all intermediate folders for *relative_dir* exist. Returns the deepest folder ID."""
        if root_folder_id in ("/", ""):
            root_folder_id = "root"
        parts = [p for p in relative_dir.split("/") if p and p != "."]
        current = root_folder_id
        for part in parts:
            current = self._ensure_folder(part, current)
        return current

    # ── fast bulk listing with caching ──────────────────────────────

    def list_files(
        self,
        folder_id: str = "root",
        progress_callback: Callable[[int, str], None] | None = None,
        file_filter: str = "all",
    ) -> Generator[dict, None, None]:
        """List all files under *folder_id*, filtered by *file_filter* ('all', 'media', 'documents').

        Uses bulk API pagination + local scan cache so scanning 20,000+ files across 1,700+ folders
        takes ~15 seconds on first run and <0.1s on subsequent phases/runs.
        """
        if folder_id in ("/", ""):
            folder_id = "root"

        raw_items = self._get_or_build_scan_cache(folder_id, progress_callback=progress_callback)
        count = 0
        for item in raw_items:
            mime_type = item.get("mimeType", "")
            if mime_type.startswith("application/vnd.google-apps."):
                continue
            if not matches_file_filter(item["name"], mime_type, file_filter):
                continue

            photo_meta = extract_from_gdrive_meta(item)
            file_meta = {
                "id": item["id"],
                "name": item["name"],
                "path": item["path"],
                "size": int(item.get("size", 0)),
                "md5": (item.get("md5Checksum") or "").lower() or None,
                "mimeType": mime_type,
                "createdTime": item.get("createdTime"),
                "modifiedTime": item.get("modifiedTime"),
                "metadata": photo_meta,
                "source": "gdrive",
            }
            count += 1
            yield file_meta
            if progress_callback:
                parent_dir = os.path.dirname(item["path"]) or "/"
                progress_callback(count, parent_dir)

    def _get_or_build_scan_cache(
        self,
        folder_id: str,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> list[dict]:
        # 1. Check in-memory cache first (so Phase 1 and Phase 2 share the exact same scan in 0ms)
        if folder_id in self._scan_memory_cache and not self._refresh_cache:
            cached = self._scan_memory_cache[folder_id]
            logger.info(
                "Using in-memory Google Drive scan cache (%d total files).",
                len(cached),
            )
            return cached

        # 2. Check on-disk cache (.gdrive_scan_cache.json)
        if not self._refresh_cache and os.path.exists(self._scan_cache_file):
            try:
                with open(self._scan_cache_file, "r", encoding="utf-8") as fh:
                    disk_data = json.load(fh)
                if disk_data.get("folder_id") == folder_id and disk_data.get("items"):
                    items = disk_data["items"]
                    self._scan_memory_cache[folder_id] = items
                    logger.info(
                        "Loaded %d Google Drive files from local scan cache (%s). "
                        "(Use --refresh-cache to force a fresh Google Drive scan).",
                        len(items),
                        self._scan_cache_file,
                    )
                    return items
            except Exception as exc:
                logger.debug("Could not load Drive scan cache: %s", exc)

        # 3. Perform fast bulk scan via Google Drive API
        items = self._bulk_scan_drive(folder_id, progress_callback=progress_callback)
        self._scan_memory_cache[folder_id] = items
        self._refresh_cache = False

        try:
            tmp_file = f"{self._scan_cache_file}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "folder_id": folder_id,
                        "cached_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "items": items,
                    },
                    fh,
                )
            os.replace(tmp_file, self._scan_cache_file)
        except Exception as exc:
            logger.debug("Could not save Drive scan cache: %s", exc)

        return items

    def _bulk_scan_drive(
        self,
        folder_id: str,
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> list[dict]:
        """Bulk-fetch all folders and files in minimal API calls instead of 1 call per folder."""
        logger.info("Fast-scanning Google Drive folder tree (starting at '%s')...", folder_id)

        # Step 1: Resolve canonical root folder ID
        root_meta = self._execute(
            lambda: self._service.files().get(fileId=folder_id, fields="id,name")
        )
        canonical_root_id = root_meta["id"]

        # Step 2: Fetch all folders in bulk (1000 per page -> ~2 API calls for 1,750 folders!)
        children_by_parent: dict[str, list[tuple[str, str]]] = {}
        page_token = None
        total_folders = 0
        while True:
            pt = page_token
            resp = self._execute(
                lambda: self._service.files().list(
                    q="mimeType='application/vnd.google-apps.folder' and trashed=false",
                    fields="nextPageToken, files(id, name, parents)",
                    pageSize=1000,
                    pageToken=pt,
                )
            )
            for f in resp.get("files", []):
                fid = f["id"]
                fname = f["name"]
                for pid in f.get("parents") or []:
                    children_by_parent.setdefault(pid, []).append((fid, fname))
                total_folders += 1
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        # Build path map for all folders descending from canonical_root_id
        folder_paths: dict[str, str] = {canonical_root_id: "/"}
        queue = deque([canonical_root_id])
        while queue:
            curr_id = queue.popleft()
            curr_path = folder_paths[curr_id]
            for child_id, child_name in sorted(children_by_parent.get(curr_id, []), key=lambda x: x[1]):
                if child_id not in folder_paths:
                    folder_paths[child_id] = f"{curr_path.rstrip('/')}/{child_name}"
                    queue.append(child_id)

        logger.info(
            "Mapped %d folder(s) under target root (out of %d total Drive folders). Fetching file metadata...",
            len(folder_paths),
            total_folders,
        )

        # Step 3: Fetch all non-folder files in bulk (1000 per page -> ~15-25 API calls total!)
        all_items: list[dict] = []
        page_token = None
        while True:
            pt = page_token
            resp = self._execute(
                lambda: self._service.files().list(
                    q="mimeType!='application/vnd.google-apps.folder' and trashed=false",
                    fields=(
                        "nextPageToken, files("
                        "id, name, parents, mimeType, size, md5Checksum, "
                        "createdTime, modifiedTime, description, starred, "
                        "imageMediaMetadata(time, location), "
                        "videoMediaMetadata(durationMillis)"
                        ")"
                    ),
                    pageSize=1000,
                    pageToken=pt,
                )
            )
            for item in resp.get("files", []):
                mime_type = item.get("mimeType", "")
                if mime_type.startswith("application/vnd.google-apps."):
                    continue
                parents = item.get("parents") or []
                matched_parent_path = None
                for pid in parents:
                    if pid in folder_paths:
                        matched_parent_path = folder_paths[pid]
                        break
                if matched_parent_path is None:
                    continue

                item["path"] = f"{matched_parent_path.rstrip('/')}/{item['name']}"
                all_items.append(item)
                if progress_callback and len(all_items) % 250 == 0:
                    progress_callback(len(all_items), matched_parent_path)

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        # Sort deterministically by path
        all_items.sort(key=lambda x: x["path"])
        logger.info("Google Drive bulk scan complete: %d file(s) discovered.", len(all_items))
        return all_items

    # ── download (with automatic retry on socket reset) ─────────────

    def download_file(
        self,
        file_meta: dict,
        dest_dir: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download a file from Google Drive using thread-safe keep-alive HTTP streaming with retry."""
        relative = file_meta["path"].lstrip("/")
        local_path = Path(dest_dir) / relative
        local_path.parent.mkdir(parents=True, exist_ok=True)
        total_size = file_meta.get("size", 0)
        url = f"https://www.googleapis.com/drive/v3/files/{file_meta['id']}?alt=media&acknowledgeAbuse=true"

        last_exc = None
        for attempt in range(1, MAX_API_RETRIES + 1):
            try:
                session = self._get_session()
                headers = {"Authorization": f"Bearer {self._get_token()}"}
                with session.get(url, headers=headers, stream=True, timeout=60) as resp:
                    if resp.status_code == 401:
                        with self._creds_lock:
                            self._creds.refresh(Request())
                        headers = {"Authorization": f"Bearer {self._get_token()}"}
                        resp = session.get(url, headers=headers, stream=True, timeout=60)
                    if resp.status_code in (403, 429, 500, 502, 503, 504):
                        raise ConnectionError(f"HTTP {resp.status_code}: {resp.text[:120]}")
                    resp.raise_for_status()

                    downloaded = 0
                    with open(local_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=262144):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                if progress_callback:
                                    progress_callback(downloaded, total_size or downloaded)

                    if total_size and int(total_size) > 0 and downloaded < int(total_size):
                        raise ConnectionError(
                            f"Incomplete download ({downloaded}/{total_size} bytes)"
                        )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt >= MAX_API_RETRIES:
                    raise
                delay = min(30.0, 1.5 ** attempt)
                logger.warning(
                    "Download interrupted for %s (%s) [attempt %d/%d]; retrying in %.1fs...",
                    file_meta["name"],
                    exc,
                    attempt,
                    MAX_API_RETRIES,
                    delay,
                )
                time.sleep(delay)
                self._thread_local.session = requests.Session()

        if last_exc:
            raise last_exc

        if progress_callback:
            actual_size = total_size or os.path.getsize(local_path)
            progress_callback(actual_size, actual_size)

        # Preserve original capture/modified timestamp on local file
        photo_meta = file_meta.get("metadata")
        ts_dt = (
            (photo_meta.taken_at if photo_meta else None)
            or parse_datetime_any(file_meta.get("modifiedTime"))
            or parse_datetime_any(file_meta.get("createdTime"))
        )
        if ts_dt:
            set_file_timestamp(local_path, ts_dt)

        return local_path

    # ── query ────────────────────────────────────────────────────────

    def find_file(self, name: str, parent_folder_id: str) -> dict | None:
        """Return metadata of an existing file with *name* in *parent_folder_id*, or None."""
        safe_name = self._escape_query(name)
        query = (
            f"name='{safe_name}' and '{parent_folder_id}' in parents "
            f"and mimeType!='application/vnd.google-apps.folder' and trashed=false"
        )
        results = self._execute(
            lambda: self._service.files().list(
                q=query, fields="files(id,name,md5Checksum,size)"
            )
        )
        files = results.get("files", [])
        return files[0] if files else None

    # ── upload ──────────────────────────────────────────────────────

    def upload_file(
        self,
        local_path: Path,
        parent_folder_id: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Upload *local_path* into *parent_folder_id*. Returns the Google Drive file metadata."""
        media = MediaFileUpload(str(local_path), resumable=True)
        meta = {"name": local_path.name, "parents": [parent_folder_id]}
        request = self._service.files().create(
            body=meta, media_body=media, fields="id,name,md5Checksum,size"
        )
        response = None
        while response is None:
            status, response = request.next_chunk(num_retries=3)
            if status and progress_callback:
                progress_callback(int(status.resumable_progress), int(status.total_size))
        if progress_callback and response:
            total = os.path.getsize(local_path)
            progress_callback(total, total)
        logger.debug("Uploaded %s  (id=%s)", local_path.name, response["id"])
        return response

    def update_file(
        self,
        file_id: str,
        local_path: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Overwrite an existing Google Drive file with new content."""
        media = MediaFileUpload(str(local_path), resumable=True)
        request = self._service.files().update(
            fileId=file_id, media_body=media, fields="id,name,md5Checksum,size"
        )
        response = None
        while response is None:
            status, response = request.next_chunk(num_retries=3)
            if status and progress_callback:
                progress_callback(int(status.resumable_progress), int(status.total_size))
        if progress_callback and response:
            total = os.path.getsize(local_path)
            progress_callback(total, total)
        logger.debug("Overwritten %s  (id=%s)", local_path.name, response["id"])
        return response

    def trash_file(self, file_id: str) -> None:
        """Move a file to Google Drive Trash (soft delete with 30-day recovery window). Thread-safe via requests."""
        token = self._get_token()
        session = self._get_session()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
        resp = session.patch(url, headers=headers, json={"trashed": True}, timeout=30)
        if resp.status_code == 404:
            logger.info("File %s already deleted/trashed", file_id)
            return
        resp.raise_for_status()
        logger.info("Moved Google Drive file to Trash: %s", file_id)

    def delete_file(self, file_id: str) -> None:
        """Delete a file from Google Drive."""
        self._execute(lambda: self._service.files().delete(fileId=file_id))
        logger.info("Deleted Google Drive file: %s", file_id)

    # ── verification ────────────────────────────────────────────────

    def verify_integrity(self, local_path: Path, uploaded_meta: dict) -> bool:
        """Compare local MD5 against the MD5 Google Drive computed on upload."""
        gdrive_md5 = uploaded_meta.get("md5Checksum")
        if not gdrive_md5:
            gdrive_md5 = self.get_file_md5(uploaded_meta["id"])
        if not gdrive_md5:
            logger.warning(
                "Google Drive did not return an MD5 for %s – skipping verification.",
                local_path.name,
            )
            return True

        local_md5 = self.compute_local_md5(local_path)
        return local_md5 == gdrive_md5

    def get_file_md5(self, file_id: str) -> str | None:
        """Return the md5Checksum reported by Google Drive for *file_id*."""
        meta = self._execute(
            lambda: self._service.files().get(fileId=file_id, fields="md5Checksum")
        )
        return meta.get("md5Checksum")

    @staticmethod
    def compute_local_md5(filepath: Path) -> str:
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            for block in iter(lambda: f.read(8192), b""):
                h.update(block)
        return h.hexdigest()

    @staticmethod
    def compute_sha256(filepath: Path) -> str:
        """Compute SHA256 hash of a local file (uppercase hex, matching OneDrive format)."""
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for block in iter(lambda: f.read(8192), b""):
                h.update(block)
        return h.hexdigest().upper()
