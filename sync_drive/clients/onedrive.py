"""OneDrive client – authenticates via MSAL, lists, downloads, and uploads files using Microsoft Graph."""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import time
from collections.abc import Callable, Generator
from pathlib import Path
from urllib.parse import quote

import msal
import requests

from sync_drive.metadata import (
    PhotoMetadata,
    matches_file_filter,
    parse_date_from_filename,
    parse_datetime_any,
)

logger = logging.getLogger(__name__)

DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Files.Read"]


IGNORED_FOLDER_NAMES = {
    "node_modules",
    ".git",
    ".next",
    ".cache",
    "venv",
    ".venv",
    "__pycache__",
    "pods",
}


class OneDriveClient:
    """Wraps Microsoft Graph API for listing, downloading, and uploading OneDrive files."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        tenant_id: str = "common",
        redirect_uri: str = "http://localhost:8400",
        token_cache_path: str = "onedrive_token_cache.bin",
    ):
        self._cache = msal.SerializableTokenCache()
        self._token_cache_path = token_cache_path
        if os.path.exists(token_cache_path):
            self._cache.deserialize(open(token_cache_path).read())

        self.client_id = client_id or os.getenv("ONEDRIVE_CLIENT_ID") or DEFAULT_CLIENT_ID
        self._app = msal.PublicClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            token_cache=self._cache,
        )
        self._redirect_uri = redirect_uri

    # ── authentication ──────────────────────────────────────────────

    def _save_cache(self) -> None:
        if self._cache.has_state_changed:
            with open(self._token_cache_path, "w") as f:
                f.write(self._cache.serialize())

    def _get_token(self) -> str:
        accounts = self._app.get_accounts()
        result = None
        if accounts:
            result = self._app.acquire_token_silent(SCOPES, account=accounts[0])

        if not result:
            flow = self._app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise RuntimeError(f"Device flow failed: {flow.get('error_description', 'unknown error')}")
            verification_uri = flow.get("verification_uri", "https://microsoft.com/devicelogin")
            user_code = flow["user_code"]
            logger.info(
                "To sign in, visit %s and enter code: %s",
                verification_uri,
                user_code,
            )
            print(f"\n  To sign in, visit {verification_uri} and enter code: {user_code}\n")
            sys.stdout.flush()
            try:
                import subprocess
                subprocess.run(["open", verification_uri], capture_output=True)
            except Exception:
                pass
            result = self._app.acquire_token_by_device_flow(flow)

        if "access_token" not in result:
            raise RuntimeError(f"Authentication failed: {result.get('error_description', 'unknown error')}")

        self._save_cache()
        return result["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    # ── path helpers ────────────────────────────────────────────────

    @staticmethod
    def _encode_path(path: str) -> str:
        """URL-encode a OneDrive path, quoting special characters in each segment."""
        parts = [p for p in path.split("/") if p]
        return "/".join(quote(p, safe="") for p in parts)

    # ── file listing ────────────────────────────────────────────────

    def list_files(
        self,
        folder_path: str = "/",
        progress_callback: Callable[[int, str], None] | None = None,
        file_filter: str = "all",
    ) -> Generator[dict, None, None]:
        """Return a flat generator of file metadata dicts under *folder_path* (recursive).

        *progress_callback(file_count, current_folder)* is called each time a
        new file is discovered so the caller can display scanning progress.
        """
        return self._walk(folder_path, [0], progress_callback, file_filter=file_filter)

    def _walk(
        self,
        path: str,
        counter: list[int],
        progress_callback: Callable[[int, str], None] | None = None,
        file_filter: str = "all",
    ) -> Generator[dict, None, None]:
        logger.info("  Scanning: %s", path)
        endpoint = (
            f"{GRAPH_BASE}/me/drive/root/children"
            if path == "/"
            else f"{GRAPH_BASE}/me/drive/root:/{self._encode_path(path)}:/children"
        )
        while endpoint:
            data = None
            for attempt in range(1, 6):
                try:
                    resp = requests.get(endpoint, headers=self._headers(), timeout=45)
                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get("Retry-After", 5))
                        logger.warning("OneDrive rate limited (429). Backing off for %ds...", retry_after)
                        time.sleep(retry_after)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except (requests.exceptions.RequestException, requests.exceptions.Timeout) as exc:
                    if attempt == 5:
                        raise
                    logger.warning("OneDrive scan interrupted (%s) on attempt %d/5; retrying in %ds...", exc, attempt, attempt * 2)
                    time.sleep(attempt * 2)

            if not data:
                break
            for item in data.get("value", []):
                if "folder" in item:
                    folder_name = item.get("name", "").strip().lower()
                    if folder_name in IGNORED_FOLDER_NAMES:
                        logger.info("Skipping ignored folder: %s/%s", path, item["name"])
                        continue
                    child_path = f"{path.rstrip('/')}/{item['name']}"
                    yield from self._walk(child_path, counter, progress_callback, file_filter=file_filter)
                elif "file" in item:
                    if item.get("name") in {".DS_Store", "Thumbs.db", "desktop.ini"}:
                        continue
                    mime_type = item.get("file", {}).get("mimeType")
                    if not matches_file_filter(item["name"], mime_type, file_filter):
                        continue
                    rel_path = f"{path.rstrip('/')}/{item['name']}"
                    photo_info = item.get("photo") or {}
                    loc_info = item.get("location") or {}
                    fs_info = item.get("fileSystemInfo") or {}

                    date_is_fallback = False
                    taken_at = parse_datetime_any(photo_info.get("takenDateTime"))
                    if not taken_at:
                        taken_at = parse_date_from_filename(item["name"], rel_path)
                    if not taken_at:
                        created = parse_datetime_any(fs_info.get("createdDateTime") or item.get("createdDateTime"))
                        modified = parse_datetime_any(fs_info.get("lastModifiedDateTime") or item.get("lastModifiedDateTime"))
                        taken_at = min(created, modified) if (created and modified) else (modified or created)
                        if taken_at is not None:
                            date_is_fallback = True

                    lat = loc_info.get("latitude")
                    lon = loc_info.get("longitude")
                    alt = loc_info.get("altitude")

                    file_meta = {
                        "id": item["id"],
                        "name": item["name"],
                        "path": rel_path,
                        "size": item.get("size", 0),
                        "sha256": item.get("file", {}).get("hashes", {}).get("sha256Hash"),
                        "sha1": item.get("file", {}).get("hashes", {}).get("sha1Hash"),
                        "download_url": item.get("@microsoft.graph.downloadUrl"),
                        "mimeType": mime_type,
                        "source": "onedrive",
                        "metadata": PhotoMetadata(
                            taken_at=taken_at,
                            latitude=float(lat) if lat is not None else None,
                            longitude=float(lon) if lon is not None else None,
                            altitude=float(alt) if alt is not None else None,
                            description=item.get("description"),
                            original_filename=item["name"],
                            date_is_fallback=date_is_fallback,
                        ),
                    }
                    counter[0] += 1
                    yield file_meta
                    if progress_callback:
                        progress_callback(counter[0], path)
            endpoint = data.get("@odata.nextLink")

    # ── download ────────────────────────────────────────────────────

    def download_file(
        self,
        file_meta: dict,
        dest_dir: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Download a single file to *dest_dir*, preserving its relative path. Returns the local Path."""
        relative = file_meta["path"].lstrip("/")
        local_path = Path(dest_dir) / relative
        local_path.parent.mkdir(parents=True, exist_ok=True)

        # Always fetch a fresh download URL – the one captured during list_files
        # contains a short-lived tempauth token that expires within seconds.
        meta_resp = requests.get(
            f"{GRAPH_BASE}/me/drive/items/{file_meta['id']}",
            headers=self._headers(),
            timeout=30,
        )
        meta_resp.raise_for_status()
        url = meta_resp.json().get("@microsoft.graph.downloadUrl")
        if not url:
            url = f"{GRAPH_BASE}/me/drive/items/{file_meta['id']}/content"

        logger.debug("Downloading %s ...", relative)
        resp = requests.get(url, stream=True, timeout=120)
        resp.raise_for_status()

        total_size = file_meta.get("size") or int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, total_size)

        return local_path

    # ── folder creation ─────────────────────────────────────────────

    def ensure_path(self, relative_dir: str, root_path: str = "/") -> str:
        """Ensure all intermediate folders for *relative_dir* exist under *root_path*.

        Returns the full OneDrive path to the deepest folder.
        """
        parts = [p for p in relative_dir.split("/") if p]
        current_path = root_path.rstrip("/") or "/"
        for part in parts:
            target_path = f"{current_path}/{part}" if current_path != "/" else f"/{part}"
            # Check if folder exists
            encoded = self._encode_path(target_path)
            check_url = f"{GRAPH_BASE}/me/drive/root:/{encoded}:"
            resp = requests.get(check_url, headers=self._headers(), timeout=30)
            if resp.status_code == 404:
                # Create the folder
                if current_path == "/":
                    parent_url = f"{GRAPH_BASE}/me/drive/root/children"
                else:
                    encoded_parent = self._encode_path(current_path)
                    parent_url = f"{GRAPH_BASE}/me/drive/root:/{encoded_parent}:/children"
                body = {
                    "name": part,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "fail",
                }
                create_resp = requests.post(
                    parent_url,
                    headers={**self._headers(), "Content-Type": "application/json"},
                    json=body,
                    timeout=30,
                )
                # 409 Conflict means it already exists (race condition) – that's fine
                if create_resp.status_code not in (200, 201, 409):
                    create_resp.raise_for_status()
                logger.info("Created OneDrive folder: %s", target_path)
            elif resp.status_code != 200:
                resp.raise_for_status()
            current_path = target_path
        return current_path

    # ── query ───────────────────────────────────────────────────────

    def find_file(self, name: str, parent_path: str) -> dict | None:
        """Return metadata of an existing file with *name* under *parent_path*, or None."""
        file_path = f"{parent_path.rstrip('/')}/{name}"
        encoded = self._encode_path(file_path)
        url = f"{GRAPH_BASE}/me/drive/root:/{encoded}:"
        resp = requests.get(url, headers=self._headers(), timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        item = resp.json()
        if "folder" in item:
            return None
        return {
            "id": item["id"],
            "name": item["name"],
            "size": item.get("size", 0),
            "sha256": item.get("file", {}).get("hashes", {}).get("sha256Hash"),
        }

    # ── upload ──────────────────────────────────────────────────────

    def upload_file(
        self,
        local_path: Path,
        parent_path: str,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Upload *local_path* into *parent_path* on OneDrive. Returns item metadata."""
        file_size = os.path.getsize(local_path)
        dest = f"{parent_path.rstrip('/')}/{local_path.name}"

        if file_size < 4 * 1024 * 1024:  # < 4 MB: simple upload
            encoded = self._encode_path(dest)
            url = f"{GRAPH_BASE}/me/drive/root:/{encoded}:/content"
            with open(local_path, "rb") as f:
                data = f.read()
            resp = requests.put(
                url,
                headers={**self._headers(), "Content-Type": "application/octet-stream"},
                data=data,
                timeout=120,
            )
            resp.raise_for_status()
            if progress_callback:
                progress_callback(file_size, file_size)
            logger.debug("Uploaded %s", local_path.name)
            return resp.json()
        else:
            return self._upload_large(local_path, dest, file_size, progress_callback)

    def update_file(
        self,
        item_id: str,
        local_path: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Overwrite an existing OneDrive file with new content."""
        file_size = os.path.getsize(local_path)

        if file_size < 4 * 1024 * 1024:  # < 4 MB: simple upload
            url = f"{GRAPH_BASE}/me/drive/items/{item_id}/content"
            with open(local_path, "rb") as f:
                data = f.read()
            resp = requests.put(
                url,
                headers={**self._headers(), "Content-Type": "application/octet-stream"},
                data=data,
                timeout=120,
            )
            resp.raise_for_status()
            if progress_callback:
                progress_callback(file_size, file_size)
            logger.debug("Overwritten %s", local_path.name)
            return resp.json()
        else:
            # Create upload session via item ID
            session_url = f"{GRAPH_BASE}/me/drive/items/{item_id}/createUploadSession"
            body = {"item": {"@microsoft.graph.conflictBehavior": "replace"}}
            resp = requests.post(
                session_url,
                headers={**self._headers(), "Content-Type": "application/json"},
                json=body,
                timeout=30,
            )
            resp.raise_for_status()
            upload_url = resp.json()["uploadUrl"]
            return self._upload_chunks(local_path, upload_url, file_size, progress_callback)

    def _upload_large(
        self,
        local_path: Path,
        dest_path: str,
        file_size: int,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Create an upload session for a new file and upload in chunks."""
        encoded = self._encode_path(dest_path)
        url = f"{GRAPH_BASE}/me/drive/root:/{encoded}:/createUploadSession"
        body = {"item": {"@microsoft.graph.conflictBehavior": "replace"}}
        resp = requests.post(
            url,
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        upload_url = resp.json()["uploadUrl"]
        return self._upload_chunks(local_path, upload_url, file_size, progress_callback)

    def _upload_chunks(
        self,
        local_path: Path,
        upload_url: str,
        file_size: int,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Upload a file in 10 MB chunks to a resumable upload session URL."""
        chunk_size = 10 * 1024 * 1024  # 10 MB (multiple of 320 KiB)
        uploaded = 0
        result = None
        with open(local_path, "rb") as f:
            while uploaded < file_size:
                chunk_data = f.read(chunk_size)
                chunk_end = min(uploaded + len(chunk_data) - 1, file_size - 1)
                headers = {
                    "Content-Length": str(len(chunk_data)),
                    "Content-Range": f"bytes {uploaded}-{chunk_end}/{file_size}",
                }
                chunk_resp = requests.put(
                    upload_url, headers=headers, data=chunk_data, timeout=120
                )
                chunk_resp.raise_for_status()
                uploaded += len(chunk_data)
                if progress_callback:
                    progress_callback(uploaded, file_size)
                if chunk_resp.status_code in (200, 201):
                    result = chunk_resp.json()
        return result

    def delete_file(self, item_id: str) -> None:
        """Delete a file from OneDrive."""
        url = f"{GRAPH_BASE}/me/drive/items/{item_id}"
        resp = requests.delete(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        logger.info("Deleted OneDrive file: %s", item_id)

    # ── verification ────────────────────────────────────────────────

    def verify_integrity(self, local_path: Path, uploaded_meta: dict) -> bool:
        """Compare local SHA256 against the SHA256 OneDrive computed on upload."""
        remote_sha256 = uploaded_meta.get("file", {}).get("hashes", {}).get("sha256Hash")
        if not remote_sha256:
            remote_sha256 = self.get_file_sha256(uploaded_meta["id"])
        if not remote_sha256:
            logger.warning(
                "OneDrive did not return a SHA256 for %s \u2013 skipping verification.",
                local_path.name,
            )
            return True

        local_sha256 = self.compute_sha256(local_path)
        return local_sha256.upper() == remote_sha256.upper()

    def get_file_sha256(self, item_id: str) -> str | None:
        """Return the sha256Hash reported by OneDrive for the given item."""
        url = f"{GRAPH_BASE}/me/drive/items/{item_id}"
        resp = requests.get(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json().get("file", {}).get("hashes", {}).get("sha256Hash")

    @staticmethod
    def compute_sha256(filepath: Path) -> str:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for block in iter(lambda: f.read(8192), b""):
                h.update(block)
        return h.hexdigest().upper()
