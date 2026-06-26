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


def _find_snapshots(cred, container: str, prefix: str = "") -> list[str]:
    """
    Return sorted snapshot names matching optional YYYY-MM-DD prefix.

    Args:
        cred: Azure credential object
        container: Name of the Azure Blob Storage container
        prefix: Optional prefix to filter snapshot names (e.g. "2026-06-11")

    Returns:
        List of snapshot names sorted in ascending order.
    """
    blob_service = _blob_client(cred)
    container_client = blob_service.get_container_client(container)
    snapshots = [
        b.name
        for b in container_client.list_blobs()
        if b.name != "status.json" and b.name.startswith(prefix)
    ]

    return sorted(snapshots)


def _download_snapshot(name: str) -> str:
    """
    Download a specific snapshot to the local filesystem. Return the path.

    Args:
        name: The name of the snapshot blob to download.

    Returns:
        The local file path where the snapshot was saved.
    """
    cred = _credential()
    container = _get_env("BACKUP_CONTAINER_NAME")
    blob_service = _blob_client(cred)
    client = blob_service.get_blob_client(container=container, blob=name)
    local_path = os.path.basename(name).replace("%3A", ":").replace(":", "_")
    with open(local_path, "wb") as f:
        f.write(client.download_blob().readall())
    print(f"Downloaded snapshot -> {local_path}")

    return local_path


def _resolve_snapshot(date_str: str | None) -> str:
    """
    Resolve a --restore-date argument to a specific snapshot filename.

    Args:
        date_str: The date string provided by the user (YYYY-MM-DD or full timestamp).

    Returns:
        The name of the snapshot blob to restore.
    """
    cred = _credential()
    container = _get_env("BACKUP_CONTAINER_NAME")

    if date_str is None:
        # Default: read from status.json
        status_client = _blob_client(cred).get_blob_client(
            container=container, blob="status.json"
        )
        status_data = json.loads(status_client.download_blob().readall())
        snap = status_data.get("latest_snapshot")
        if not snap:
            raise ValueError("status.json has no latest_snapshot field")
        return snap

    # Exact match if full timestamp provided
    candidates = _find_snapshots(cred, container, prefix=date_str)
    if not candidates:
        raise ValueError(f"No snapshot found matching prefix: {date_str}")

    # If multiple match the date prefix, pick the latest (last in sorted order)
    return candidates[-1]


def _confirm(prompt: str) -> bool:
    """
    Ask y/N and return True only on an explicit 'y' or 'yes'.

    Args:
        prompt: The prompt message to display to the user.

    Returns:
        True if the user confirms with 'y' or 'yes', False otherwise.
    """
    reply = input(f"{prompt} ").strip().lower()

    return reply in ("y", "yes")


def main() -> int:
    """
    Prompt for backup and/or restore, then execute.

    Args:
        None

    Returns:
        Exit code: 0 for success, 1 for failure, 130 for keyboard interrupt
    """
    parser = argparse.ArgumentParser(
        description="Interactive backup/restore CLI for NHP ATS."
    )
    parser.add_argument("--source-table", help="Table to back up from")
    parser.add_argument("--target-table", help="Table to restore into")
    parser.add_argument(
        "--restore-date",
        metavar="YYYY-MM-DD or YYYY-MM-DDTHH:MMZ",
        help="Restore a specific snapshot instead of the latest from status.json",
    )
    args = parser.parse_args()

    source_table = args.source_table or _get_env("PROD_TABLE_NAME")
    env = os.environ.get("AZURE_FUNCTIONS_ENVIRONMENT", "Development")
    target_table = args.target_table or _get_env(
        "DEV_TABLE_NAME" if env == "Development" else "PROD_TABLE_NAME"
    )

    try:
        if _confirm(f"Back up table [{source_table}]? [y/N]"):
            run_backup(source_table=args.source_table)
            print("Backup complete.")
        else:
            print("Backup skipped.")

        if _confirm(f"Restore table [{target_table}]? [y/N]"):
            try:
                snapshot_name = _resolve_snapshot(args.restore_date)
                snapshot_path = _download_snapshot(snapshot_name)
                with open(snapshot_path) as f:
                    entity_count = len(json.load(f))
            except Exception as e:
                print(f"Failed to fetch snapshot: {e}")
                return 1

            print(
                f"Snapshot: {snapshot_name} | entities: {entity_count} | target: {target_table}"
            )
            if not _confirm(f"Proceed with restore to {target_table}? [y/N]"):
                print("Restore aborted.")
                return 0

            run_restore(snapshot_path, target_table=args.target_table)
            print("Restore complete.")
        else:
            print("Restore skipped.")

        return 0

    except KeyboardInterrupt:
        print("\nAborted.")
        return 130
    except Exception as e:
        print(f"Failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
