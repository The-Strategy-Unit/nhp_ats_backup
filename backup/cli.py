"""Interactive backup/restore CLI for NHP ATS."""

import argparse
import json
import os
import sys

from backup.core import (
    _blob_client,
    _credential,
    _get_env,
    run_backup,
    run_restore,
)


def _latest_snapshot() -> str:
    """Download status.json, parse it, and return the latest snapshot blob name."""
    cred = _credential()
    blob_service = _blob_client(cred)
    container = _get_env("BACKUP_CONTAINER_NAME")

    status_client = blob_service.get_blob_client(container=container, blob="status.json")
    status_data = json.loads(status_client.download_blob().readall())

    snap_name = status_data.get("latest_snapshot")
    if not snap_name:
        raise ValueError("status.json has no latest_snapshot field")

    snap_client = blob_service.get_blob_client(container=container, blob=snap_name)
    local_path = snap_name
    with open(local_path, "wb") as f:
        f.write(snap_client.download_blob().readall())
    print(f"Downloaded snapshot -> {local_path}")
    return local_path


def _confirm(prompt: str) -> bool:
    """Ask y/N and return True only on an explicit 'y' or 'yes'."""
    reply = input(f"{prompt} ").strip().lower()
    return reply in ("y", "yes")


def main() -> int:
    """Prompt for backup and/or restore, then execute."""
    parser = argparse.ArgumentParser(
        description="Interactive backup/restore CLI for NHP ATS."
    )
    parser.add_argument("--source-table", help="Table to back up from")
    parser.add_argument("--target-table", help="Table to restore into")
    args = parser.parse_args()

    # Resolve display names (mirrors core.py defaults)
    source_table = args.source_table or _get_env("PROD_TABLE_NAME")
    if args.target_table:
        target_table = args.target_table
    else:
        env = os.environ.get("AZURE_FUNCTIONS_ENVIRONMENT", "Development")
        target_table = _get_env(
            "DEV_TABLE_NAME" if env == "Development" else "PROD_TABLE_NAME"
        )

    try:
        # --- Backup ---
        if _confirm(f"Back up table [{source_table}]? [y/N]"):
            run_backup(source_table=args.source_table)
            print("Backup complete.")
        else:
            print("Backup skipped.")

        # --- Restore ---
        if _confirm(f"Restore table [{target_table}]? [y/N]"):
            try:
                snapshot_path = _latest_snapshot()
            except Exception as e:
                print(f"Failed to fetch latest snapshot: {e}")
                return 1
            run_restore(snapshot_path, target_table=args.target_table)
            print("Restore complete.")
        else:
            print("Restore skipped.")

        return 0

    except KeyboardInterrupt:
        print("\nAborted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
