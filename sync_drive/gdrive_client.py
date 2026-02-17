"""Google Drive client – authenticates via OAuth 2.0 and uploads files."""

import hashlib
import logging
import os
from collections.abc import Callable
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_FILE = "token.json"


class GDriveClient:
    """Wraps the Google Drive v3 API for folder creation and file upload."""

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

    def _ensure_folder(self, name: str, parent_id: str) -> str:
        """Return the ID of *name* inside *parent_id*, creating it if needed."""
        cache_key = f"{parent_id}/{name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        query = (
            f"name='{name}' and '{parent_id}' in parents "
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

    # ── query ────────────────────────────────────────────────────────

    def find_file(self, name: str, parent_folder_id: str) -> dict | None:
        """Return metadata of an existing file with *name* in *parent_folder_id*, or None."""
        query = (
            f"name='{name}' and '{parent_folder_id}' in parents "
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
