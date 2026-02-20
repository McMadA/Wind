"""Google Drive client – authenticates via OAuth 2.0, lists, downloads, and uploads files."""

from __future__ import annotations

import hashlib
import io
import logging
import os
from collections.abc import Callable, Generator
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
TOKEN_FILE = "token.json"


class GDriveClient:
    """Wraps the Google Drive v3 API for folder creation, file listing, download, and upload."""

    def __init__(self, credentials_file: str = "credentials.json"):
        self._creds = self._authenticate(credentials_file)
        self._service = build("drive", "v3", credentials=self._creds)
        self._folder_cache: dict[str, str] = {}

    # ── authentication ──────────────────────────────────────────────

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
        results = self._service.files().list(q=query, fields="files(id)").execute()
        files = results.get("files", [])
        if files:
            folder_id = files[0]["id"]
        else:
            meta = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }
            folder = self._service.files().create(body=meta, fields="id").execute()
            folder_id = folder["id"]
            logger.info("Created Google Drive folder: %s", name)

        self._folder_cache[cache_key] = folder_id
        return folder_id

    def ensure_path(self, relative_dir: str, root_folder_id: str) -> str:
        """Ensure all intermediate folders for *relative_dir* exist. Returns the deepest folder ID."""
        parts = [p for p in relative_dir.split("/") if p]
        current = root_folder_id
        for part in parts:
            current = self._ensure_folder(part, current)
        return current

    # ── listing ─────────────────────────────────────────────────────

    def list_files(
        self,
        folder_id: str = "root",
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> Generator[dict, None, None]:
        """Recursively list all files under *folder_id*.

        Yields file metadata dicts with keys: id, name, path, size, md5.
        Skips Google Workspace files (Docs, Sheets, etc.) which have no binary content.
        *progress_callback(file_count, current_folder)* is called for each discovered file.
        """
        return self._walk_drive(folder_id, "/", [0], progress_callback)

    def _walk_drive(
        self,
        folder_id: str,
        path_prefix: str,
        counter: list[int],
        progress_callback: Callable[[int, str], None] | None = None,
    ) -> Generator[dict, None, None]:
        logger.info("  Scanning: %s", path_prefix)
        page_token = None
        while True:
            safe_id = self._escape_query(folder_id)
            query = f"'{safe_id}' in parents and trashed=false"
            resp = self._service.files().list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, size, md5Checksum)",
                pageSize=1000,
                pageToken=page_token,
            ).execute()
            for item in resp.get("files", []):
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    child_path = f"{path_prefix.rstrip('/')}/{item['name']}"
                    yield from self._walk_drive(
                        item["id"], child_path, counter, progress_callback
                    )
                elif not item["mimeType"].startswith("application/vnd.google-apps."):
                    file_meta = {
                        "id": item["id"],
                        "name": item["name"],
                        "path": f"{path_prefix.rstrip('/')}/{item['name']}",
                        "size": int(item.get("size", 0)),
                        "md5": item.get("md5Checksum"),
                    }
                    counter[0] += 1
                    yield file_meta
                    if progress_callback:
                        progress_callback(counter[0], path_prefix)
                else:
                    logger.debug(
                        "Skipping Google Workspace file: %s (type: %s)",
                        item["name"], item["mimeType"],
                    )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    # ── download ────────────────────────────────────────────────────

    def download_file(
        self,
        file_meta: dict,
        dest_dir: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download a file from Google Drive to *dest_dir*, preserving relative path. Returns the local Path."""
        relative = file_meta["path"].lstrip("/")
        local_path = Path(dest_dir) / relative
        local_path.parent.mkdir(parents=True, exist_ok=True)

        request = self._service.files().get_media(fileId=file_meta["id"])
        total_size = file_meta.get("size", 0)

        with open(local_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                if status and progress_callback:
                    progress_callback(int(status.resumable_progress), total_size)

        if progress_callback:
            actual_size = total_size or os.path.getsize(local_path)
            progress_callback(actual_size, actual_size)

        return local_path

    # ── query ────────────────────────────────────────────────────────

    def find_file(self, name: str, parent_folder_id: str) -> dict | None:
        """Return metadata of an existing file with *name* in *parent_folder_id*, or None."""
        safe_name = self._escape_query(name)
        query = (
            f"name='{safe_name}' and '{parent_folder_id}' in parents "
            f"and mimeType!='application/vnd.google-apps.folder' and trashed=false"
        )
        results = (
            self._service.files()
            .list(q=query, fields="files(id,name,md5Checksum,size)")
            .execute()
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
            status, response = request.next_chunk()
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
            status, response = request.next_chunk()
            if status and progress_callback:
                progress_callback(int(status.resumable_progress), int(status.total_size))
        if progress_callback and response:
            total = os.path.getsize(local_path)
            progress_callback(total, total)
        logger.debug("Overwritten %s  (id=%s)", local_path.name, response["id"])
        return response

    # ── verification ────────────────────────────────────────────────

    def verify_integrity(self, local_path: Path, uploaded_meta: dict) -> bool:
        """Compare local MD5 against the MD5 Google Drive computed on upload."""
        gdrive_md5 = uploaded_meta.get("md5Checksum")
        if not gdrive_md5:
            gdrive_md5 = self.get_file_md5(uploaded_meta["id"])
        if not gdrive_md5:
            logger.warning(
                "Google Drive did not return an MD5 for %s \u2013 skipping verification.",
                local_path.name,
            )
            return True

        local_md5 = self.compute_local_md5(local_path)
        return local_md5 == gdrive_md5

    def get_file_md5(self, file_id: str) -> str | None:
        """Return the md5Checksum reported by Google Drive for *file_id*."""
        meta = self._service.files().get(fileId=file_id, fields="md5Checksum").execute()
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
