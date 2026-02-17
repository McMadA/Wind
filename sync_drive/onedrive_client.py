"""OneDrive client – authenticates via MSAL and downloads files using Microsoft Graph."""

import hashlib
import logging
import os
from collections.abc import Callable
import sys
from pathlib import Path

import msal
import requests

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Files.Read", "Files.Read.All"]


class OneDriveClient:
    """Wraps Microsoft Graph API for listing and downloading OneDrive files."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tenant_id: str = "common",
        redirect_uri: str = "http://localhost:8400",
        token_cache_path: str = "onedrive_token_cache.bin",
    ):
        self._cache = msal.SerializableTokenCache()
        self._token_cache_path = token_cache_path
        if os.path.exists(token_cache_path):
            self._cache.deserialize(open(token_cache_path).read())

        self._app = msal.PublicClientApplication(
            client_id,
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
            logger.info(
                "To sign in, visit https://microsoft.com/devicelogin and enter code: %s",
                flow["user_code"],
            )
            print(f"\n  To sign in, visit https://microsoft.com/devicelogin and enter code: {flow['user_code']}\n")
            sys.stdout.flush()
            result = self._app.acquire_token_by_device_flow(flow)

        if "access_token" not in result:
            raise RuntimeError(f"Authentication failed: {result.get('error_description', 'unknown error')}")

        self._save_cache()
        return result["access_token"]

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    # ── file operations ─────────────────────────────────────────────

    def list_files(self, folder_path: str = "/") -> list[dict]:
        """Return a flat list of file metadata dicts under *folder_path* (recursive)."""
        items: list[dict] = []
        self._walk(folder_path, items)
        return items

    def _walk(self, path: str, accumulator: list[dict]) -> None:
        logger.info("  Scanning: %s", path)
        endpoint = (
            f"{GRAPH_BASE}/me/drive/root/children"
            if path == "/"
            else f"{GRAPH_BASE}/me/drive/root:/{path.strip('/')}:/children"
        )
        while endpoint:
            resp = requests.get(endpoint, headers=self._headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("value", []):
                if "folder" in item:
                    child_path = f"{path.rstrip('/')}/{item['name']}"
                    self._walk(child_path, accumulator)
                elif "file" in item:
                    accumulator.append({
                        "id": item["id"],
                        "name": item["name"],
                        "path": f"{path.rstrip('/')}/{item['name']}",
                        "size": item.get("size", 0),
                        "sha256": item.get("file", {}).get("hashes", {}).get("sha256Hash"),
                        "sha1": item.get("file", {}).get("hashes", {}).get("sha1Hash"),
                        "download_url": item.get("@microsoft.graph.downloadUrl"),
                    })
            endpoint = data.get("@odata.nextLink")

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

    @staticmethod
    def compute_sha256(filepath: Path) -> str:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for block in iter(lambda: f.read(8192), b""):
                h.update(block)
        return h.hexdigest().upper()
