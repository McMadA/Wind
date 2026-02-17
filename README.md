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

## Getting credentials

### OneDrive (Microsoft Azure)

1. Go to the [Azure App Registrations](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade) portal and sign in.
2. Click **New registration**.
   - **Name**: choose any name (e.g. `WindSync`)
   - **Supported account types**: select *Personal Microsoft accounts only* (or include org accounts if needed)
   - **Redirect URI**: select **Public client/native (mobile & desktop)** and enter `http://localhost:8400`
3. Click **Register**. On the overview page, copy the **Application (client) ID** — this is your `ONEDRIVE_CLIENT_ID`.
4. In the left sidebar go to **Certificates & secrets** > **Client secrets** > **New client secret**. Copy the secret **Value** (not the ID) — this is your `ONEDRIVE_CLIENT_SECRET`.
5. In **API permissions**, make sure **Microsoft Graph > Files.Read** (delegated) is listed. Click **Add a permission** > **Microsoft Graph** > **Delegated permissions** > search for `Files.Read` and `Files.Read.All`, then add them.

### Google Drive

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a new project (or select an existing one).
2. Enable the **Google Drive API**: go to **APIs & Services** > **Library**, search for "Google Drive API", and click **Enable**.
3. Set up the OAuth consent screen: go to **APIs & Services** > **OAuth consent screen**.
   - Choose **External** user type and click **Create**.
   - Fill in the required app name and email fields, then click **Save and Continue**.
   - On the **Scopes** step, click **Add or remove scopes**, search for `https://www.googleapis.com/auth/drive`, select it, and click **Update**.
   - Add your Google account email under **Test users** (required while the app is in "Testing" status).
4. Create credentials: go to **APIs & Services** > **Credentials** > **Create Credentials** > **OAuth client ID**.
   - **Application type**: Desktop app
   - **Name**: choose any name
5. Click **Download JSON** and save the file as `credentials.json` in the project root.

### Finding your Google Drive folder ID

If you want to sync into a specific Google Drive folder instead of the root:

1. Open the folder in [Google Drive](https://drive.google.com) in your browser.
2. The URL will look like: `https://drive.google.com/drive/folders/1aBcDeFgHiJkLmNoPqRsTuVwXyZ`
3. The last part of the URL (`1aBcDeFgHiJkLmNoPqRsTuVwXyZ`) is the folder ID — use it as `GOOGLE_DRIVE_TARGET_FOLDER` in your `.env`.

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure credentials
cp .env.example .env
# Fill in ONEDRIVE_CLIENT_ID, ONEDRIVE_CLIENT_SECRET, etc.
# (see "Getting credentials" above)

# 3. Place your Google OAuth credentials file
#    (credentials.json from the Google Cloud Console step above)
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
