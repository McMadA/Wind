"""Apple Photos (iCloud Photos) client for macOS – indexes existing library, deduplicates by hash/pixel-scan/filename, injects EXIF/GPS metadata, and imports directly into Photos.app."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from sync_drive.metadata import (
    PhotoMetadata,
    apply_metadata_to_file,
    compute_file_hashes,
    extract_from_exif,
)

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = ".icloud_photos_state.json"
DEFAULT_PHOTOS_LIBRARY = Path.home() / "Pictures" / "Photos Library.photoslibrary"

# Generic filenames that should never be skipped on filename alone without a size or hash match
GENERIC_FILENAMES = {
    "image.jpg", "image.jpeg", "image.png", "image.heic", "image.gif",
    "photo.jpg", "photo.jpeg", "photo.png", "photo.heic",
    "video.mp4", "video.mov", "unnamed.jpg", "unnamed.png",
    "download.jpg", "download.png", "fullsizerender.jpg", "fullsizerender.heic",
}

# JXA script to bulk-fetch all filenames and file sizes from Apple Photos
_JXA_LIST_LIBRARY_SCRIPT = """
function run(argv) {
    const Photos = Application("Photos");
    const items = Photos.mediaItems;
    const total = items.length;
    if (total === 0) {
        return JSON.stringify({ count: 0, filenames: [], sizes: [] });
    }
    const filenames = items.filename({ timeout: 600 });
    let sizes = [];
    try {
        sizes = items.size({ timeout: 600 });
    } catch (e) {
        sizes = [];
    }
    return JSON.stringify({
        count: total,
        filenames: filenames || [],
        sizes: sizes || []
    });
}
"""

# JXA script to import a media file into Apple Photos and apply capture date + GPS location + description + favorite
_JXA_IMPORT_SCRIPT = """
function run(argv) {
    const payload = JSON.parse(argv[0]);
    const Photos = Application("Photos");
    const filePath = Path(payload.file_path);

    let importOpts = { skipCheckDuplicates: false };
    if (payload.album_name) {
        const existing = Photos.albums.whose({ name: payload.album_name })();
        const targetAlbum = existing.length > 0
            ? existing[0]
            : Photos.make({ new: "album", named: { name: payload.album_name } });
        importOpts.into = targetAlbum;
    }

    const imported = Photos.import([filePath], importOpts, { timeout: 30 });
    const items = Array.isArray(imported) ? imported : (imported ? [imported] : []);
    let mutated = false;

    for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (payload.taken_epoch_ms !== null && payload.taken_epoch_ms !== undefined) {
            try {
                const targetDate = new Date(payload.taken_epoch_ms);
                const current = item.date();
                if (payload.date_is_fallback) {
                    if (!current || current.getTime() > targetDate.getTime() + 60000) {
                        item.date = targetDate;
                        mutated = true;
                    }
                } else {
                    if (!current || Math.abs(current.getTime() - targetDate.getTime()) > 60000) {
                        item.date = targetDate;
                        mutated = true;
                    }
                }
            } catch (e) {}
        }
        if (payload.latitude !== null && payload.longitude !== null &&
            payload.latitude !== undefined && payload.longitude !== undefined) {
            try {
                const curLoc = item.location();
                const needLoc = !curLoc || curLoc.length < 2 || curLoc[0] === null ||
                    Math.abs(Number(curLoc[0]) - Number(payload.latitude)) > 0.001 ||
                    Math.abs(Number(curLoc[1]) - Number(payload.longitude)) > 0.001;
                if (needLoc) {
                    item.location = [Number(payload.latitude), Number(payload.longitude)];
                    mutated = true;
                }
            } catch (e) {}
        }
        if (payload.description) {
            try {
                item.description = String(payload.description);
                mutated = true;
            } catch (e) {}
        }
        if (payload.favorite) {
            try {
                item.favorite = true;
                mutated = true;
            } catch (e) {}
        }
    }

    return JSON.stringify({
        imported_count: items.length,
        id: items.length > 0 ? items[0].id() : null,
        mutated: mutated
    });
}
"""


class ApplePhotosClient:
    """Client for macOS Apple Photos (which automatically syncs to iCloud Photos).

    Features:
    - Scans your existing Apple Photos library so existing iCloud Photos are skipped.
    - 3-way content deduplication:
        1. Exact file MD5 & SHA-256 hash
        2. EXIF-agnostic JPEG Scan SHA-256 hash (catches identical photos even when Google Photos stripped GPS EXIF!)
        3. Filename + file size matching
    - Triple-layer metadata preservation:
        1. Lossless EXIF injection (DateTimeOriginal + GPSInfo) into JPEG/TIFF/WebP files
        2. macOS APFS file creation date (st_birthtime) & modification date (st_mtime)
        3. Direct Apple Photos MediaItem property assignment (date, GPS location, description, favorite) via JXA
    """

    def __init__(
        self,
        state_file: str = DEFAULT_STATE_FILE,
        album_name: str | None = None,
        staging_dir: str | None = None,
        refresh_cache: bool = False,
        save_every: int = 10,
    ):
        self._state_file = state_file
        self._album_name = album_name
        self._staging_dir = Path(staging_dir).expanduser().resolve() if staging_dir else None
        self._save_every = save_every
        self._ops_since_save = 0
        self._lock = threading.Lock()
        self._import_lock = threading.Lock()
        self._buffer_root = Path(".sync_temp/photos_import_buffer").resolve()
        self._buffer_root.mkdir(parents=True, exist_ok=True)
        self._buffer_seq = 0
        self._buffer_queue: deque[Path] = deque()
        self._mutations_since_restart = 0

        self.filenames: set[str] = set()
        self.filename_sizes: set[str] = set()
        self.md5_hashes: set[str] = set()
        self.sha256_hashes: set[str] = set()
        self.jpeg_scan_hashes: set[str] = set()
        self.imported_source_ids: set[str] = set()
        self.library_scanned: bool = False

        self.ensure_cache_loaded(force_refresh=refresh_cache)

    # ── State & Library Indexing ─────────────────────────────────────

    def ensure_cache_loaded(self, force_refresh: bool = False) -> None:
        if os.path.exists(self._state_file):
            try:
                with open(self._state_file, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self.filenames = set(data.get("filenames", []))
                self.filename_sizes = set(data.get("filename_sizes", []))
                self.md5_hashes = set(data.get("md5_hashes", []))
                self.sha256_hashes = set(data.get("sha256_hashes", []))
                self.jpeg_scan_hashes = set(data.get("jpeg_scan_hashes", []))
                self.imported_source_ids = set(data.get("imported_source_ids", []))
                self.library_scanned = bool(data.get("library_scanned", False))
                logger.info(
                    "Loaded iCloud Photos state: %d filenames, %d MD5 hashes, %d imported IDs.",
                    len(self.filenames),
                    len(self.md5_hashes),
                    len(self.imported_source_ids),
                )
            except Exception as exc:
                logger.warning("Could not read %s (%s); starting fresh.", self._state_file, exc)

        if not self.library_scanned or force_refresh:
            self.scan_apple_photos_library()

    def scan_apple_photos_library(self) -> None:
        """Index existing media items in the macOS Apple Photos library."""
        logger.info("Indexing existing Apple Photos (iCloud Photos) library...")
        indexed_count = self._try_scan_sqlite()
        if indexed_count is None:
            indexed_count = self._scan_via_jxa()

        self.library_scanned = True
        self.flush()
        logger.info(
            "Apple Photos index ready: %d unique filenames (%d filename+size pairs).",
            len(self.filenames),
            len(self.filename_sizes),
        )

    def _try_scan_sqlite(self) -> int | None:
        """Fast read-only scan of Photos.sqlite if Terminal has permission."""
        db_path = DEFAULT_PHOTOS_LIBRARY / "database" / "Photos.sqlite"
        if not db_path.exists():
            return None

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT a.ZORIGINALFILENAME, a.ZORIGINALFILESIZE
                FROM ZADDITIONALASSETATTRIBUTES a
                JOIN ZASSET z ON a.ZASSET = z.Z_PK
                WHERE z.ZTRASHEDSTATE = 0
                """
            )
            count = 0
            for orig_name, orig_size in cur.fetchall():
                if orig_name:
                    fn_lower = str(orig_name).strip().lower()
                    self.filenames.add(fn_lower)
                    if orig_size:
                        self.filename_sizes.add(f"{fn_lower}:{int(orig_size)}")
                    count += 1
            conn.close()
            logger.info("Indexed %d existing items from Photos.sqlite.", count)
            return count
        except Exception as exc:
            logger.debug("SQLite fast-scan unavailable (%s); falling back to Photos.app JXA.", exc)
            return None

    def _scan_via_jxa(self) -> int:
        """Query Apple Photos.app via JXA (JavaScript for Automation) for existing filenames and sizes."""
        try:
            proc = subprocess.run(
                ["/usr/bin/osascript", "-l", "JavaScript", "-e", _JXA_LIST_LIBRARY_SCRIPT],
                capture_output=True,
                text=True,
                timeout=660,
            )
            if proc.returncode != 0:
                logger.warning(
                    "Could not query Apple Photos.app via JXA: %s",
                    proc.stderr.strip(),
                )
                return 0

            data = json.loads(proc.stdout.strip())
            filenames = data.get("filenames") or []
            sizes = data.get("sizes") or []
            for idx, fn in enumerate(filenames):
                if not fn:
                    continue
                fn_lower = str(fn).strip().lower()
                self.filenames.add(fn_lower)
                if idx < len(sizes) and sizes[idx]:
                    try:
                        self.filename_sizes.add(f"{fn_lower}:{int(sizes[idx])}")
                    except (ValueError, TypeError):
                        pass
            count = int(data.get("count", len(filenames)))
            logger.info("Indexed %d existing items from Apple Photos.app via JXA.", count)
            return count
        except Exception as exc:
            logger.warning("Failed to scan Apple Photos via JXA: %s", exc)
            return 0

    def flush(self) -> None:
        """Atomically persist deduplication state to disk."""
        with self._lock:
            self._persist_unlocked()

    def _persist_unlocked(self) -> None:
        tmp_path = f"{self._state_file}.tmp"
        payload = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "library_scanned": self.library_scanned,
            "filenames": sorted(self.filenames),
            "filename_sizes": sorted(self.filename_sizes),
            "md5_hashes": sorted(self.md5_hashes),
            "sha256_hashes": sorted(self.sha256_hashes),
            "jpeg_scan_hashes": sorted(self.jpeg_scan_hashes),
            "imported_source_ids": sorted(self.imported_source_ids),
        }
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp_path, self._state_file)
        self._ops_since_save = 0

    # ── Deduplication Checks ─────────────────────────────────────────

    @staticmethod
    def _source_key(file_meta: dict) -> str | None:
        src = file_meta.get("source")
        fid = file_meta.get("id")
        if src and fid:
            return f"{src}:{fid}"
        return None

    def check_duplicate_pre_download(
        self,
        file_meta: dict,
        dedup_mode: str = "filename+hash",
    ) -> tuple[bool, str]:
        """Check whether *file_meta* can be skipped BEFORE downloading from the cloud."""
        if dedup_mode == "none":
            return False, ""

        with self._lock:
            # 1. Exact source item ID already imported in a previous run
            src_key = self._source_key(file_meta)
            if src_key and src_key in self.imported_source_ids:
                return True, f"already imported ({src_key})"

            # 2. Pre-known MD5 hash (provided by Google Drive API without downloading!)
            md5 = (file_meta.get("md5") or "").lower()
            if md5 and dedup_mode in ("hash", "filename+hash"):
                if md5 in self.md5_hashes:
                    return True, f"MD5 hash already in library ({md5[:12]}...)"

            # 3. Filename / Filename+Size check
            name = (file_meta.get("name") or "").strip()
            name_lower = name.lower()
            size = int(file_meta.get("size") or 0)

            if name_lower and dedup_mode in ("filename", "filename+hash"):
                # Exact filename + byte size match in Apple Photos or previous imports
                if size > 0 and f"{name_lower}:{size}" in self.filename_sizes:
                    return True, f"filename + size ({size} B) already in iCloud Photos"

                # Filename match (only if not a generic name like 'image.jpg')
                if name_lower not in GENERIC_FILENAMES and name_lower in self.filenames:
                    if dedup_mode == "filename" or size == 0:
                        return True, f"filename '{name}' already in iCloud Photos"

        return False, ""

    def check_duplicate_post_download(
        self,
        local_path: Path,
        file_meta: dict,
        dedup_mode: str = "filename+hash",
    ) -> tuple[bool, str, dict[str, str]]:
        """Compute hashes of the downloaded file and check against all known hashes."""
        hashes = compute_file_hashes(local_path)
        if dedup_mode == "none":
            return False, "", hashes

        md5 = hashes["md5"]
        sha256 = hashes["sha256"]
        jpeg_scan = hashes.get("jpeg_scan_sha256")
        actual_size = os.path.getsize(local_path)
        name_lower = (file_meta.get("name") or local_path.name).strip().lower()

        with self._lock:
            if dedup_mode in ("hash", "filename+hash"):
                if md5 in self.md5_hashes:
                    return True, f"exact MD5 content match ({md5[:12]}...)", hashes
                if sha256 in self.sha256_hashes:
                    return True, f"exact SHA-256 content match ({sha256[:12]}...)", hashes
                if jpeg_scan and jpeg_scan in self.jpeg_scan_hashes:
                    return True, f"identical JPEG image pixels ({jpeg_scan[:12]}...)", hashes

            if dedup_mode in ("filename", "filename+hash"):
                if actual_size > 0 and f"{name_lower}:{actual_size}" in self.filename_sizes:
                    return True, f"filename + size ({actual_size} B) match in iCloud Photos", hashes

        return False, "", hashes

    def record_imported(
        self,
        file_meta: dict,
        local_path: Path,
        hashes: dict[str, str] | None = None,
    ) -> None:
        """Record a successfully imported (or verified duplicate) file into the persistent state."""
        if hashes is None and local_path.exists():
            hashes = compute_file_hashes(local_path)

        name_lower = (file_meta.get("name") or local_path.name).strip().lower()
        size = int(file_meta.get("size") or (os.path.getsize(local_path) if local_path.exists() else 0))
        src_key = self._source_key(file_meta)

        with self._lock:
            if name_lower:
                self.filenames.add(name_lower)
                if size > 0:
                    self.filename_sizes.add(f"{name_lower}:{size}")
            if file_meta.get("md5"):
                self.md5_hashes.add(file_meta["md5"].lower())
            if hashes:
                if hashes.get("md5"):
                    self.md5_hashes.add(hashes["md5"])
                if hashes.get("sha256"):
                    self.sha256_hashes.add(hashes["sha256"])
                if hashes.get("jpeg_scan_sha256"):
                    self.jpeg_scan_hashes.add(hashes["jpeg_scan_sha256"])
            if src_key:
                self.imported_source_ids.add(src_key)

            self._ops_since_save += 1
            if self._ops_since_save >= self._save_every:
                self._persist_unlocked()

    # ── SyncEngine compatibility interface ───────────────────────────

    def ensure_path(self, relative_dir: str, root_path: str = "/") -> str:
        """Apple Photos uses a flat library + optional Album rather than nested folders."""
        return self._album_name or "root"

    def find_file(self, name: str, parent_path: str) -> dict | None:
        """Used by SyncEngine pre-download check."""
        name_lower = name.strip().lower()
        with self._lock:
            if name_lower not in GENERIC_FILENAMES and name_lower in self.filenames:
                return {"id": name, "name": name}
        return None

    def upload_file(
        self,
        local_path: Path,
        parent_path: str,
        progress_callback: Callable[[int, int], None] | None = None,
        file_meta: dict | None = None,
    ) -> dict:
        """Enrich *local_path* with EXIF capture date + GPS coordinates and import into Apple Photos."""
        file_meta = file_meta or {"name": local_path.name}

        # 1. Compute hashes BEFORE EXIF injection so raw content hashes match source
        is_dup, dup_reason, raw_hashes = self.check_duplicate_post_download(
            local_path, file_meta, dedup_mode="filename+hash"
        )
        if is_dup:
            logger.info("SKIP post-download duplicate (%s): %s", dup_reason, local_path.name)
            self.record_imported(file_meta, local_path, raw_hashes)
            file_size = os.path.getsize(local_path)
            if progress_callback:
                progress_callback(file_size, file_size)
            return {
                "id": local_path.name,
                "name": local_path.name,
                "size": file_size,
                "skipped_duplicate": True,
                "reason": dup_reason,
            }

        # 2. Apply EXIF DateTimeOriginal + GPSInfo & macOS file creation/modification timestamps
        external_meta: PhotoMetadata | None = file_meta.get("metadata")
        merged_meta = apply_metadata_to_file(local_path, external_meta)
        file_size = os.path.getsize(local_path)

        # 3. Optional staging copy (organized by YYYY/MM)
        if self._staging_dir:
            year_month = (
                merged_meta.taken_at.strftime("%Y/%m")
                if merged_meta.taken_at
                else "Unknown_Date"
            )
            stage_target_dir = self._staging_dir / year_month
            stage_target_dir.mkdir(parents=True, exist_ok=True)
            stage_file = stage_target_dir / local_path.name
            if not stage_file.exists():
                shutil.copy2(local_path, stage_file)

        # 4. Stage file into a persistent rolling buffer so worker_temp cleanup never races with Photos.app
        with self._lock:
            self._buffer_seq += 1
            seq = self._buffer_seq
            while len(self._buffer_queue) > 2500:
                old_dir = self._buffer_queue.popleft()
                shutil.rmtree(old_dir, ignore_errors=True)
            buf_dir = self._buffer_root / f"b_{seq}"
            buf_dir.mkdir(parents=True, exist_ok=True)
            self._buffer_queue.append(buf_dir)

        buffered_path = buf_dir / local_path.name
        try:
            os.link(local_path, buffered_path)
        except OSError:
            shutil.copy2(local_path, buffered_path)

        # 5. Import into macOS Photos.app via JXA and set date + GPS location on the MediaItem
        album = (
            parent_path
            if parent_path and parent_path not in ("/", "root", ".")
            else self._album_name
        )
        taken_epoch_ms = (
            int(merged_meta.taken_at.timestamp() * 1000)
            if merged_meta.taken_at
            else None
        )
        jxa_payload = {
            "file_path": str(buffered_path.resolve()),
            "filename": local_path.name,
            "album_name": album,
            "taken_epoch_ms": taken_epoch_ms,
            "date_is_fallback": merged_meta.date_is_fallback,
            "latitude": merged_meta.latitude if merged_meta.has_location else None,
            "longitude": merged_meta.longitude if merged_meta.has_location else None,
            "description": merged_meta.description,
            "favorite": merged_meta.favorite,
        }

        # Serialize only the IPC call to Photos.app while downloads & EXIF injection run in parallel
        resp_data: dict = {}
        with self._import_lock:
            for import_attempt in range(1, 3):
                resp_data = {}
                rc = -1
                try:
                    proc = subprocess.run(
                        [
                            "/usr/bin/osascript",
                            "-l",
                            "JavaScript",
                            "-e",
                            _JXA_IMPORT_SCRIPT,
                            json.dumps(jxa_payload),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=35,
                    )
                    rc = proc.returncode
                    if rc == 0 and proc.stdout.strip():
                        try:
                            resp_data = json.loads(proc.stdout.strip())
                        except Exception:
                            pass
                except subprocess.TimeoutExpired:
                    rc = -9

                if rc == 0 and int(resp_data.get("imported_count") or 0) > 0:
                    if resp_data.get("mutated"):
                        self._mutations_since_restart += 1
                        if self._mutations_since_restart >= 40:
                            self._mutations_since_restart = 0
                            subprocess.run(["killall", "Photos"], capture_output=True)
                            time.sleep(1.0)
                            subprocess.run(["open", "-g", "-a", "Photos"], capture_output=True)
                            time.sleep(1.5)
                    break

                # If imported_count == 0 or osascript failed, Photos.app may have a modal alert dialog open.
                # Automatically restart Photos.app in the background to dismiss any modal dialog.
                logger.warning(
                    "Photos.app import returned count=%s (rc=%d) for %s [attempt %d/2]; restarting Photos.app to clear any modal dialog...",
                    resp_data.get("imported_count"),
                    rc,
                    local_path.name,
                    import_attempt,
                )
                self._mutations_since_restart = 0
                subprocess.run(["killall", "Photos"], capture_output=True)
                time.sleep(1.5)
                subprocess.run(["open", "-g", "-a", "Photos"], capture_output=True)
                time.sleep(2.0)

        if int(resp_data.get("imported_count") or 0) <= 0:
            # Save fallback copy to iCloud Drive so the file is never lost
            fallback_dir = (
                Path.home()
                / "Library/Mobile Documents/com~apple~CloudDocs/Foto_Import_Fallback"
            )
            fallback_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(buffered_path, fallback_dir / local_path.name)
            raise RuntimeError(
                f"Apple Photos could not import {local_path.name} (saved copy to iCloud Drive/Foto_Import_Fallback)"
            )

        # Also compute post-EXIF hashes so both raw and EXIF-enriched hashes are tracked
        post_hashes = compute_file_hashes(local_path)
        self.record_imported(file_meta, local_path, raw_hashes)
        self.record_imported(file_meta, local_path, post_hashes)

        if progress_callback:
            progress_callback(file_size, file_size)

        date_str = merged_meta.taken_at.strftime("%Y-%m-%d %H:%M:%S") if merged_meta.taken_at else "unknown"
        gps_str = (
            f"GPS({merged_meta.latitude:.4f}, {merged_meta.longitude:.4f})"
            if merged_meta.has_location
            else "no-GPS"
        )
        logger.info(
            "Imported into Apple Photos: %s [Date: %s | %s]",
            local_path.name,
            date_str,
            gps_str,
        )

        return {
            "id": resp_data.get("id") or local_path.name,
            "name": local_path.name,
            "size": file_size,
        }

    def update_file(
        self,
        file_id: str,
        local_path: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        return self.upload_file(local_path, "root", progress_callback)

    def verify_integrity(self, local_path: Path, uploaded_meta: dict) -> bool:
        return local_path.exists() and uploaded_meta is not None
