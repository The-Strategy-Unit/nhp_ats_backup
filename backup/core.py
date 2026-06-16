"""
Core backup logic for NHP Azure Table Storage.

Usage (manual):
    python -m backup.core

Environment variables required:
    AZURE_STORAGE_ACCOUNT_NAME  : Storage account name
    MODEL_RUNS_TABLE_NAME       : Table to back up
    BACKUP_CONTAINER_NAME       : Blob container for backups 

Backup layout in blob storage:
    YYYY-MM-DDTHH:MMZ.json   — daily snapshot (JSON, EDM-type-tagged)
    status.json              — latest run status and validation hash (to be read by nhp_ats_tui)

Snapshot strategy: full copy, not delta
    Rationale:
    - Current table: ~100 entries, ~180 KB per JSON snapshot.
    - Seven rolling full snapshots: ~1.3 MB total storage.
    - Azure Blob Storage (hot tier, UK South or UK West) costs £0.0142/GB/month.
    - At this scale, annual storage cost is under 1p — negligible compared
      to the engineering time to implement, test, and maintain delta logic.
    - Delta chains introduce replay dependency (corrupt one delta, corrupt
      the whole chain), complicate hash validation, and require base-snapshot
      lifecycle management. These risks are not justified at this scale.
    - Re-evaluate trigger: when annual storage cost exceeds the equivalent
      of one hour of engineering time to implement and maintain delta logic.

Restore (from latest snapshot):
    1. Identify latest snapshot:
         az storage blob list --account-name <account> --container-name <container> \
             --query "[].name" -o tsv | grep -E '^\\d{4}-' | sort | tail -1
    2. Download:
         az storage blob download --account-name <account> \
             --container-name <container> --name <filename> --file restore.json
    3. Re-create table if missing:
         az storage table create --name <table> --account-name <account>
    4. Restore entities:
         uv run python -m backup.core --restore restore.json
"""

import json
import logging
import os
from datetime import UTC, datetime

from azure.data.tables import TableClient, TableServiceClient, TransactionOperation
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

load_dotenv()

STATUS_BLOB = "status.json"
MAX_DAILY_SNAPSHOTS = 7
MAX_MONTHLY_SNAPSHOTS = 6

# Entity Data Model (EDM) types supported by Azure Table Storage.
# https://learn.microsoft.com/en-us/rest/api/searchservice/supported-data-types
SUPPORTED_EDM_TYPES = {
    "Edm.DateTime",
    "Edm.Boolean",
    "Edm.Int32",
    "Edm.Int64",
    "Edm.Double",
    "Edm.String",
}


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
    tagged = []
    for e in table_client.list_entities():
        entity = {}
        for key, value in e.items():
            if key.startswith("__"):
                continue
            # Get EDM type from metadata, or infer from Python type
            edm_type = e.metadata.get("type", {}).get(key) if e.metadata else None
            if not edm_type:
                edm_type = _infer_edm_type(value)
            entity[key] = {"__type__": edm_type, "value": _serialise_value(value)}
        tagged.append(entity)
    return tagged


def _infer_edm_type(value):
    """Guess EDM type from Python value."""
    type_name = type(value).__name__
    if type_name == "TablesEntityDatetime":
        return "Edm.DateTime"
    if isinstance(value, bool):
        return "Edm.Boolean"
    if isinstance(value, int):
        return "Edm.Int64"
    if isinstance(value, float):
        return "Edm.Double"
    return "Edm.String"


def _serialise_value(value):
    if type(value).__name__ == "TablesEntityDatetime":
        return value.isoformat()
    return value


def _deserialise_value(edm_type: str, value):
    if edm_type not in SUPPORTED_EDM_TYPES:
        raise ValueError(f"Unsupported EDM type: {edm_type}")
    if "DateTime" in edm_type:
        return datetime.fromisoformat(value)
    if "Boolean" in edm_type:
        return bool(value)
    if "Int" in edm_type:
        return int(value)
    if "Double" in edm_type:
        return float(value)
    return str(value)


def _upload_blob(
    blob_service: BlobServiceClient, container: str, name: str, data: str
) -> None:
    """Upload a string payload to a blob, overwriting if it already exists."""
    client = blob_service.get_blob_client(container=container, blob=name)
    client.upload_blob(data, overwrite=True)


def _prune_snapshots(blob_service: BlobServiceClient, container: str) -> None:
    """Delete oldest snapshots, keeping only the last MAX_DAILY_SNAPSHOTS."""
    container_client = blob_service.get_container_client(container)
    blobs = sorted(
        [b.name for b in container_client.list_blobs() if b.name != STATUS_BLOB]
    )

    blobs = [b for b in blobs if b != STATUS_BLOB]  # Exclude status from count
    if len(blobs) > MAX_DAILY_SNAPSHOTS:
        for old in blobs[:-MAX_DAILY_SNAPSHOTS]:
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

    snapshot_name = f"{started_at.strftime('%Y-%m-%dT%H:%MZ')}.json"
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


def _batch_clear_table(table: TableClient) -> int:
    """
    Delete all entities using batch transactions. Returns count deleted.

    Why delete: Azure Table Storage has no 'truncate' command. If pre-existing
    rows (different RowKey, or same RowKey with divergent properties) remain
    untouched, the restored table is not identical to the snapshot. We must
    delete everything before inserting the snapshot to guarantee fidelity.

    Why batches: Each transaction is limited to 100 operations and must target
    a single PartitionKey. Batch deletes minimise round-trips (100× fewer API
    calls than individual deletes) and keep the operation atomic per partition.
    At current scale this is negligible; the pattern is chosen for correctness
    at any scale.
    """

    deleted = 0
    batch = []
    current_partition = None

    for entity in table.list_entities():
        pk = entity["PartitionKey"]
        if pk != current_partition and batch:
            table.submit_transaction(batch)
            deleted += len(batch)
            batch = []
        current_partition = pk

        batch.append(
            (
                TransactionOperation.DELETE,
                {"PartitionKey": pk, "RowKey": entity["RowKey"]},
            )
        )

        if len(batch) == 100:
            table.submit_transaction(batch)
            deleted += len(batch)
            batch = []

    if batch:
        table.submit_transaction(batch)
        deleted += len(batch)

    return deleted


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

    cleared = _batch_clear_table(table)
    logging.info("Deleted %d existing entities in batches before restore.", cleared)

    with open(snapshot_path) as f:
        tagged_entities = json.load(f)

    for tagged in tagged_entities:
        # Support both tagged format and legacy plain format
        entity = {}
        for k, v in tagged.items():
            if isinstance(v, dict) and "__type__" in v:
                entity[k] = _deserialise_value(v["__type__"], v["value"])
            else:
                entity[k] = v  # Legacy format: use as-is
        table.upsert_entity(entity)

    after_count = sum(1 for _ in table.list_entities())
    logging.info("Entities after restore: %d", after_count)
    logging.info("Restore complete. %d entities upserted.", len(tagged_entities))


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
