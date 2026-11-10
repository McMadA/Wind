"""Fast multi-threaded trashing for Google Drive files owned by the user."""
from __future__ import annotations

import concurrent.futures
import sys
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from sync_drive.clients.gdrive import GDriveClient

console = Console()


def main() -> int:
    g = GDriveClient()
    console.print("[cyan]Scanning for untrashed files and folders owned by you in Google Drive...[/cyan]")

    # 1. Fetch all untrashed files owned by the user
    untrashed = []
    page_token = None
    while True:
        resp = g._service.files().list(
            q="trashed=false and 'me' in owners",
            pageSize=1000,
            pageToken=page_token,
            fields="nextPageToken, files(id, name, mimeType)",
        ).execute()
        files = resp.get("files", [])
        untrashed.extend(files)
        console.print(f"  Found {len(untrashed)} untrashed items so far...")
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if not untrashed:
        console.print("[bold green]✔ Google Drive is ALREADY 100% clean! Zero untrashed items remaining.[/bold green]")
        return 0

    console.print(f"[bold red]Trashing {len(untrashed)} items with 16 parallel workers...[/bold red]")

    trashed_count = 0
    failed_count = 0

    def trash_one(item: dict) -> tuple[bool, str]:
        try:
            g.trash_file(item["id"])
            return True, item["name"]
        except Exception as exc:
            return False, f"{item['name']}: {exc}"

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold red]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Moving to Trash", total=len(untrashed))

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(trash_one, item) for item in untrashed]
            for fut in concurrent.futures.as_completed(futures):
                ok, info = fut.result()
                if ok:
                    trashed_count += 1
                else:
                    failed_count += 1
                progress.update(task, advance=1)

    console.print(
        f"\n[bold green]✔ Complete![/bold green] {trashed_count} items moved to Google Drive Trash "
        f"({failed_count} failed)."
    )

    # Final verification
    resp = g._service.files().list(
        q="trashed=false and 'me' in owners",
        pageSize=10,
        fields="files(id, name)",
    ).execute()
    remaining = resp.get("files", [])
    if remaining:
        console.print(f"[yellow]Note: {len(remaining)} items still reporting untrashed in Drive index.[/yellow]")
    else:
        console.print("[bold green]✔ VERIFIED: 0 items owned by you remain untrashed in Google Drive![/bold green]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
