"""Google Photos client – authenticates via OAuth 2.0, uploads files to Google Photos library."""

from __future__ import annotations

import logging
import os
import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Optional, Set, Dict, List

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.readonly",
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
]

# Google Photos API endpoints
PHOTOS_UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
PHOTOS_BATCH_CREATE_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
PHOTOS_LIST_URL = "https://photoslibrary.googleapis.com/v1/mediaItems"


class GooglePhotosClient:
    """Wraps Google Photos API for uploading files."""

    def __init__(
        self,
        credentials_file: str = "credentials.json",
        token_file: str = "gphotos_token.json",
        photos_cache_file: str = "photos_filename_cache.json",
    ):
        self._creds = self._authenticate(credentials_file, token_file)
        self._photos_cache_file = photos_cache_file
        self._filenames: Set[str] = set()
        self._loaded_cache = False

    def _authenticate(self, credentials_file: str, token_file: str) -> Credentials:
        creds = None
        if os.path.exists(token_file):
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(token_file, "w") as token:
                token.write(creds.to_json())
        return creds

    def _get_token(self) -> str:
        if not self._creds.valid:
            self._creds.refresh(Request())
        return self._creds.token

    def list_files(self, folder_path: str = "/", progress_callback=None):
        """Listing files from Google Photos is limited and slow. 
        For now, we'll implement a basic version or skip if used as source.
        """
        # This would require paging through the entire library.
        # For a sync engine, GPhotos is usually a destination.
        logger.warning("Google Photos as a source is not fully supported in this sync engine.")
        return []

    def ensure_path(self, relative_dir: str, root_path: str = "/") -> str:
        """Google Photos doesn't have a folder hierarchy like GDrive. 
        It uses Albums, but for now we'll just return root.
        """
        return "root"

    def find_file(self, name: str, parent_path: str) -> dict | None:
        """Check if a file with the given name exists in the Photos library cache."""
        self.ensure_cache_loaded()
        if name in self._filenames:
            return {"id": name, "name": name}
        return None

    def ensure_cache_loaded(self, force_refresh: bool = False):
        if self._loaded_cache and not force_refresh:
            return

        if not force_refresh and os.path.exists(self._photos_cache_file):
            try:
                with open(self._photos_cache_file) as fh:
                    data = json.load(fh)
                self._filenames = set(data.get("filenames", []))
                self._loaded_cache = True
                return
            except Exception:
                logger.warning("Failed to load Photos cache, rebuilding...")

        self._rebuild_cache()

    def _rebuild_cache(self):
        logger.info("Scanning Google Photos library for existing filenames...")
        filenames: List[str] = []
        page_token = None
        
        while True:
            params = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token

            resp = requests.get(
                PHOTOS_LIST_URL,
                headers={"Authorization": f"Bearer {self._get_token()}"},
                params=params,
            )

            if resp.status_code == 429:
                time.sleep(60)
                continue
            
            if resp.status_code != 200:
                break

            body = resp.json()
            for item in body.get("mediaItems", []):
                fn = item.get("filename", "")
                if fn:
                    filenames.append(fn)

            page_token = body.get("nextPageToken")
            if not page_token:
                break
        
        self._filenames = set(filenames)
        with open(self._photos_cache_file, "w") as fh:
            json.dump({"filenames": sorted(self._filenames)}, fh)
        self._loaded_cache = True

    def upload_file(
        self,
        local_path: Path,
        parent_path: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Upload a file to Google Photos."""
        file_size = os.path.getsize(local_path)
        filename = local_path.name
        
        with open(local_path, "rb") as f:
            data = f.read()
            
        # 1. Upload bytes
        upload_resp = requests.post(
            PHOTOS_UPLOAD_URL,
            headers={
                "Authorization": f"Bearer {self._get_token()}",
                "Content-type": "application/octet-stream",
                "X-Goog-Upload-File-Name": filename,
                "X-Goog-Upload-Protocol": "raw",
            },
            data=data,
        )
        
        if upload_resp.status_code != 200:
            raise RuntimeError(f"Failed to upload bytes: {upload_resp.text}")
            
        upload_token = upload_resp.text
        
        # 2. Create media item
        create_resp = requests.post(
            PHOTOS_BATCH_CREATE_URL,
            headers={
                "Authorization": f"Bearer {self._get_token()}",
                "Content-type": "application/json",
            },
            json={
                "newMediaItems": [{
                    "simpleMediaItem": {
                        "uploadToken": upload_token,
                        "fileName": filename,
                    }
                }]
            },
        )
        
        if create_resp.status_code != 200:
            raise RuntimeError(f"Failed to create media item: {create_resp.text}")
            
        results = create_resp.json().get("newMediaItemResults", [])
        if not results or results[0].get("status", {}).get("code", -1) != 0:
             raise RuntimeError(f"Failed to create media item: {results}")

        if progress_callback:
            progress_callback(file_size, file_size)
            
        self._filenames.add(filename)
        return {"id": filename, "name": filename, "size": file_size}

    def update_file(
        self,
        file_id: str,
        local_path: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Google Photos doesn't support updating existing items via API. 
        We'll just upload a new one or skip.
        """
        return self.upload_file(local_path, "root", progress_callback)

    def verify_integrity(self, local_path: Path, uploaded_meta: dict) -> bool:
        """Verification is limited in Photos. We'll check if it exists in our cache."""
        return uploaded_meta["name"] in self._filenames
