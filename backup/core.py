"""
Core backup logic for NHP Azure Table Storage.

Usage (manual):
    python -m backup.core

Environment variables required:
    AZURE_STORAGE_ACCOUNT_NAME  : Storage account name
    PROD_TABLE_NAME       : Table to back up
    BACKUP_CONTAINER_NAME       : Blob container for backups 

Backup layout in blob storage:
    YYYY-MM-DDTHH:MMZ.json   - daily snapshot (JSON, EDM-type-tagged)
    status.json       - latest run status and validation hash (to be read by nhp_ats_tui)

Snapshot strategy: full copy, not delta
    Rationale:
    - Current table: ~100 entries, ~180 KB per JSON snapshot.
    - Seven rolling full snapshots: ~1.3 MB total storage.
    - Azure Blob Storage (hot tier, UK South or UK West) costs £0.0142/GB/month.
    - At this scale, annual storage cost is under 1p - negligible compared
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
    """
    Retrieve a required environment variable, raising clearly if absent.

    Args:
        name: The name of the environment variable to retrieve.

    Returns:
        The value of the environment variable.
    """
    value = os.environ.get(name)
    if not value:
        raise EnvironmentError(f"Required environment variable not set: {name}")

    return value


def _credential() -> DefaultAzureCredential:
    """
    Return a DefaultAzureCredential,
    using managed identity in Azure and CLI auth locally.

    Args:
        None

    Returns:
        An instance of DefaultAzureCredential for authenticating with Azure services.
    """

    return DefaultAzureCredential()


def _table_client(
    credential: DefaultAzureCredential, table_name: str | None = None
) -> TableClient:
    """
    Build a TableClient for the given Azure Table Storage table.

    Args:
        credential: An Azure credential (e.g. DefaultAzureCredential).
        table_name: Explicit table name. If omitted, falls back to PROD_TABLE_NAME.

    Returns:
        A TableClient pointing to https://{account}.table.core.windows.net.
    """
    account = _get_env("AZURE_STORAGE_ACCOUNT_NAME")
    table = table_name if table_name is not None else _get_env("PROD_TABLE_NAME")
    endpoint = f"https://{account}.table.core.windows.net"

    return TableClient(endpoint=endpoint, table_name=table, credential=credential)


def _blob_client(credential: DefaultAzureCredential) -> BlobServiceClient:
    """
    Build an authenticated BlobServiceClient from environment config.

    Args:
        credential: An Azure credential (e.g. DefaultAzureCredential).

    Returns:
        A BlobServiceClient pointing to https://{account}.blob.core.windows.net.
    """
    account = _get_env("AZURE_STORAGE_ACCOUNT_NAME")
    endpoint = f"https://{account}.blob.core.windows.net"

    return BlobServiceClient(account_url=endpoint, credential=credential)


def _fetch_entities(table_client: TableClient) -> list[dict]:
    """
    Fetch all entities from the given table and return them as a list of EDM-tagged dicts.

    Args:
        table_client: An authenticated TableClient for the source table.

    Returns:
        A list of dicts, each representing an entity with EDM type tags for serialization.
    """
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
            entity[key] = {"__type__": edm_type, "value": _serialize_value(value)}
        tagged.append(entity)

    return tagged


def _infer_edm_type(value):
    """
    Guess EDM type from Python value.

    Args:
        value: A Python value (str, int, float, bool, datetime, etc.)

    Returns:
        A string representing the corresponding EDM type.
    """
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


def _serialize_value(value):
    """
    Convert a TablesEntityDatetime to ISO 8601 string for JSON serialization.

    Args:
        value: A Python value, potentially a TablesEntityDatetime.

    Returns:
        The value converted to a JSON-serializable format (ISO string for datetime).
    """
    if type(value).__name__ == "TablesEntityDatetime":
        return value.isoformat()

    return value


def _deserialize_value(edm_type: str, value):
    """
    Convert a value from its EDM type to a Python type.

    Args:
        edm_type: The EDM type string (e.g., "Edm.DateTime", "Edm.Boolean").
        value: The value to convert.

    Returns:
        The value converted to the corresponding Python type.
    """
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
    """
    Upload a string payload to a blob, overwriting if it already exists.

    Args:
        blob_service: An authenticated BlobServiceClient.
        container: The name of the blob container.
        name: The name of the blob to create or overwrite.
        data: The string data to upload to the blob.

    Returns:
        None. Raises exceptions on failure.
    """
    client = blob_service.get_blob_client(container=container, blob=name)
    client.upload_blob(data, overwrite=True)


def _prune_snapshots(blob_service: BlobServiceClient, container: str) -> None:
    """
    Delete oldest snapshots, keeping the last MAX_DAILY_SNAPSHOTS dailies
    plus the latest snapshot from each of the last MAX_MONTHLY_SNAPSHOTS months.

    Args:
        blob_service: An authenticated BlobServiceClient.
        container: The name of the blob container.

    Returns:
        None. Raises exceptions on failure.
    """
    container_client = blob_service.get_container_client(container)
    snapshots = sorted(
        b.name for b in container_client.list_blobs() if b.name != STATUS_BLOB
    )

    if len(snapshots) <= MAX_DAILY_SNAPSHOTS:
        return

    keep = set(snapshots[-MAX_DAILY_SNAPSHOTS:])

    # From older snapshots, keep the latest from each month
    old = snapshots[:-MAX_DAILY_SNAPSHOTS]
    by_month = {}
    for name in old:
        month = name[:7]  # "YYYY-MM"
        by_month.setdefault(month, []).append(name)

    monthly_keepers = sorted(max(names) for names in by_month.values())
    keep.update(monthly_keepers[-MAX_MONTHLY_SNAPSHOTS:])

    for name in snapshots:
        if name not in keep:
            blob_service.get_blob_client(container=container, blob=name).delete_blob()
            logging.info("Pruned old snapshot: %s", name)


def _validate_snapshot(
    blob_service: BlobServiceClient, container: str, name: str, expected_count: int
) -> None:
    """
    Validate that the snapshot blob contains the expected number of entities.

    Args:
        blob_service: An authenticated BlobServiceClient.
        container: The name of the blob container.
        name: The name of the snapshot blob to validate.
        expected_count: The expected number of entities in the snapshot.

    Raises:
        RuntimeError: If the actual count of entities does not match the expected count.
    """
    client = blob_service.get_blob_client(container=container, blob=name)
    data = json.loads(client.download_blob().readall())
    actual_count = len(data)
    if actual_count != expected_count:
        raise RuntimeError(
            f"Validation failed for {name}: expected {expected_count} entities, "
            f"got {actual_count}. Aborting prune and status update."
        )
    logging.info("Validation passed: %d entities in %s.", actual_count, name)


def run_backup(source_table=None) -> None:
    """
    Run a full backup cycle: snapshot → upload → validate → prune → status.

    Args:
        source_table: Optional table name to back up. If None, uses PROD_TABLE_NAME.

    Returns:
        None. Raises exceptions on failure.
    """
    started_at = datetime.now(UTC)
    container = _get_env("BACKUP_CONTAINER_NAME")
    credential = _credential()

    logging.info("Connecting to table...")
    table = _table_client(credential, table_name=source_table)
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

    _validate_snapshot(blob_service, container, snapshot_name, entity_count)
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

    Args:
        table: An authenticated TableClient for the target table.

    Returns:
        The total number of entities deleted.
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


def _get_restore_target() -> str:
    """
    Determine the target table for restore based on the environment.

    Args:
        None
    Returns:
        The name of the target table for restore (DEV_TABLE_NAME or PROD_TABLE_NAME).
    """
    env = os.environ.get("AZURE_FUNCTIONS_ENVIRONMENT", "Development")

    if env == "Development":
        return _get_env("DEV_TABLE_NAME")
    return _get_env("PROD_TABLE_NAME")


def run_restore(snapshot_path: str, target_table=None) -> None:
    """
    Restore all entities from a local JSON snapshot file.

    Args:
        snapshot_path: Path to a downloaded snapshot JSON file.

    The target table is re-created if it does not exist.
    Existing entities with matching keys are overwritten (upsert).
    Prints before/after entity counts for verification.

    Args:
        snapshot_path: Path to the local snapshot JSON file.
        target_table: Optional table name to restore into.
            If None, uses DEV_TABLE_NAME or PROD_TABLE_NAME based on environment.

    Returns:
        None. Raises exceptions on failure.
    """
    credential = _credential()
    account = _get_env("AZURE_STORAGE_ACCOUNT_NAME")
    table_name = target_table if target_table is not None else _get_restore_target()
    endpoint = f"https://{account}.table.core.windows.net"

    # Ensure table exists
    service = TableServiceClient(endpoint=endpoint, credential=credential)
    service.create_table_if_not_exists(table_name)

    table = _table_client(credential, table_name=table_name)

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
                entity[k] = _deserialize_value(v["__type__"], v["value"])
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

    parser.add_argument("--source-table", help="Override table to back up from")
    parser.add_argument("--target-table", help="Override table to restore into")
    args = parser.parse_args()

    if args.restore:
        run_restore(args.restore, target_table=args.target_table)
    else:
        run_backup(source_table=args.source_table)
