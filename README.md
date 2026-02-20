# Wind – Cloud Storage Sync Tools

A collection of sync scripts for moving files between cloud storage services.

- **Cloud Drive Sync** – Generic sync between **OneDrive**, **Google Drive**, **iCloud**, and **Google Photos** with verification.
- **Google Drive → Google Photos (v2)** – multi-threaded bulk upload with advanced deduplication.

---

## Tool 1 – Cloud Drive Sync (`sync_drive/`)

### Features

- **Multi-service support** – Sync between any combination of **OneDrive**, **Google Drive**, **iCloud Drive**, and **Google Photos**.
- **Recursive sync** – syncs all files and folders from the source directory.
- **Move mode** – delete files from source after successful verification at destination (`--move`). This uses a "Copy-then-Verify-then-Delete" strategy to ensure no data loss.
- **Integrity verification** – Service-specific checks:
    - **OneDrive**: SHA256 hash comparison.
    - **Google Drive**: MD5 hash comparison.
    - **iCloud**: File size comparison.
    - **Google Photos**: Upload confirmation.
- **Dry-run mode** – preview which files would be synced before transferring.
- **Duplicate handling** – skip, overwrite, or create copies of existing files.
- **Audit log** – every run is saved to a timestamped log file in `logs/`.

### How it works

1. Lists all files in the specified source folder (recursively).
2. Downloads each file to a local temp directory.
3. Recreates the folder structure at the destination and uploads the file.
4. Verifies the upload using the destination's integrity check (SHA256, MD5, or size).
5. **(Optional) Move Logic**: If `--move` is specified, the source file is deleted **only if verification passed**. If a checksum mismatch is detected, the source file is preserved and an error is logged.
6. Reports a summary of transferred, verified, and failed files.

### Prerequisites

- Python 3.11+
- A **Microsoft Azure** app registration (for OneDrive / Microsoft Graph access).
- A **Google Cloud** project with the Drive and Photos Library APIs enabled and an OAuth 2.0 client.
- **Apple ID** and App-Specific Password (for iCloud).

### Getting credentials

#### iCloud (Apple)

1. Sign in to [appleid.apple.com](https://appleid.apple.com/).
2. Go to **Sign-In and Security** > **App-Specific Passwords**.
3. Generate a new password (e.g., "WindSync").
4. Use your Apple ID and this password in your `.env` file (`APPLE_ID` and `APPLE_PASSWORD`).
   - Note: The first time you run with iCloud, you will be prompted for a 2FA code in the terminal.

#### OneDrive (Microsoft Azure)

1. Go to the [Azure App Registrations](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade) portal and sign in.
2. Click **New registration**.
   - **Name**: `WindSync`
   - **Supported account types**: select *Personal Microsoft accounts only*
   - **Redirect URI**: select **Public client/native (mobile & desktop)** and enter `http://localhost:8400`
3. Click **Register**. Copy the **Application (client) ID** — this is your `ONEDRIVE_CLIENT_ID`.
4. In the left sidebar go to **Certificates & secrets** > **Client secrets** > **New client secret**. Copy the secret **Value** (not the ID) — this is your `ONEDRIVE_CLIENT_SECRET`.
5. In **API permissions**, click **Add a permission** > **Microsoft Graph** > **Delegated permissions** > search for `Files.ReadWrite` and `Files.ReadWrite.All`, then add them.

#### Google Drive & Photos

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a new project.
2. Enable the **Google Drive API** and **Photos Library API**.
3. Set up the OAuth consent screen: go to **APIs & Services** > **OAuth consent screen**.
   - Choose **External** user type and click **Create**.
   - On the **Scopes** step, add `https://www.googleapis.com/auth/drive` and `https://www.googleapis.com/auth/photoslibrary`.
   - Add your Google account email under **Test users**.
4. Create credentials: go to **APIs & Services** > **Credentials** > **Create Credentials** > **OAuth client ID**.
   - **Application type**: Desktop app
5. Click **Download JSON** and save the file as `credentials.json` in the project root.

### Setup

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

### Usage

```bash
# ── Sync between any service (OneDrive, GDrive, iCloud) ──

# Sync from iCloud to Google Drive
python -m sync_drive.cli --source icloud --dest gdrive --source-path /Photos --dest-path <gdrive-folder-id>

# Sync from Google Drive to Google Photos
python -m sync_drive.cli --source gdrive --dest gphotos --source-path <gdrive-folder-id>

# Sync from OneDrive to Google Photos
python -m sync_drive.cli --source onedrive --dest gphotos --source-path /Pictures

# ── Moving files (Delete source after sync) ──

# Move from iCloud to OneDrive
python -m sync_drive.cli --source icloud --dest onedrive --source-path /Documents --dest-path /Backup --move

# Move from OneDrive to iCloud
python -m sync_drive.cli --source onedrive --dest icloud --source-path /Archive --dest-path / --move

# ── Common options ──

# Preview files without transferring (dry run)
python -m sync_drive.cli --source icloud --dest gdrive --dry-run

# Verbose output
python -m sync_drive.cli -v

# Overwrite existing files instead of skipping
python -m sync_drive.cli --on-duplicate overwrite

# Disable colored output and progress bars
python -m sync_drive.cli --no-color
```

### Configuration

All options can be set via environment variables (`.env`) or CLI flags. CLI flags take precedence.

| Env variable              | CLI flag              | Default            | Description |
| ------------------------- | --------------------- | ------------------ | ----------- |
| `SOURCE_SERVICE`          | `--source`            | `onedrive`         | `onedrive, gdrive, icloud, gphotos` |
| `DEST_SERVICE`            | `--dest`              | `gdrive`           | `onedrive, gdrive, icloud, gphotos` |
| `SOURCE_PATH`             | `--source-path`       | `/`                | Source folder path or ID |
| `DEST_PATH`               | `--dest-path`         | `/`                | Destination folder path or ID |
| `TEMP_DIR`                | `--temp-dir`          | `.sync_temp`       | Local temp directory for downloads |
| –                         | `--on-duplicate`      | `skip`             | `skip, overwrite, duplicate` |
| –                         | `--move`              | off                | Delete source after successful verify |
| –                         | `--dry-run`           | off                | List files without transferring |
| –                         | `--verbose` / `-v`    | off                | Enable debug logging |
| –                         | `--no-color`          | off                | Disable colored output/progress bars |
| `ONEDRIVE_CLIENT_ID`      | –                     | *(required)*       | Azure App Client ID |
| `ONEDRIVE_CLIENT_SECRET`  | –                     | *(required)*       | Azure App Client Secret |
| `APPLE_ID`                | –                     | *(required)*       | Apple ID for iCloud |
| `APPLE_PASSWORD`          | –                     | *(required)*       | App-Specific Password for iCloud |
| `GOOGLE_CREDENTIALS_FILE` | –                     | `credentials.json` | Path to Google OAuth JSON |

Set the `NO_COLOR` environment variable to any value to disable colored output (follows the [no-color.org](https://no-color.org/) convention).

### Project structure

```
sync_drive/
  clients/              # Cloud storage service implementations
    gdrive.py           # Google Drive API wrapper
    onedrive.py         # Microsoft Graph / OneDrive API wrapper
    icloud.py           # iCloud Drive API wrapper
    gphotos.py          # Google Photos API wrapper
  cli.py                # CLI entry point with rich logging and progress
  engine.py             # Orchestrator with progress bars and verification
tools/
  drive2photos/         # Advanced multi-threaded tool for GDrive to Photos
infra/
  free-vm.bicep         # Azure Bicep template for a free VM
```

---

## Tool 2 – Google Drive → Google Photos Sync (`drive2photos/`)

Bulk-uploads photos and videos from Google Drive directly into your Google
Photos library. This script is optimized for performance using a **Batch Collector**
that groups uploads, reducing API round-trips by up to 50×.

### Features

- **Multi-threaded uploads** – configurable worker count (`--workers N`, default 10).
- **Batching** – uses the `batchCreate` endpoint to process up to 50 items per request.
- **Three dedup modes** – `filename`, `hash`, or `filename+hash` to avoid re-uploading.
- **Photos library cache** – scans your Photos library once and caches results locally.
- **Resumable** – progress is saved to `uploaded_ids.json`, allowing for easy resumption.
- **Date filter** – `--since YYYY-MM-DD` to only process recently modified files.
- **Dry-run mode** – preview what would be uploaded without touching Photos.
- **Metadata preserved** – original Drive filename and timestamps stored in the Photos item description.
- **Interactive folder browser** – navigate your Drive hierarchy and pick folders.

### Prerequisites

```bash
pip install google-api-python-client google-auth google-auth-oauthlib requests
```

Uses the same `credentials.json` in the project root as Tool 1. Ensure the **Photos Library API** is enabled in your Google Cloud project.

### State files (auto-created in `tools/drive2photos/`)

| File | Purpose |
|---|---|
| `uploaded_ids.json` | Drive file IDs that were successfully uploaded |
| `uploaded_hashes.json` | SHA-256 hashes of uploaded content (hash dedup) |
| `photos_filename_cache.json` | Cached Photos library filenames (filename dedup) |

### Usage

```bash
cd tools/drive2photos

# Interactive folder picker (default)
python drive_to_photos_sync.py

# Sync a specific folder by Drive folder ID
python drive_to_photos_sync.py --folder DRIVE_FOLDER_ID

# Sync everything in your entire Drive
python drive_to_photos_sync.py --all --workers 10

# Most thorough dedup (checks filename AND content hash)
python drive_to_photos_sync.py --dedup-mode filename+hash

# Preview what would be uploaded without uploading
python drive_to_photos_sync.py --dry-run --all
```

### Performance tips

| Goal | Command |
|---|---|
| First-time sync | `python drive_to_photos_sync.py --all --workers 15 --skip-dedup` |
| Fast incremental | `python drive_to_photos_sync.py --all --workers 15 --dedup-mode filename` |
| High integrity | `python drive_to_photos_sync.py --all --workers 10 --dedup-mode filename+hash` |

Key levers:

- **`--workers N`** — 10-15 is the sweet spot for the Photos API.
- **`--skip-dedup`** — bypasses all checks; fast but may cause duplicates in Photos.
- **`--since DATE`** — skips files that haven't changed since the given date.
- **`--save-every N`** — controls how often the ID/Hash log is flushed to disk.

### Dedup modes

| Mode | How it works | Speed |
|---|---|---|
| `none` | Always upload | Fastest |
| `filename` | Skip if the filename already exists in Photos (uses cache) | Fast |
| `hash` | Download first, skip if same content was uploaded before | Slower |
| `filename+hash` | Skip if *either* filename or hash matches | Most thorough |

### CLI reference

| Flag | Default | Description |
|---|---|---|
| `--folder ID` | – | Drive folder ID to sync |
| `--all` | – | Sync all media in your entire Drive |
| `--since YYYY-MM-DD` | – | Only files modified after this date |
| `--workers N` | `10` | Number of parallel upload threads |
| `--dedup-mode MODE` | `filename` | Dedup strategy (see table above) |
| `--skip-dedup` | off | Alias for `--dedup-mode none` |
| `--refresh-cache` | off | Force full Photos library rescan |
| `--save-every N` | `25` | Save progress to disk every N uploads |
| `--limit N` | – | Process at most N files (useful for testing) |
| `--dry-run` | off | Show what would happen without uploading |

---

## Optional – Run on a Free Azure VM (`free-vm.bicep`)

Syncing a large library can take hours. The `free-vm.bicep` template spins up
a **Standard_B1s** Ubuntu VM (eligible for Azure's 750 free hours/month on new
accounts) so you can kick off the sync, close your laptop, and let Azure handle it.

### What the template creates

- Ubuntu 24.04 LTS VM (`Standard_B1s`, 1 vCPU / 1 GB RAM)
- 64 GB Premium SSD OS disk (covered by the free tier's 2 × 64 GB disk allowance)
- Virtual network, subnet, public IP, and NIC
- Network security group with SSH (port 22) open

### Deploy

```bash
# 1. Log in and set your subscription
az login
az account set --subscription "<your-subscription-id>"

# 2. Create a resource group
az group create --name wind-sync-rg --location eastus

# 3. Deploy the template (paste your SSH public key when prompted)
az deployment group create \
  --resource-group wind-sync-rg \
  --template-file free-vm.bicep \
  --parameters sshPublicKey="$(cat ~/.ssh/id_rsa.pub)"

# 4. Grab the public IP from the deployment output and SSH in
ssh azureuser@<public-ip>
```

> **Tip:** If you don't have an SSH key yet, generate one with `ssh-keygen -t rsa -b 4096`.

### Run the sync on the VM

```bash
# On the VM — install Python and dependencies
sudo apt update && sudo apt install -y python3-pip
pip3 install google-api-python-client google-auth google-auth-oauthlib requests

# Create tool directory
mkdir -p ~/tools/drive2photos

# Upload your credentials (from your local machine)
scp credentials.json azureuser@<public-ip>:~/tools/
scp tools/drive2photos/drive_to_photos_sync.py azureuser@<public-ip>:~/tools/drive2photos/

# Start the sync in a tmux session so it survives SSH disconnects
sudo apt install -y tmux
tmux new -s sync
cd ~/tools/drive2photos
python3 drive_to_photos_sync.py --all --workers 10

# Detach from tmux: Ctrl+B then D
# Re-attach later:  tmux attach -t sync
```

### Clean up when done

```bash
az group delete --name wind-sync-rg --yes --no-wait
```

This deletes the VM and all associated resources, stopping any further charges.
