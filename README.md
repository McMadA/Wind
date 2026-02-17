# Wind – OneDrive to Google Drive Sync

Automatically moves files from OneDrive to Google Drive and verifies every
transfer using MD5 checksums.

## Features

- **Recursive sync** – syncs all files and folders from a OneDrive directory
- **MD5 verification** – every upload is verified against Google Drive's checksum
- **Progress bars** – real-time download/upload progress with transfer speeds
- **Colored output** – structured, color-coded log messages in the terminal
- **Dry-run mode** – preview which files would be synced before transferring
- **Duplicate handling** – skip, overwrite, or create copies of existing files
- **Audit log** – every run is saved to a timestamped log file in `logs/`

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

# Preview files without transferring (dry run)
python -m sync_drive.cli --dry-run

# Verbose output
python -m sync_drive.cli -v

# Disable colored output and progress bars
python -m sync_drive.cli --no-color
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
| –                            | `--on-duplicate`      | `skip`            |
| –                            | `--dry-run`           | off               |
| –                            | `--no-color`          | off               |
| –                            | `--verbose` / `-v`    | off               |
| `NO_COLOR`                   | –                     | *(unset)*         |

Set the `NO_COLOR` environment variable to any value to disable colored output
(follows the [no-color.org](https://no-color.org/) convention).

## Project structure

```
sync_drive/
  __init__.py           # Package version
  cli.py                # CLI entry point with rich logging and progress
  onedrive_client.py    # Microsoft Graph / OneDrive API wrapper
  gdrive_client.py      # Google Drive API wrapper
  sync_engine.py        # Orchestrator with progress bars and checksum verification
```
