# Wind – OneDrive to Google Drive Sync

Automatically moves files from OneDrive to Google Drive and verifies every
transfer using MD5 checksums.

## How it works

1. Lists all files in the specified OneDrive folder (recursively).
2. Downloads each file to a local temp directory.
3. Recreates the folder structure in Google Drive and uploads the file.
4. Compares the local MD5 against the MD5 reported by Google Drive.
5. Reports a summary of transferred, verified, and failed files.

## Prerequisites

- Python 3.11+
- A **Microsoft Azure** app registration (for OneDrive / Microsoft Graph access)
- A **Google Cloud** project with the Drive API enabled and an OAuth 2.0 client

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials
cp .env.example .env
# Fill in ONEDRIVE_CLIENT_ID, ONEDRIVE_CLIENT_SECRET, etc.

# 3. Place your Google OAuth credentials file
#    Download credentials.json from Google Cloud Console and put it
#    in the project root.
```

## Usage

```bash
# Sync everything in OneDrive root to Google Drive root
python -m sync_drive.cli

# Sync a specific OneDrive folder into a specific Google Drive folder
python -m sync_drive.cli --onedrive-folder /Documents --gdrive-folder-id <folder-id>

# Verbose output
python -m sync_drive.cli -v
```

## Configuration

All options can be set via environment variables (`.env`) or CLI flags:

| Env variable                 | CLI flag              | Default           |
| ---------------------------- | --------------------- | ----------------- |
| `ONEDRIVE_CLIENT_ID`         | –                     | *(required)*      |
| `ONEDRIVE_CLIENT_SECRET`     | –                     | *(required)*      |
| `ONEDRIVE_TENANT_ID`         | –                     | `common`          |
| `ONEDRIVE_SYNC_FOLDER`       | `--onedrive-folder`   | `/`               |
| `GOOGLE_CREDENTIALS_FILE`    | –                     | `credentials.json`|
| `GOOGLE_DRIVE_TARGET_FOLDER` | `--gdrive-folder-id`  | `root`            |
| `TEMP_DIR`                   | `--temp-dir`          | `.sync_temp`      |

## Project structure

```
sync_drive/
  __init__.py
  cli.py               # CLI entry point
  onedrive_client.py    # Microsoft Graph / OneDrive API wrapper
  gdrive_client.py      # Google Drive API wrapper
  sync_engine.py        # Orchestrator with checksum verification
```
