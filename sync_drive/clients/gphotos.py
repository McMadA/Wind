"""Google Photos client – authenticates via OAuth 2.0, lists, downloads, and uploads files to Google Photos library."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import List, Set

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from sync_drive.metadata import (
    apply_metadata_to_file,
    extract_from_gphotos_api,
    matches_file_filter,
)

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
    """Wraps Google Photos API for listing, downloading, and uploading media items."""

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
        self._session = requests.Session()

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

    # ── listing (Google Photos as a source) ─────────────────────────

    def list_files(
        self,
        folder_path: str = "/",
        progress_callback: Callable[[int, str], None] | None = None,
        file_filter: str = "all",
    ) -> Generator[dict, None, None]:
        """Page through the user's Google Photos library and yield media item metadata dicts."""
        if file_filter == "documents":
            logger.info("Skipping Google Photos scan because file_filter='documents'.")
            return

        logger.info("Scanning Google Photos library...")
        page_token = None
        count = 0

        while True:
            params: dict = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token

            try:
                resp = self._session.get(
                    PHOTOS_LIST_URL,
                    headers={"Authorization": f"Bearer {self._get_token()}"},
                    params=params,
                    timeout=30,
                )
            except requests.exceptions.RequestException as exc:
                logger.warning("Google Photos connection reset (%s); reconnecting in 3s...", exc)
                time.sleep(3)
                self._session = requests.Session()
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                logger.warning("Google Photos API returned %s; waiting 15s...", resp.status_code)
                time.sleep(15)
                continue

            if resp.status_code != 200:
                raise RuntimeError(
                    f"Google Photos API error ({resp.status_code}): {resp.text[:200]}"
                )

            body = resp.json()
            for item in body.get("mediaItems", []):
                filename = item.get("filename") or f"{item['id']}.jpg"
                mime_type = item.get("mimeType", "image/jpeg")
                if not matches_file_filter(filename, mime_type, file_filter):
                    continue

                photo_meta = extract_from_gphotos_api(item)
                media_meta = item.get("mediaMetadata") or {}
                is_video = "video" in media_meta or mime_type.startswith("video/")

                file_meta = {
                    "id": item["id"],
                    "name": filename,
                    "path": f"/{filename}",
                    "size": 0,  # Google Photos API does not return byte size in list
                    "mimeType": mime_type,
                    "baseUrl": item.get("baseUrl"),
                    "is_video": is_video,
                    "createdTime": media_meta.get("creationTime"),
                    "metadata": photo_meta,
                    "source": "gphotos",
                }
                count += 1
                yield file_meta
                if progress_callback:
                    progress_callback(count, "Google Photos")

            page_token = body.get("nextPageToken")
            if not page_token:
                break

    # ── downloading (with baseUrl refresh & EXIF date restoration) ──

    def _fetch_fresh_base_url(self, media_item_id: str) -> str:
        """Fetch a fresh baseUrl for *media_item_id* (since Google Photos baseUrls expire after 60m)."""
        resp = self._session.get(
            f"{PHOTOS_LIST_URL}/{media_item_id}",
            headers={"Authorization": f"Bearer {self._get_token()}"},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to refresh Google Photos item {media_item_id}: {resp.status_code}"
            )
        return resp.json()["baseUrl"]

    def download_file(
        self,
        file_meta: dict,
        dest_dir: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download original media bytes from Google Photos and restore capture date metadata."""
        relative = file_meta["path"].lstrip("/")
        local_path = Path(dest_dir) / relative
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # Avoid overwriting a different photo with the same filename in temp dir
        if local_path.exists():
            short_id = file_meta["id"][-8:]
            local_path = local_path.with_name(
                f"{local_path.stem}_{short_id}{local_path.suffix}"
            )

        base_url = file_meta.get("baseUrl")
        if not base_url:
            base_url = self._fetch_fresh_base_url(file_meta["id"])

        dl_suffix = "=dv" if file_meta.get("is_video") else "=d"
        download_url = f"{base_url}{dl_suffix}"

        resp = self._session.get(download_url, stream=True)
        if resp.status_code in (401, 403):
            # baseUrl expired (> 60 minutes old); fetch a fresh one
            base_url = self._fetch_fresh_base_url(file_meta["id"])
            resp = self._session.get(f"{base_url}{dl_suffix}", stream=True)

        if resp.status_code != 200 and dl_suffix == "=dv":
            # Fallback to =d if =dv fails
            resp = self._session.get(f"{base_url}=d", stream=True)

        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to download {file_meta['name']} from Google Photos: HTTP {resp.status_code}"
            )

        total_size = int(resp.headers.get("Content-Length", 0))
        downloaded = 0

        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_size or downloaded)

        file_meta["size"] = downloaded

        # Restore EXIF DateTimeOriginal & macOS file creation/modification timestamps
        photo_meta = file_meta.get("metadata")
        if photo_meta:
            apply_metadata_to_file(local_path, photo_meta)

        return local_path

    # ── destination helpers ─────────────────────────────────────────

    def ensure_path(self, relative_dir: str, root_path: str = "/") -> str:
        """Google Photos doesn't have a folder hierarchy like GDrive."""
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

            resp = self._session.get(
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
        upload_resp = self._session.post(
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
        create_resp = self._session.post(
            PHOTOS_BATCH_CREATE_URL,
            headers={
                "Authorization": f"Bearer {self._get_token()}",
                "Content-type": "application/json",
            },
            json={
                "newMediaItems": [
                    {
                        "simpleMediaItem": {
                            "uploadToken": upload_token,
                            "fileName": filename,
                        }
                    }
                ]
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
        """Google Photos doesn't support updating existing items via API."""
        return self.upload_file(local_path, "root", progress_callback)

    def verify_integrity(self, local_path: Path, uploaded_meta: dict) -> bool:
        """Check if uploaded filename is registered in our cache."""
        return uploaded_meta["name"] in self._filenames
