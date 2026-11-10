import asyncio
import json
import os
import sys
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Wind Cloud Storage Sync Tools Web UI")

# Ensure static directory exists
os.makedirs("web/frontend", exist_ok=True)

# Mount the static frontend
app.mount("/static", StaticFiles(directory="web/frontend"), name="static")


@app.get("/")
async def root():
    return FileResponse("web/frontend/index.html")


class DriveSyncRequest(BaseModel):
    source: str
    dest: str
    source_path: str = "/"
    dest_path: str = "/"
    file_filter: str = "all"
    dedup_mode: str = "filename+hash"
    album: Optional[str] = None
    move: bool = False
    dry_run: bool = False
    refresh_cache: bool = False
    on_duplicate: str = "skip"
    verbose: bool = False


class MigrationRequest(BaseModel):
    gdrive_folder: str = "root"
    icloud_drive_folder: str = "/"
    takeout_dir: Optional[str] = None
    use_gphotos_api: bool = False
    album: Optional[str] = None
    dedup_mode: str = "filename+hash"
    skip_documents: bool = False
    skip_gdrive_photos: bool = False
    refresh_cache: bool = False
    dry_run: bool = False
    verbose: bool = False


class PhotosSyncRequest(BaseModel):
    folder: Optional[str] = None
    sync_all: bool = False
    workers: int = 10
    dedup_mode: str = "filename"
    dry_run: bool = False


async def run_command_stream(cmd: list[str], cwd: Optional[str] = None):
    """Run a subprocess and yield its stdout/stderr as a stream in real-time."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=env,
    )

    try:
        while True:
            chunk = await process.stdout.read(64)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            yield f"data: {json.dumps({'text': text})}\n\n"
    except asyncio.CancelledError:
        process.terminate()
        yield 'data: {"text": "\\n[Process cancelled by client.]"}\n\n'
        raise

    await process.wait()
    done_msg = f"\n[PROCESS_COMPLETE] Exit Code: {process.returncode}\n"
    yield f"data: {json.dumps({'text': done_msg})}\n\n"


@app.post("/api/sync/drive")
async def trigger_drive_sync(req: DriveSyncRequest):
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "sync_drive.cli",
        "--source",
        req.source,
        "--dest",
        req.dest,
        "--source-path",
        req.source_path,
        "--dest-path",
        req.dest_path,
        "--filter",
        req.file_filter,
        "--dedup-mode",
        req.dedup_mode,
        "--on-duplicate",
        req.on_duplicate,
        "--no-color",
    ]
    if req.album:
        cmd.extend(["--album", req.album])
    if req.refresh_cache:
        cmd.append("--refresh-cache")
    if req.move:
        cmd.append("--move")
    if req.dry_run:
        cmd.append("--dry-run")
    if req.verbose:
        cmd.append("-v")

    return StreamingResponse(run_command_stream(cmd), media_type="text/event-stream")


@app.post("/api/sync/migrate")
async def trigger_migration(req: MigrationRequest):
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "sync_drive.migrate",
        "--gdrive-folder",
        req.gdrive_folder or "root",
        "--icloud-drive-folder",
        req.icloud_drive_folder or "/",
        "--dedup-mode",
        req.dedup_mode,
        "--no-color",
    ]
    if req.takeout_dir and req.takeout_dir.strip():
        cmd.extend(["--takeout-dir", req.takeout_dir.strip()])
    if req.use_gphotos_api:
        cmd.append("--use-gphotos-api")
    if req.album and req.album.strip():
        cmd.extend(["--album", req.album.strip()])
    if req.skip_documents:
        cmd.append("--skip-documents")
    if req.skip_gdrive_photos:
        cmd.append("--skip-gdrive-photos")
    if req.refresh_cache:
        cmd.append("--refresh-cache")
    if req.dry_run:
        cmd.append("--dry-run")
    if req.verbose:
        cmd.append("-v")

    return StreamingResponse(run_command_stream(cmd), media_type="text/event-stream")


@app.post("/api/sync/photos")
async def trigger_photos_sync(req: PhotosSyncRequest):
    cmd = [
        sys.executable,
        "-u",
        "drive_to_photos_sync.py",
        "--workers",
        str(req.workers),
        "--dedup-mode",
        req.dedup_mode,
    ]
    if req.sync_all:
        cmd.append("--all")
    elif req.folder:
        cmd.extend(["--folder", req.folder])

    if req.dry_run:
        cmd.append("--dry-run")

    tools_dir = os.path.join(os.getcwd(), "tools", "drive2photos")
    return StreamingResponse(
        run_command_stream(cmd, cwd=tools_dir), media_type="text/event-stream"
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
