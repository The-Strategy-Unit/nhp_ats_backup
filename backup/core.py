"""
Core backup logic for NHP Azure Table Storage.

Usage (manual):
    python -m backup.core

Environment variables required:
    AZURE_STORAGE_ACCOUNT_NAME  : Storage account name
    MODEL_RUNS_TABLE_NAME       : Table to back up
    BACKUP_CONTAINER_NAME       : Blob container for backups (e.g. 'nhp-backups')

Backup layout in blob storage:
    ats-backups/YYYY-MM-DDTHH:MMZ.json   — snapshot
    ats-backups/status.json              — latest run status (read by nhp_ats_tui)

Restore (from latest snapshot):
    1. Identify latest snapshot:
         az storage blob list --account-name <account> --container-name <container> \
             --prefix ats-backups/ --query "[].name" -o tsv | sort | tail -1
    2. Download:
         az storage blob download --account-name <account> \
             --container-name <container> --name ats-backups/<filename> --file restore.json
    3. Re-create table if missing:
         az storage table create --name <table> --account-name <account>
    4. Restore entities:
         python -m backup.core --restore restore.json
"""

import json
import logging
import os
from datetime import UTC, datetime

from azure.data.tables import TableClient, TableServiceClient
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

load_dotenv()

BACKUP_PREFIX = ""
STATUS_BLOB = "status.json"
MAX_SNAPSHOTS = 7


def _get_env(name: str) -> str:
    """Retrieve a required environment variable, raising clearly if absent."""
    value = os.environ.get(name)
    if not value:
        raise EnvironmentError(f"Required environment variable not set: {name}")
    return value


def _credential() -> DefaultAzureCredential:
    """
    Return a DefaultAzureCredential,
    using managed identity in Azure and CLI auth locally.
    """
    return DefaultAzureCredential()


def _table_client(credential: DefaultAzureCredential) -> TableClient:
    """Build an authenticated TableClient from environment config."""
    account = _get_env("AZURE_STORAGE_ACCOUNT_NAME")
    table = _get_env("MODEL_RUNS_TABLE_NAME")
    endpoint = f"https://{account}.table.core.windows.net"
    return TableClient(endpoint=endpoint, table_name=table, credential=credential)


def _blob_client(credential: DefaultAzureCredential) -> BlobServiceClient:
    """Build an authenticated BlobServiceClient from environment config."""
    account = _get_env("AZURE_STORAGE_ACCOUNT_NAME")
    endpoint = f"https://{account}.blob.core.windows.net"
    return BlobServiceClient(account_url=endpoint, credential=credential)


def _fetch_entities(table_client: TableClient) -> list[dict]:
    """Fetch all entities from the table as plain dicts."""
    return [dict(e) for e in table_client.list_entities()]


def _upload_blob(
    blob_service: BlobServiceClient, container: str, name: str, data: str
) -> None:
    """Upload a string payload to a blob, overwriting if it already exists."""
    client = blob_service.get_blob_client(container=container, blob=name)
    client.upload_blob(data, overwrite=True)


def _prune_snapshots(blob_service: BlobServiceClient, container: str) -> None:
    """Delete oldest snapshots, keeping only the last MAX_SNAPSHOTS."""
    container_client = blob_service.get_container_client(container)
    blobs = sorted(
        [
            b.name
            for b in container_client.list_blobs(name_starts_with=BACKUP_PREFIX)
            if b.name != STATUS_BLOB
        ]
    )
    for old in blobs[:-MAX_SNAPSHOTS]:
        blob_service.get_blob_client(container=container, blob=old).delete_blob()
        logging.info("Pruned old snapshot: %s", old)


def run_backup() -> None:
    """Run a full backup cycle: snapshot → upload → prune → write status."""
    started_at = datetime.now(UTC)
    container = _get_env("BACKUP_CONTAINER_NAME")
    credential = _credential()

    logging.info("Connecting to table...")
    table = _table_client(credential)
    entities = _fetch_entities(table)
    entity_count = len(entities)
    logging.info("Fetched %d entities.", entity_count)

    snapshot_name = f"{BACKUP_PREFIX}{started_at.strftime('%Y-%m-%dT%H:%MZ')}.json"
    blob_service = _blob_client(credential)

    logging.info("Uploading snapshot: %s", snapshot_name)
    _upload_blob(
        blob_service,
        container,
        snapshot_name,
        json.dumps(entities, default=str, indent=2),
    )

    _prune_snapshots(blob_service, container)

    status = {
        "status": "success",
        "timestamp": started_at.isoformat(),
        "entity_count": entity_count,
        "latest_snapshot": snapshot_name,
    }
    _upload_blob(blob_service, container, STATUS_BLOB, json.dumps(status, indent=2))
    logging.info("Backup complete. status.json updated.")


def run_restore(snapshot_path: str) -> None:
    """
    Restore all entities from a local JSON snapshot file.

    Args:
        snapshot_path: Path to a downloaded snapshot JSON file.

    The target table is re-created if it does not exist.
    Existing entities with matching keys are overwritten (upsert).
    Prints before/after entity counts for verification.
    """
    credential = _credential()
    account = _get_env("AZURE_STORAGE_ACCOUNT_NAME")
    table_name = _get_env("MODEL_RUNS_TABLE_NAME")
    endpoint = f"https://{account}.table.core.windows.net"

    # Ensure table exists
    service = TableServiceClient(endpoint=endpoint, credential=credential)
    service.create_table_if_not_exists(table_name)

    table = _table_client(credential)

    before_count = sum(1 for _ in table.list_entities())
    logging.info("Entities before restore: %d", before_count)

    with open(snapshot_path) as f:
        entities = json.load(f)

    for entity in entities:
        table.upsert_entity(entity)

    after_count = sum(1 for _ in table.list_entities())
    logging.info("Entities after restore: %d", after_count)
    logging.info("Restore complete. %d entities upserted.", len(entities))


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="NHP ATS backup/restore tool.")
    parser.add_argument(
        "--restore", metavar="FILE", help="Restore from a local snapshot JSON file."
    )
    args = parser.parse_args()

    if args.restore:
        run_restore(args.restore)
    else:
        run_backup()
