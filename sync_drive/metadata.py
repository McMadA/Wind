"""Metadata extraction, EXIF/GPS injection, macOS timestamp preservation, and content hashing."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

try:
    import piexif
except ImportError:
    piexif = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Media MIME types and file extensions used to separate Photos/Videos from Documents
MEDIA_MIME_PREFIXES = ("image/", "video/")

NON_PHOTO_EXTENSIONS = {".svg", ".ico", ".cur", ".icns"}
NON_PHOTO_MIMES = {"image/svg+xml", "image/x-icon", "image/vnd.microsoft.icon"}

MEDIA_EXTENSIONS = {
    # Images
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".bmp", ".tiff", ".tif", ".avif", ".jp2", ".jxl",
    # RAW formats
    ".raw", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".pef", ".srw", ".raf",
    # Videos
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".3g2",
    ".mts", ".m2ts", ".mpg", ".mpeg", ".wmv", ".flv", ".webm", ".ogv",
}

EXIF_WRITABLE_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
HEIC_EXIF_EXTENSIONS = {".heic", ".heif", ".avif"}
QUICKTIME_EXTENSIONS = {".mov", ".mp4", ".m4v", ".3gp", ".3g2"}

# Common filename timestamp patterns (including OneDrive iOS YYYYMMDD_HHMMSSmmm_iOS, Pixel, WhatsApp, BeReal, etc.)
_FILENAME_DATE_PATTERNS = [
    # 1. IMG_20210814_153022, PXL_20210814_153022123, 20221203_223618607_iOS, Bathroom-20240328125733253, Recording 2024-11-06 142908
    re.compile(
        r"(?<!\d)(?P<Y>20\d{2}|19\d{2})[-_.]?(?P<m>0[1-9]|1[0-2])[-_.]?(?P<d>0[1-9]|[12]\d|3[01])"
        r"(?:[-_ T]| at )?(?P<H>[01]\d|2[0-3])[-_.:]?(?P<M>[0-5]\d)[-_.:]?(?P<S>[0-5]\d)(?:\d{1,6})?(?!\d)",
        re.IGNORECASE,
    ),
    # 2. Explicit YYYY-MM-DD or YYYY.MM.DD date (e.g. 2020-01-01(1).jpg, 2020-04-03 (1).png)
    re.compile(
        r"(?<!\d)(?P<Y>20\d{2}|19\d{2})[-_.](?P<m>0[1-9]|1[0-2])[-_.](?P<d>0[1-9]|[12]\d|3[01])(?!\d)",
        re.IGNORECASE,
    ),
    # 3. WhatsApp Image 2021-08-14 ... or IMG-20210814-WA0001
    re.compile(
        r"(?:IMG|VID|WhatsApp|Screenshot|Photo|Video)[-_ ]*(?:Image|Video)?[-_ ]*"
        r"(?P<Y>20\d{2}|19\d{2})[-_.]*(?P<m>0[1-9]|1[0-2])[-_.]*(?P<d>0[1-9]|[12]\d|3[01])(?!\d)",
        re.IGNORECASE,
    ),
]

_FILENAME_EPOCH_MS_RE = re.compile(r"(?:^|[-_ ])(?P<epoch_ms>1[3-8]\d{11})(?:$|[-_. (])")
_FILENAME_YYMMDD_PREFIX_RE = re.compile(r"^(?P<y>1\d|2[0-6])(?P<m>0[1-9]|1[0-2])(?P<d>0[1-9]|[12]\d|3[01])(?:\s+|[-_])")
_PATH_YEAR_MONTH_RE = re.compile(r"/(?P<Y>20\d{2}|19\d{2})/(?P<m>0[1-9]|1[0-2])(?:/|$)")


def is_media_file(filename: str, mime_type: str | None = None) -> bool:
    """Return True if the file is a photo or video based on MIME type or extension."""
    ext = Path(filename).suffix.lower()
    if ext in NON_PHOTO_EXTENSIONS:
        return False
    if mime_type and mime_type.lower() in NON_PHOTO_MIMES:
        return False
    if ext in MEDIA_EXTENSIONS:
        return True
    if mime_type:
        mime_lower = mime_type.lower()
        if any(mime_lower.startswith(prefix) for prefix in MEDIA_MIME_PREFIXES):
            return True
    return False


def matches_file_filter(filename: str, mime_type: str | None, file_filter: str) -> bool:
    """Check whether a file matches the requested filter ('all', 'media', or 'documents')."""
    if file_filter == "all":
        return True
    is_media = is_media_file(filename, mime_type)
    if file_filter == "media":
        return is_media
    if file_filter == "documents":
        return not is_media
    raise ValueError(f"Unknown file_filter: {file_filter}")


@dataclass
class PhotoMetadata:
    """Normalized metadata for a photo or video item."""

    taken_at: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    description: str | None = None
    favorite: bool = False
    original_filename: str | None = None
    date_is_fallback: bool = False

    @property
    def has_location(self) -> bool:
        if self.latitude is None or self.longitude is None:
            return False
        # Ignore (0.0, 0.0) "Null Island" which Google Takeout uses for unset GPS
        if abs(self.latitude) < 1e-6 and abs(self.longitude) < 1e-6:
            return False
        return -90.0 <= self.latitude <= 90.0 and -180.0 <= self.longitude <= 180.0

    def merge_with(self, fallback: PhotoMetadata | None) -> PhotoMetadata:
        """Return a new PhotoMetadata filling missing fields from *fallback*.

        Non-fallback capture dates (from EXIF, QuickTime, Takeout, or filename)
        always take precedence over fallback modification/upload dates.
        """
        if fallback is None:
            return self
        lat = self.latitude if self.has_location else (fallback.latitude if fallback.has_location else None)
        lon = self.longitude if self.has_location else (fallback.longitude if fallback.has_location else None)
        alt = self.altitude if self.altitude is not None else fallback.altitude

        if self.taken_at and not self.date_is_fallback:
            chosen_date = self.taken_at
            is_fb = False
        elif fallback.taken_at and not fallback.date_is_fallback:
            chosen_date = fallback.taken_at
            is_fb = False
        elif self.taken_at:
            chosen_date = self.taken_at
            is_fb = self.date_is_fallback
        else:
            chosen_date = fallback.taken_at
            is_fb = fallback.date_is_fallback

        return PhotoMetadata(
            taken_at=chosen_date,
            latitude=lat,
            longitude=lon,
            altitude=alt,
            description=self.description or fallback.description,
            favorite=self.favorite or fallback.favorite,
            original_filename=self.original_filename or fallback.original_filename,
            date_is_fallback=is_fb,
        )


# ── Date parsing helpers ─────────────────────────────────────────────────────


def parse_datetime_any(value: str | int | float | None) -> datetime | None:
    """Parse EXIF date strings, ISO-8601 / RFC-3339 strings, or Unix timestamps."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            if value <= 0:
                return None
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None

    text = str(value).strip()
    if not text:
        return None

    # Pure numeric timestamp string (e.g. Google Takeout "1594730096")
    if text.isdigit():
        return parse_datetime_any(int(text))

    # EXIF format: "YYYY:MM:DD HH:MM:SS"
    for fmt in (
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y:%m:%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if 1900 <= dt.year <= 2100:
                return dt
        except ValueError:
            pass

    # ISO-8601 fallback
    try:
        iso_text = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if 1900 <= dt.year <= 2100:
            return dt
    except ValueError:
        pass

    return None


def parse_date_from_filename(filename: str, relative_path: str = "") -> datetime | None:
    """Try to extract a capture date from common camera/OneDrive/WhatsApp/Pixel/BeReal filename patterns."""
    stem = Path(filename).stem
    for pattern in _FILENAME_DATE_PATTERNS:
        m = pattern.search(stem)
        if m:
            groups = m.groupdict()
            try:
                year = int(groups["Y"])
                month = int(groups["m"])
                day = int(groups["d"])
                hour = int(groups.get("H") or 12)
                minute = int(groups.get("M") or 0)
                second = int(groups.get("S") or 0)
                if 1970 <= year <= 2100:
                    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
            except ValueError:
                continue

    # Check 13-digit Unix millisecond timestamp in filename (e.g. bereal-1662494061910.jpg, FACE_SC_1588928034251.jpg)
    m_ms = _FILENAME_EPOCH_MS_RE.search(stem)
    if m_ms:
        try:
            epoch_s = int(m_ms.group("epoch_ms")) / 1000.0
            dt = datetime.fromtimestamp(epoch_s, tz=timezone.utc)
            if 2005 <= dt.year <= 2030:
                return dt
        except (ValueError, OSError):
            pass

    # Check YYMMDD prefix (e.g. "200826 RABO Bouma.jpg")
    m_yy = _FILENAME_YYMMDD_PREFIX_RE.match(stem)
    if m_yy:
        try:
            year = 2000 + int(m_yy.group("y"))
            month = int(m_yy.group("m"))
            day = int(m_yy.group("d"))
            return datetime(year, month, day, 12, 0, 0, tzinfo=timezone.utc)
        except ValueError:
            pass

    # Fallback: check folder path for /YYYY/MM/ (e.g. /Camera-album/2023/10/abarth.mp4)
    if relative_path:
        m_path = _PATH_YEAR_MONTH_RE.search(relative_path)
        if m_path:
            try:
                year = int(m_path.group("Y"))
                month = int(m_path.group("m"))
                return datetime(year, month, 15, 12, 0, 0, tzinfo=timezone.utc)
            except ValueError:
                pass

    return None


# ── Extractors for Google Drive, Google Photos API, and Google Takeout ───────


def extract_from_gdrive_meta(file_meta: dict) -> PhotoMetadata:
    """Extract capture date, GPS coordinates, and description from Google Drive file metadata."""
    img_meta = file_meta.get("imageMediaMetadata") or {}
    vid_meta = file_meta.get("videoMediaMetadata") or {}
    loc = img_meta.get("location") or vid_meta.get("location") or {}
    rel_path = file_meta.get("path") or file_meta.get("relative_dir") or ""

    # Priority:
    # 1. EXIF capture time reported by Drive (imageMediaMetadata.time)
    # 2. Filename / folder path pattern (for OneDrive YYYYMMDD_HHMMSSmmm_iOS, WhatsApp, BeReal, etc.)
    # 3. Drive modifiedTime / createdTime (marked as date_is_fallback=True so embedded EXIF/QuickTime wins)
    date_is_fallback = False
    taken_at = parse_datetime_any(img_meta.get("time"))
    if not taken_at:
        taken_at = parse_date_from_filename(file_meta.get("name", ""), rel_path)

    if not taken_at:
        created = parse_datetime_any(file_meta.get("createdTime"))
        modified = parse_datetime_any(file_meta.get("modifiedTime"))
        if created and modified:
            taken_at = min(created, modified)
        else:
            taken_at = modified or created
        if taken_at is not None:
            date_is_fallback = True

    lat = loc.get("latitude")
    lon = loc.get("longitude")
    alt = loc.get("altitude")

    return PhotoMetadata(
        taken_at=taken_at,
        latitude=float(lat) if lat is not None else None,
        longitude=float(lon) if lon is not None else None,
        altitude=float(alt) if alt is not None else None,
        description=file_meta.get("description"),
        favorite=bool(file_meta.get("starred", False)),
        original_filename=file_meta.get("name"),
        date_is_fallback=date_is_fallback,
    )


def extract_from_gphotos_api(item: dict) -> PhotoMetadata:
    """Extract capture date and description from a Google Photos API mediaItem dict."""
    media_meta = item.get("mediaMetadata") or {}
    filename = item.get("filename", "")
    taken_at = (
        parse_datetime_any(media_meta.get("creationTime"))
        or parse_date_from_filename(filename)
    )
    return PhotoMetadata(
        taken_at=taken_at,
        description=item.get("description"),
        original_filename=filename or None,
    )


def find_takeout_sidecar(media_path: Path) -> Path | None:
    """Locate the matching Google Takeout .json sidecar for *media_path*."""
    parent = media_path.parent
    name = media_path.name
    stem = media_path.stem
    suffix = media_path.suffix

    # 1. Direct candidates in the same folder
    candidates = [
        parent / f"{name}.json",
        parent / f"{name}.supplemental-metadata.json",
        parent / f"{stem}.json",
        parent / f"{stem}.supplemental-metadata.json",
    ]

    # 2. Handle edited variants (e.g. IMG_1234-bewerkt.jpg, IMG_1234-edited.jpg)
    clean_stem = re.sub(r"[-_](?:edited|bewerkt|effects|smile|pop)$", "", stem, flags=re.IGNORECASE)
    if clean_stem != stem:
        candidates.extend([
            parent / f"{clean_stem}{suffix}.json",
            parent / f"{clean_stem}{suffix}.supplemental-metadata.json",
            parent / f"{clean_stem}.json",
        ])

    # 3. Handle numbered duplicates like IMG_1234(1).jpg -> IMG_1234.jpg(1).json
    dup_match = re.match(r"^(.*?)(\(\d+\))$", stem)
    if dup_match:
        base_stem, num_part = dup_match.groups()
        candidates.extend([
            parent / f"{base_stem}{suffix}{num_part}.json",
            parent / f"{base_stem}{suffix}.supplemental-metadata{num_part}.json",
            parent / f"{base_stem}{suffix}.json",
            parent / f"{base_stem}.json",
        ])

    for cand in candidates:
        if cand.is_file():
            return cand

    # 4. Fallback: truncated Takeout sidecar filenames (Google truncates long names before .json)
    prefix = name[:40].lower()
    try:
        for entry in parent.iterdir():
            if entry.suffix.lower() == ".json" and entry.name.lower().startswith(prefix):
                return entry
    except OSError:
        pass

    return None


def extract_from_takeout_sidecar(media_path: Path) -> PhotoMetadata:
    """Read capture date, GPS coordinates, description, and favorite flag from a Google Takeout .json sidecar."""
    sidecar = find_takeout_sidecar(media_path)
    meta = PhotoMetadata(
        taken_at=parse_date_from_filename(media_path.name, str(media_path)),
        original_filename=media_path.name,
    )
    if not sidecar:
        return meta

    try:
        with open(sidecar, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.debug("Could not parse Takeout JSON %s: %s", sidecar, exc)
        return meta

    # 1. Capture timestamp
    taken_ts = (
        (data.get("photoTakenTime") or {}).get("timestamp")
        or (data.get("creationTime") or {}).get("timestamp")
    )
    taken_at = parse_datetime_any(taken_ts) or meta.taken_at

    # 2. GPS coordinates (prefer geoData, fallback to geoDataExif)
    lat: float | None = None
    lon: float | None = None
    alt: float | None = None
    for geo_key in ("geoData", "geoDataExif"):
        geo = data.get(geo_key) or {}
        g_lat = geo.get("latitude")
        g_lon = geo.get("longitude")
        if g_lat is not None and g_lon is not None:
            if abs(float(g_lat)) > 1e-6 or abs(float(g_lon)) > 1e-6:
                lat = float(g_lat)
                lon = float(g_lon)
                if geo.get("altitude") is not None:
                    alt = float(geo["altitude"])
                break

    description = data.get("description") or None
    favorited = bool(data.get("favorited", False))
    title = data.get("title") or media_path.name

    return PhotoMetadata(
        taken_at=taken_at,
        latitude=lat,
        longitude=lon,
        altitude=alt,
        description=description,
        favorite=favorited,
        original_filename=title,
    )


# ── EXIF & QuickTime Reading & Writing ───────────────────────────────────────


def _rational_to_float(val) -> float:
    if isinstance(val, tuple) and len(val) == 2 and val[1] != 0:
        return float(val[0]) / float(val[1])
    return float(val)


def _dms_to_deg(dms, ref: bytes | str) -> float | None:
    try:
        deg = _rational_to_float(dms[0])
        minutes = _rational_to_float(dms[1])
        seconds = _rational_to_float(dms[2])
        result = deg + (minutes / 60.0) + (seconds / 3600.0)
        ref_str = ref.decode("ascii", errors="ignore") if isinstance(ref, bytes) else str(ref)
        if ref_str.upper() in ("S", "W"):
            result = -result
        return result
    except Exception:
        return None


def _deg_to_dms_rational(deg_float: float) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    abs_deg = abs(deg_float)
    degrees = int(abs_deg)
    minutes_full = (abs_deg - degrees) * 60.0
    minutes = int(minutes_full)
    seconds = (minutes_full - minutes) * 60.0
    sec_frac = Fraction(seconds).limit_denominator(10000)
    return ((degrees, 1), (minutes, 1), (sec_frac.numerator, sec_frac.denominator))


_ISO6709_RE = re.compile(rb"([+-]\d{2}\.\d{3,8})([+-]\d{3}\.\d{3,8})(?:([+-]\d+\.\d+))?")
_QT_ISO_DATE_RE = re.compile(rb"(20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:?\d{2}|Z))")


def _extract_from_quicktime(local_path: Path) -> PhotoMetadata:
    """Extract capture date and GPS coordinates from QuickTime / MP4 (.mov, .mp4, .m4v) containers."""
    taken_at: datetime | None = None
    lat: float | None = None
    lon: float | None = None
    alt: float | None = None

    try:
        size = os.path.getsize(local_path)
        with open(local_path, "rb") as fh:
            head = fh.read(min(size, 524288))
            tail = b""
            if size > 524288:
                fh.seek(max(0, size - 524288))
                tail = fh.read(524288)
        blob = head + tail

        # 1. Check Apple QuickTime ISO-8601 creationdate string (most accurate, includes timezone)
        m_date = _QT_ISO_DATE_RE.search(blob)
        if m_date:
            raw_iso = m_date.group(1).decode("ascii", errors="ignore")
            # Normalize +0100 -> +01:00 for fromisoformat
            if len(raw_iso) >= 5 and (raw_iso[-5] in "+-") and ":" not in raw_iso[-5:]:
                raw_iso = f"{raw_iso[:-2]}:{raw_iso[-2:]}"
            taken_at = parse_datetime_any(raw_iso)

        # 2. Fallback to MP4 mvhd atom creation_time (seconds since 1904-01-01 UTC)
        if not taken_at:
            mvhd_idx = blob.find(b"mvhd")
            if mvhd_idx != -1 and mvhd_idx + 20 <= len(blob):
                version = blob[mvhd_idx + 4]
                if version == 0:
                    ctime = struct.unpack(">I", blob[mvhd_idx + 8 : mvhd_idx + 12])[0]
                else:
                    ctime = struct.unpack(">Q", blob[mvhd_idx + 8 : mvhd_idx + 16])[0]
                if ctime > 2082844800:  # > 1970-01-01
                    dt = datetime.fromtimestamp(ctime - 2082844800, tz=timezone.utc)
                    if 1990 <= dt.year <= 2030:
                        taken_at = dt

        # 3. Check QuickTime ISO-6709 GPS coordinates (+DD.DDDD+DDD.DDDD)
        m_gps = _ISO6709_RE.search(blob)
        if m_gps:
            try:
                cand_lat = float(m_gps.group(1))
                cand_lon = float(m_gps.group(2))
                if -90.0 <= cand_lat <= 90.0 and -180.0 <= cand_lon <= 180.0:
                    lat = cand_lat
                    lon = cand_lon
                    if m_gps.group(3):
                        alt = float(m_gps.group(3))
            except ValueError:
                pass
    except Exception as exc:
        logger.debug("Could not parse QuickTime metadata from %s: %s", local_path.name, exc)

    return PhotoMetadata(taken_at=taken_at, latitude=lat, longitude=lon, altitude=alt)


def extract_from_exif(local_path: Path) -> PhotoMetadata:
    """Read existing DateTimeOriginal and GPS coordinates from JPEG/TIFF/WebP/HEIC EXIF or QuickTime/MP4."""
    ext = local_path.suffix.lower()
    if ext in QUICKTIME_EXTENSIONS:
        return _extract_from_quicktime(local_path)

    if piexif is None:
        return PhotoMetadata()

    exif_dict = None
    if ext in EXIF_WRITABLE_EXTENSIONS:
        try:
            exif_dict = piexif.load(str(local_path))
        except Exception:
            return PhotoMetadata()
    elif ext in HEIC_EXIF_EXTENSIONS:
        try:
            with open(local_path, "rb") as fh:
                head = fh.read(262144)
            for sig in (b"Exif\x00\x00II", b"Exif\x00\x00MM"):
                idx = head.find(sig)
                if idx != -1:
                    exif_dict = piexif.load(head[idx : idx + 65536])
                    break
        except Exception:
            return PhotoMetadata()

    if not exif_dict:
        return PhotoMetadata()

    # 1. Capture Date
    taken_at: datetime | None = None
    exif_ifd = exif_dict.get("Exif") or {}
    zeroth_ifd = exif_dict.get("0th") or {}

    for raw_val in (
        exif_ifd.get(piexif.ExifIFD.DateTimeOriginal),
        exif_ifd.get(piexif.ExifIFD.DateTimeDigitized),
        zeroth_ifd.get(piexif.ImageIFD.DateTime),
    ):
        if raw_val:
            text = raw_val.decode("utf-8", errors="ignore") if isinstance(raw_val, bytes) else str(raw_val)
            taken_at = parse_datetime_any(text.strip("\x00 "))
            if taken_at:
                break

    # 2. GPS Info
    lat: float | None = None
    lon: float | None = None
    alt: float | None = None
    gps_ifd = exif_dict.get("GPS") or {}
    if gps_ifd:
        raw_lat = gps_ifd.get(piexif.GPSIFD.GPSLatitude)
        raw_lat_ref = gps_ifd.get(piexif.GPSIFD.GPSLatitudeRef, b"N")
        raw_lon = gps_ifd.get(piexif.GPSIFD.GPSLongitude)
        raw_lon_ref = gps_ifd.get(piexif.GPSIFD.GPSLongitudeRef, b"E")
        if raw_lat and raw_lon:
            lat = _dms_to_deg(raw_lat, raw_lat_ref)
            lon = _dms_to_deg(raw_lon, raw_lon_ref)
        raw_alt = gps_ifd.get(piexif.GPSIFD.GPSAltitude)
        if raw_alt:
            try:
                alt = _rational_to_float(raw_alt)
                if gps_ifd.get(piexif.GPSIFD.GPSAltitudeRef) == 1:
                    alt = -alt
            except Exception:
                pass

    return PhotoMetadata(
        taken_at=taken_at,
        latitude=lat,
        longitude=lon,
        altitude=alt,
    )


def apply_metadata_to_file(local_path: Path, external_meta: PhotoMetadata | None = None) -> PhotoMetadata:
    """Ensure *local_path* has embedded EXIF capture date + GPS (if supported) and macOS creation/modification timestamps.

    Returns the merged PhotoMetadata (combining embedded EXIF/QuickTime + external_meta).
    """
    embedded = extract_from_exif(local_path)
    merged = embedded.merge_with(external_meta)

    fn_date = parse_date_from_filename(local_path.name, str(local_path))
    if fn_date:
        if not merged.taken_at or merged.date_is_fallback:
            merged.taken_at = fn_date
            merged.date_is_fallback = False

    # 1. Inject EXIF into JPEG/TIFF/WebP if capture date or GPS is missing in the file itself
    if piexif is not None and local_path.suffix.lower() in EXIF_WRITABLE_EXTENSIONS:
        needs_date = embedded.taken_at is None and merged.taken_at is not None
        needs_gps = (not embedded.has_location) and merged.has_location

        if needs_date or needs_gps:
            try:
                try:
                    exif_dict = piexif.load(str(local_path))
                except Exception:
                    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

                if needs_date and merged.taken_at is not None:
                    exif_date_bytes = merged.taken_at.strftime("%Y:%m:%d %H:%M:%S").encode("ascii")
                    exif_dict.setdefault("Exif", {})[piexif.ExifIFD.DateTimeOriginal] = exif_date_bytes
                    exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = exif_date_bytes
                    exif_dict.setdefault("0th", {})[piexif.ImageIFD.DateTime] = exif_date_bytes

                if needs_gps and merged.latitude is not None and merged.longitude is not None:
                    gps_ifd = exif_dict.setdefault("GPS", {})
                    gps_ifd[piexif.GPSIFD.GPSVersionID] = (2, 2, 0, 0)
                    gps_ifd[piexif.GPSIFD.GPSLatitudeRef] = b"N" if merged.latitude >= 0 else b"S"
                    gps_ifd[piexif.GPSIFD.GPSLatitude] = _deg_to_dms_rational(merged.latitude)
                    gps_ifd[piexif.GPSIFD.GPSLongitudeRef] = b"E" if merged.longitude >= 0 else b"W"
                    gps_ifd[piexif.GPSIFD.GPSLongitude] = _deg_to_dms_rational(merged.longitude)
                    if merged.altitude is not None:
                        gps_ifd[piexif.GPSIFD.GPSAltitudeRef] = 0 if merged.altitude >= 0 else 1
                        alt_frac = Fraction(abs(merged.altitude)).limit_denominator(100)
                        gps_ifd[piexif.GPSIFD.GPSAltitude] = (alt_frac.numerator, alt_frac.denominator)

                exif_bytes = piexif.dump(exif_dict)
                piexif.insert(exif_bytes, str(local_path))
                logger.debug(
                    "Injected EXIF into %s (date=%s, gps=%s)",
                    local_path.name,
                    merged.taken_at.isoformat() if merged.taken_at else None,
                    f"{merged.latitude:.5f},{merged.longitude:.5f}" if merged.has_location else None,
                )
            except Exception as exc:
                logger.debug("Could not inject EXIF into %s: %s", local_path.name, exc)

    # 2. Set macOS file creation time (st_birthtime) and modification time (st_mtime)
    # On macOS APFS, setting utime to a timestamp older than current birthtime
    # automatically sets BOTH birthtime and mtime to that timestamp!
    if merged.taken_at is not None:
        set_file_timestamp(local_path, merged.taken_at)

    return merged


def set_file_timestamp(local_path: Path, dt: datetime) -> None:
    """Set filesystem modification and creation time on macOS."""
    try:
        ts = dt.timestamp()
        # Clamp to sensible POSIX range (1970-01-02 .. now)
        ts = max(86400.0, ts)
        os.utime(local_path, (ts, ts))
    except Exception as exc:
        logger.debug("Could not set timestamp on %s: %s", local_path, exc)


# ── Hashing (Full-file MD5/SHA256 + EXIF-agnostic JPEG Scan Hash) ────────────


def compute_file_hashes(local_path: Path) -> dict[str, str]:
    """Compute MD5, SHA-256, and (for JPEGs) the EXIF-agnostic compressed scan SHA-256.

    Why `jpeg_scan_sha256`?
    When the same photo exists in Google Drive (with full EXIF/GPS) and in Google Photos
    (where Google strips GPS or modifies EXIF tags), the file-level MD5 differs,
    but the JPEG compressed pixel stream starting at SOS (0xFFDA) is 100% identical!
    """
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()

    with open(local_path, "rb") as fh:
        data = fh.read()

    md5.update(data)
    sha256.update(data)

    result = {
        "md5": md5.hexdigest().lower(),
        "sha256": sha256.hexdigest().lower(),
    }

    # Check if JPEG (starts with 0xFFD8) and find Start Of Scan (0xFFDA)
    if len(data) > 4 and data[:2] == b"\xff\xd8":
        sos_index = _find_jpeg_sos(data)
        if sos_index > 0:
            scan_hash = hashlib.sha256(data[sos_index:]).hexdigest().lower()
            result["jpeg_scan_sha256"] = scan_hash

    return result


def _find_jpeg_sos(data: bytes) -> int:
    """Find the byte offset of the main JPEG Start-Of-Scan (0xFFDA) marker, skipping thumbnails."""
    idx = 2
    length = len(data)
    while idx + 4 < length:
        if data[idx] != 0xFF:
            idx += 1
            continue
        marker = data[idx + 1]
        # Skip padding 0xFF bytes
        while marker == 0xFF and idx + 2 < length:
            idx += 1
            marker = data[idx + 1]
        # Standalone markers without length
        if marker in (0xD8, 0xD9) or (0xD0 <= marker <= 0xD7) or marker == 0x01:
            idx += 2
            continue
        if marker == 0xDA:  # Start of Scan (main image)
            return idx
        if idx + 4 > length:
            break
        seg_len = (data[idx + 2] << 8) | data[idx + 3]
        if seg_len < 2:
            break
        idx += 2 + seg_len
    return -1
