import asyncio
import sys
import os
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

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
    source_path: str
    dest_path: str
    move: bool = False
    dry_run: bool = False
    on_duplicate: str = "skip"
    verbose: bool = False

class PhotosSyncRequest(BaseModel):
    folder: Optional[str] = None
    sync_all: bool = False
    workers: int = 10
    dedup_mode: str = "filename"
    dry_run: bool = False

async def run_command_stream(cmd: list[str], cwd: Optional[str] = None):
    """Run a subprocess and yield its stdout/stderr as a stream in real-time."""
    # Force python and tqdm to be unbuffered to ensure we get live output
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=env
    )

    try:
        while True:
            # Read single bytes or small chunks to catch \r without waiting for \n
            chunk = await process.stdout.read(64)
            if not chunk:
                break
            
            # Yield chunked SSE format
            text = chunk.decode('utf-8', errors='replace')
            # Replace carriage returns with a unique token or handle directly
            # For SSE, standard newlines mark the end of a message.
            # We'll split the text and emit it so the browser receives it as data.
            # We must be careful because text might just be a partial string without newline.
            # In SSE, `data: <string>\n\n` is the packet. We can just send the raw text.
            # The browser will concatenate these chunks.
            # Wait, SSE expects properly formatted lines. If we want raw streaming, 
            # we should send each chunk as a separate data packet and let the frontend assemble it.
            # BUT: Server-Sent Events are line-oriented.
            # Better approach: stream characters until \n or \r.
            pass
            
            # Let's fix this properly. 
            # We yield every chunk as a JSON string inside a data payload so \r is preserved.
            import json
            yield f"data: {json.dumps({'text': text})}\n\n"

    except asyncio.CancelledError:
        process.terminate()
        yield "data: {\"text\": \"\\n[Process cancelled by client.]\"}\n\n"
        raise

    await process.wait()
    yield f"data: {json.dumps({'text': f'\\n[PROCESS_COMPLETE] Exit Code: {process.returncode}\\n'})}\n\n"

@app.post("/api/sync/drive")
async def trigger_drive_sync(req: DriveSyncRequest):
    cmd = [
        sys.executable, "-u", "-m", "sync_drive.cli",
        "--source", req.source,
        "--dest", req.dest,
        "--source-path", req.source_path,
        "--dest-path", req.dest_path,
        "--on-duplicate", req.on_duplicate,
        "--no-color"
    ]
    if req.move:
        cmd.append("--move")
    if req.dry_run:
        cmd.append("--dry-run")
    if req.verbose:
        cmd.append("-v")

    return StreamingResponse(run_command_stream(cmd), media_type="text/event-stream")

@app.post("/api/sync/photos")
async def trigger_photos_sync(req: PhotosSyncRequest):
    cmd = [
        sys.executable, "-u", "drive_to_photos_sync.py",
        "--workers", str(req.workers),
        "--dedup-mode", req.dedup_mode
    ]
    if req.sync_all:
        cmd.append("--all")
    elif req.folder:
        cmd.extend(["--folder", req.folder])
        
    if req.dry_run:
        cmd.append("--dry-run")

    tools_dir = os.path.join(os.getcwd(), "tools", "drive2photos")
    return StreamingResponse(run_command_stream(cmd, cwd=tools_dir), media_type="text/event-stream")
