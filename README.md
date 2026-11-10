# Wind – Cloud Storage Sync & Migration Suite

A high-performance, modular Python suite for syncing, deduplicating, and migrating data across **Google Drive**, **Google Photos**, **Google Takeout**, **Microsoft OneDrive**, **Apple iCloud Drive**, and macOS **Apple Photos**.

Built for large-scale libraries (10,000+ files / 500+ GB) with zero data loss, rock-solid deduplication (hash + pixel comparison), EXIF capture-date and GPS map coordinate preservation, resilient automatic reconnection on socket resets, and verified cleanup.

---

## Key Capabilities

1. **Google Drive → macOS iCloud Drive (`sync_drive.migrate`)**
   - Recursively mirrors original Google Drive folder hierarchies directly into macOS iCloud Drive (`~/Library/Mobile Documents/com~apple~CloudDocs`).
   - Native macOS background syncing via Apple's `bird` daemon—bypasses web API rate limits and 2FA session timeouts.
   - Comprehensive deduplication (path, filename, size, and MD5 checksum), including evicted `.filename.icloud` placeholders.

2. **Google Drive / Photos / Takeout → macOS Apple Photos (`sync_drive.migrate`)**
   - Imports all photos and videos directly into Apple Photos / iCloud Photos library.
   - **Full EXIF & GPS Restoration**: Reads Google Takeout `.json` sidecars or Google Drive metadata, injects `DateTimeOriginal` EXIF tags and GPS latitude/longitude/altitude into files before import, so your photos appear on the Apple Photos World Map and chronological timeline.
   - **Four-tier Deduplication**: Direct SQLite query into Photos library + AppleScript/JXA bridge + SHA256/MD5 hashing prevents duplicate imports.
   - Automatic fallback: Any unsupported codec/legacy clip is safely stored in `iCloud Drive/Foto_Import_Fallback/`.

3. **Microsoft OneDrive Delta Scan & Sync (`sync_drive.onedrive_delta`)**
   - Connects to Microsoft Graph API via interactive device-login (`microsoft.com/devicelogin`).
   - Recursively scans your OneDrive while skipping junk directories (`node_modules`, `.git`, `.next`, `venv`, `Pods`, `.DS_Store`).
   - Cross-checks all items against your local iCloud Drive and Apple Photos index.
   - Reports exact deltas (dry-run) and migrates only missing documents and media.

4. **100% Verified Cloud Cleanup (`sync_drive.cleanup` & `sync_drive.fast_trash`)**
   - Verifies that every single file exists locally on your Mac before touching anything on Google Drive.
   - Moves verified files to Google Drive **Trash** (`trashed=True`) with a 30-day recovery window—never permanently deletes without safety.
   - Multi-threaded parallel batch trashing targeting user-owned files (`'me' in owners`), bypassing shared-file permission blocks.

5. **Modern Web UI (`web/backend/main.py`)**
   - FastAPI backend with a reactive frontend.
   - Real-time terminal streaming via Server-Sent Events (SSE).
   - Form-based controls for paths, dedup modes, worker threads, and dry-run previews.

---

## Quick Start

### 1. Installation

Requires macOS (Sonoma, Ventura, Sequoia) and Python 3.9+.

```bash
# Clone the repository
git clone https://github.com/McMadA/Wind.git
cd Wind

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Credentials

Copy the example environment configuration:

```bash
cp .env.example .env
```

- **Google Drive / Photos**: Place your `credentials.json` (from [Google Cloud Console](https://console.cloud.google.com/)) in the project root.
- **Microsoft OneDrive**: Optional: add `ONEDRIVE_CLIENT_ID` and `ONEDRIVE_CLIENT_SECRET` to `.env` (defaults to standard native public client).

---

## Migration Workflows

### Complete Google (Drive + Photos) → iCloud (Drive + Photos)

To migrate all documents to **iCloud Drive** and all media to **Apple Photos** with EXIF/GPS preservation and deduplication:

```bash
# Preview what will be migrated (Dry Run)
python3 -m sync_drive.migrate --dry-run

# Run full migration (Google Drive Docs -> iCloud Drive, Media -> Apple Photos)
python3 -m sync_drive.migrate --use-gphotos-api

# If you also have a Google Takeout export folder (restores 100% of JSON GPS coordinates):
python3 -m sync_drive.migrate --takeout-dir ~/Downloads/Takeout --use-gphotos-api
```

### Microsoft OneDrive Delta Scan & Migration

Scan your OneDrive account, compare against what's already in iCloud/Photos, and import only the missing items:

```bash
# 1. Scan and report only (Simulation / Dry Run)
python3 -m sync_drive.onedrive_delta

# 2. Live migration of missing delta items (multi-threaded download & import)
python3 -m sync_drive.onedrive_delta --sync --workers 8
```

### Verified Google Drive Cleanup

Safely move files to Google Drive Trash only after 100% verification on macOS:

```bash
# 1. Verify and preview what would be trashed
python3 -m sync_drive.cleanup

# 2. Execute verified cleanup
python3 -m sync_drive.cleanup --execute

# Fast multi-threaded cleanup of remaining owned items:
python3 -m sync_drive.fast_trash
```

---

## Generic CLI Usage (`sync_drive.cli`)

You can run individual sync and transfer tasks between any pair of services:

```bash
# Sync documents from Google Drive -> local macOS iCloud Drive
python3 -m sync_drive.cli --source gdrive --dest icloud-drive --filter documents

# Sync photos & videos from Google Drive -> Apple Photos (iCloud Photos)
python3 -m sync_drive.cli --source gdrive --dest icloud-photos --filter media

# Sync from Google Takeout export -> Apple Photos with EXIF restoration
python3 -m sync_drive.cli --source takeout --source-path ~/Downloads/Takeout --dest icloud-photos

# Sync from OneDrive -> Google Drive
python3 -m sync_drive.cli --source onedrive --dest gdrive --source-path /Documents --dest-path <folder-id>
```

### CLI Options

| Flag | Default | Description |
|---|---|---|
| `--source` | `onedrive` | Source service: `gdrive`, `onedrive`, `icloud-drive`, `icloud-photos`, `takeout`, `gphotos` |
| `--dest` | `gdrive` | Destination service: `gdrive`, `onedrive`, `icloud-drive`, `icloud-photos`, `gphotos` |
| `--source-path` | `root` | Source folder path, ID, or local directory |
| `--dest-path` | `/` | Destination folder path, ID, or album name |
| `--filter` | `all` | File type filter: `all`, `documents`, `media` |
| `--dedup-mode` | `filename+hash` | Deduplication mode: `filename+hash`, `hash`, `filename`, `path`, `none` |
| `--move` | `off` | Delete source file after verified transfer |
| `--dry-run` | `off` | Preview changes without transferring files |
| `--workers` | `8` | Parallel transfer worker threads |
| `--verbose` / `-v` | `off` | Enable detailed debug logging |

---

## Web Interface

Launch the interactive web console:

```bash
python3 web/backend/main.py
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

- Select source and destination services from dropdowns.
- Choose file filters (All, Documents, Photos/Videos).
- Monitor real-time logs and progress bars streamed directly from the CLI.

---

## Privacy & Security

- **Zero Cloud Intermediaries**: All operations run locally on your Mac. No credentials, tokens, or personal file contents are ever sent to third-party servers.
- **Local Credentials**: OAuth tokens (`token.json`, `onedrive_token_cache.bin`) and scan caches are stored exclusively on your local machine and are excluded by `.gitignore`.
- **Soft Deletion Safety Net**: Trashing operations use `trashed=True`, preserving files in your cloud provider's Trash folder for 30 days before permanent purging.

---

## License

MIT License. Feel free to use and modify for your personal cloud migrations.
