# Wind – Cloud Storage Sync Tools

A collection of sync scripts for moving files between cloud storage services.

- **Cloud Drive Sync** – Generic sync between **OneDrive**, **Google Drive**, **iCloud**, and **Google Photos** with verification.
- **Google Drive → Google Photos (legacy tool)** – multi-threaded bulk upload with dedup.

---

## Tool 1 – Cloud Drive Sync (`sync_drive/`)

### Features

- **Multi-service support** – Sync between any combination of **OneDrive**, **Google Drive**, **iCloud Drive**, and **Google Photos**.
- **Recursive sync** – syncs all files and folders from the source directory.
- **Integrity verification** – Service-specific checksums (MD5 for GDrive, SHA256 for OneDrive) or file size/cache (iCloud, GPhotos) verification after each upload.
- **Progress bars** – real-time download/upload progress with transfer speeds.
- **Colored output** – structured, color-coded log messages in the terminal.
- **Dry-run mode** – preview which files would be synced before transferring.
- **Duplicate handling** – skip, overwrite, or create copies of existing files.
- **Audit log** – every run is saved to a timestamped log file in `logs/`.

### How it works

1. Lists all files in the specified source folder (recursively).
2. Downloads each file to a local temp directory.
3. Recreates the folder structure at the destination and uploads the file.
4. Verifies the upload using the destination's checksum (MD5 for Google Drive, SHA256 for OneDrive).
5. Reports a summary of transferred, verified, and failed files.

### Prerequisites

- Python 3.11+
- A **Microsoft Azure** app registration (for OneDrive / Microsoft Graph access).
- A **Google Cloud** project with the Drive API enabled and an OAuth 2.0 client.
- **Apple ID** and App-Specific Password (for iCloud).

### Getting credentials

#### iCloud (Apple)

1. Sign in to [appleid.apple.com](https://appleid.apple.com/).
2. Go to **Sign-In and Security** > **App-Specific Passwords**.
3. Generate a new password (e.g., "WindSync").
4. Use your Apple ID and this password in your `.env` file.
   - Note: The first time you run with iCloud, you will be prompted for a 2FA code in the terminal.

#### OneDrive (Microsoft Azure)

1. Go to the [Azure App Registrations](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade) portal and sign in.
2. Click **New registration**.
   - **Name**: choose any name (e.g. `WindSync`)
   - **Supported account types**: select *Personal Microsoft accounts only* (or include org accounts if needed)
   - **Redirect URI**: select **Public client/native (mobile & desktop)** and enter `http://localhost:8400`
3. Click **Register**. On the overview page, copy the **Application (client) ID** — this is your `ONEDRIVE_CLIENT_ID`.
4. In the left sidebar go to **Certificates & secrets** > **Client secrets** > **New client secret**. Copy the secret **Value** (not the ID) — this is your `ONEDRIVE_CLIENT_SECRET`.
5. In **API permissions**, click **Add a permission** > **Microsoft Graph** > **Delegated permissions** > search for `Files.ReadWrite` and `Files.ReadWrite.All`, then add them.

#### Google Drive

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

#### Finding your Google Drive folder ID

If you want to sync from/into a specific Google Drive folder instead of the root:

1. Open the folder in [Google Drive](https://drive.google.com) in your browser.
2. The URL will look like: `https://drive.google.com/drive/folders/1aBcDeFgHiJkLmNoPqRsTuVwXyZ`
3. The last part of the URL (`1aBcDeFgHiJkLmNoPqRsTuVwXyZ`) is the folder ID — use it as `GOOGLE_DRIVE_TARGET_FOLDER` in your `.env`.

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

### Upgrading from v0.2

v0.3 changes the OAuth scopes for both Google Drive and OneDrive. After
upgrading you must re-authenticate by deleting the cached token files:

```bash
rm -f token.json onedrive_token_cache.bin
```

The next run will prompt you to sign in again.

### Usage

```bash
# ── Sync between any service (OneDrive, GDrive, iCloud) ──

# Sync from iCloud to Google Drive
python -m sync_drive.cli --source icloud --dest gdrive --source-path /Photos --dest-path root

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

All options can be set via environment variables (`.env`) or CLI flags:

| Env variable                 | CLI flag              | Default              |
| ---------------------------- | --------------------- | -------------------- |
| `ONEDRIVE_CLIENT_ID`         | –                     | *(required)*         |
| `ONEDRIVE_CLIENT_SECRET`     | –                     | *(required)*         |
| `ONEDRIVE_TENANT_ID`         | –                     | `common`             |
| `APPLE_ID`                   | –                     | *(for iCloud)*       |
| `APPLE_PASSWORD`             | –                     | *(for iCloud)*       |
| `SOURCE_SERVICE`             | `--source`            | `onedrive, gdrive, icloud, gphotos` |
| `DEST_SERVICE`               | `--dest`              | `onedrive, gdrive, icloud, gphotos` |
| `SOURCE_PATH`                | `--source-path`       | `/`                  |
| `DEST_PATH`                  | `--dest-path`         | `/`                  |
| `GOOGLE_CREDENTIALS_FILE`    | –                     | `credentials.json`   |
| `TEMP_DIR`                   | `--temp-dir`          | `.sync_temp`         |
| –                            | `--on-duplicate`      | `skip`               |
| –                            | `--dry-run`           | off                  |
| –                            | `--move`              | off                  |
| –                            | `--no-color`          | off                  |
| –                            | `--verbose` / `-v`    | off                  |
| `NO_COLOR`                   | –                     | *(unset)*            |

The `--onedrive-folder` and `--gdrive-folder-id` arguments swap roles depending
on the direction: one is the source and the other is the destination.

Set the `NO_COLOR` environment variable to any value to disable colored output
(follows the [no-color.org](https://no-color.org/) convention).

### Project structure

```
sync_drive/
  __init__.py           # Package version
  cli.py                # CLI entry point with rich logging and progress
  onedrive_client.py    # Microsoft Graph / OneDrive API wrapper
  gdrive_client.py      # Google Drive API wrapper
  sync_engine.py        # Orchestrator with progress bars and checksum verification
```

---

## Tool 2 – Google Drive → Google Photos Sync (`drive2photos/`)

Bulk-uploads photos and videos from Google Drive directly into your Google
Photos library. Designed for large libraries — it resumes interrupted runs,
skips files you've already uploaded, and processes multiple files in parallel.

### Features

- **Multi-threaded uploads** – configurable worker count (`--workers N`, default 10)
- **Three dedup modes** – `filename`, `hash`, or `filename+hash` to avoid re-uploading
- **Photos library cache** – scans your Photos library once and caches results locally; use `--refresh-cache` to force a rescan
- **Resumable** – progress is saved to `uploaded_ids.json` every N files, so a crash or Ctrl+C loses at most a handful of uploads
- **Date filter** – `--since YYYY-MM-DD` to only process recently modified files
- **Dry-run mode** – preview what would be uploaded without touching Photos
- **Metadata preserved** – original Drive filename and timestamps stored in the Photos item description
- **Interactive folder browser** – navigate your Drive hierarchy and pick one or more folders, or pass `--folder ID` / `--all` to skip the prompt

### Prerequisites

```bash
pip install google-api-python-client google-auth google-auth-oauthlib requests
```

You will need a Google Cloud project with **two** APIs enabled:

1. **Google Drive API** – to read your files
2. **Google Photos Library API** – to upload them

#### Setting up Google Cloud credentials

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and create or select a project.
2. Go to **APIs & Services** > **Library** and enable both:
   - **Google Drive API**
   - **Photos Library API**
3. Go to **APIs & Services** > **OAuth consent screen**.
   - Choose **External**, fill in the required fields, and click **Save and Continue**.
   - On the **Scopes** step add:
     - `https://www.googleapis.com/auth/drive.readonly`
     - `https://www.googleapis.com/auth/photoslibrary.readonly`
     - `https://www.googleapis.com/auth/photoslibrary.appendonly`
   - Add your Google account under **Test users**.
4. Go to **APIs & Services** > **Credentials** > **Create Credentials** > **OAuth client ID**.
   - **Application type**: Desktop app
5. Click **Download JSON** and save it as `client_secret.json` in the `drive2photos/` directory.

### State files (auto-created)

| File | Purpose |
|---|---|
| `client_secret.json` | OAuth credentials — you supply this |
| `token.json` | Cached OAuth token — auto-refreshed |
| `uploaded_ids.json` | Drive file IDs that were successfully uploaded |
| `uploaded_hashes.json` | SHA-256 hashes of uploaded content (hash dedup) |
| `photos_filename_cache.json` | Cached Photos library filenames (filename dedup) |

### Usage

```bash
cd drive2photos

# Interactive folder picker (default)
python drive_to_photos_sync.py

# Sync a specific folder by Drive folder ID
python drive_to_photos_sync.py --folder DRIVE_FOLDER_ID

# Sync everything in your entire Drive
python drive_to_photos_sync.py --all --workers 8

# Most thorough dedup (checks filename AND content hash)
python drive_to_photos_sync.py --dedup-mode filename+hash

# Only files modified since a given date
python drive_to_photos_sync.py --all --since 2024-06-01

# Preview what would be uploaded without uploading
python drive_to_photos_sync.py --dry-run --all

# Force a fresh scan of your Photos library
python drive_to_photos_sync.py --refresh-cache
```

### Performance tips

| Goal | Command |
|---|---|
| First-time sync (no existing duplicates) | `python drive_to_photos_sync.py --all --workers 15 --skip-dedup --save-every 100` |
| Fast with light dedup (filename only) | `python drive_to_photos_sync.py --all --workers 15 --dedup-mode filename --save-every 50` |
| Incremental (only new/changed files) | `python drive_to_photos_sync.py --all --workers 15 --since 2025-01-01` |

Key levers:

- **`--workers N`** — default is 10; the Photos API comfortably supports 10–15 concurrent connections. Raising this is the single biggest throughput knob.
- **`--skip-dedup`** — removes all hash computation and Photos library scanning. Safe on a first run where no duplicates can exist.
- **`--save-every 100`** — reduces disk writes from the default (every 25 files) to every 100, cutting I/O overhead.
- **`--since DATE`** — skips files that haven't changed since the given date; huge speedup for incremental runs.

Uploads are sent to Photos in batches of up to 50 items per API request (the maximum the `batchCreate` endpoint accepts), which reduces API round-trips by up to 50× compared to one request per file.

### Dedup modes

| Mode | How it works | Speed |
|---|---|---|
| `none` | Always upload (Photos may end up with duplicates) | Fastest |
| `filename` *(default)* | Skip if the filename already exists anywhere in Photos | Fast |
| `hash` | Download first, compute SHA-256, skip if same content was uploaded before | Slower |
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

# Upload your credentials
scp client_secret.json azureuser@<public-ip>:~/drive2photos/
scp drive_to_photos_sync.py azureuser@<public-ip>:~/drive2photos/

# Start the sync in a tmux session so it survives SSH disconnects
sudo apt install -y tmux
tmux new -s sync
cd ~/drive2photos
python3 drive_to_photos_sync.py --all --workers 10

# Detach from tmux: Ctrl+B then D
# Re-attach later:  tmux attach -t sync
```

### Clean up when done

```bash
az group delete --name wind-sync-rg --yes --no-wait
```

This deletes the VM and all associated resources, stopping any further charges.
