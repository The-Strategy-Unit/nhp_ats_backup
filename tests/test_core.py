"""Unit tests for backup.core using mocked Azure SDK clients."""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, call, mock_open, patch

import pytest

from backup.core import (
    MAX_SNAPSHOTS,
    _fetch_entities,
    _prune_snapshots,
    run_backup,
    run_restore,
)


# ---------------------------------------------------------------------------
# Constants and fixtures
# ---------------------------------------------------------------------------

ENTITIES = [
    {"PartitionKey": "a", "RowKey": "1"},
    {"PartitionKey": "b", "RowKey": "2"},
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_blob(name: str) -> MagicMock:
    b = MagicMock()
    b.name = name
    return b


# ---------------------------------------------------------------------------
# _fetch_entities
# ---------------------------------------------------------------------------

def test_fetch_entities_returns_plain_dicts():
    table = MagicMock()
    table.list_entities.return_value = ENTITIES
    result = _fetch_entities(table)
    assert result == ENTITIES # should be plain dicts, not SDK Entity objects 


def test_fetch_entities_empty_table():
    table = MagicMock()
    table.list_entities.return_value = []
    assert _fetch_entities(table) == []


# ---------------------------------------------------------------------------
# _prune_snapshots
# ---------------------------------------------------------------------------

def _make_blobs(names):
    return [make_blob(n) for n in names]


def test_prune_deletes_oldest_when_over_limit():
    container_client = MagicMock()
    blob_service = MagicMock()
    blob_service.get_container_client.return_value = container_client

    # 8 snapshots — oldest should be deleted
    names = [f"ats-backups/2026-01-0{i}T00:00Z.json" for i in range(1, 9)]
    container_client.list_blobs.return_value = _make_blobs(names)

    _prune_snapshots(blob_service, "my-container")

    blob_service.get_blob_client.assert_called_once_with(
        container="my-container", blob=names[0]
    )
    blob_service.get_blob_client.return_value.delete_blob.assert_called_once()


def test_prune_does_not_delete_when_at_limit():
    container_client = MagicMock()
    blob_service = MagicMock()
    blob_service.get_container_client.return_value = container_client

    names = [f"ats-backups/2026-01-0{i}T00:00Z.json" for i in range(1, MAX_SNAPSHOTS + 1)]
    container_client.list_blobs.return_value = _make_blobs(names)

    _prune_snapshots(blob_service, "my-container")

    blob_service.get_blob_client.assert_not_called()


def test_prune_excludes_status_blob():
    container_client = MagicMock()
    blob_service = MagicMock()
    blob_service.get_container_client.return_value = container_client

    # status.json + 7 snapshots — nothing should be pruned
    names = [f"ats-backups/2026-01-0{i}T00:00Z.json" for i in range(1, MAX_SNAPSHOTS + 1)]
    names.append("ats-backups/status.json")
    container_client.list_blobs.return_value = _make_blobs(names)

    _prune_snapshots(blob_service, "my-container")

    blob_service.get_blob_client.assert_not_called()


# ---------------------------------------------------------------------------
# run_backup
# ---------------------------------------------------------------------------

@patch("backup.core._prune_snapshots")
@patch("backup.core._blob_client")
@patch("backup.core._table_client")
@patch("backup.core._credential")
@patch(
    "backup.core.datetime",
    **{"now.return_value": datetime(2026, 6, 11, 2, 0, tzinfo=UTC)},
)
@patch.dict(
    "os.environ",
    {
        "AZURE_STORAGE_ACCOUNT_NAME": "myaccount",
        "MODEL_RUNS_TABLE_NAME": "mytable",
        "BACKUP_CONTAINER_NAME": "mycontainer",
    },
)
def test_run_backup_uploads_snapshot_and_status(
    mock_dt, mock_cred, mock_table_client, mock_blob_client, mock_prune
):
    mock_table_client.return_value.list_entities.return_value = [
        {"PartitionKey": "a", "RowKey": "1"}
    ]
    blob_service = mock_blob_client.return_value
    blob_client = MagicMock()
    blob_service.get_blob_client.return_value = blob_client

    run_backup()

    uploaded = [c.args[0] for c in blob_client.upload_blob.call_args_list]
    snapshot_payload = json.loads(uploaded[0])
    status_payload = json.loads(uploaded[1])

    assert snapshot_payload == [{"PartitionKey": "a", "RowKey": "1"}]
    assert status_payload["status"] == "success"
    assert status_payload["entity_count"] == 1
    assert status_payload["latest_snapshot"] == "ats-backups/2026-06-11T02:00Z.json"
    mock_prune.assert_called_once()


# ---------------------------------------------------------------------------
# run_restore
# ---------------------------------------------------------------------------

@patch("backup.core.TableServiceClient")
@patch("backup.core._table_client")
@patch("backup.core._credential")
@patch.dict(
    "os.environ",
    {
        "AZURE_STORAGE_ACCOUNT_NAME": "myaccount",
        "MODEL_RUNS_TABLE_NAME": "mytable",
    },
)
def test_run_restore_upserts_all_entities(mock_cred, mock_table_client, mock_tsc):
    entities = [
        {"PartitionKey": "a", "RowKey": "1"},
        {"PartitionKey": "b", "RowKey": "2"},
    ]
    table = mock_table_client.return_value
    table.list_entities.return_value = iter(entities)

    with patch("builtins.open", mock_open(read_data=json.dumps(entities))):
        run_restore("restore.json")

    assert table.upsert_entity.call_count == 2
    table.upsert_entity.assert_any_call({"PartitionKey": "a", "RowKey": "1"})
    table.upsert_entity.assert_any_call({"PartitionKey": "b", "RowKey": "2"})


@patch("backup.core.TableServiceClient")
@patch("backup.core._table_client")
@patch("backup.core._credential")
@patch.dict(
    "os.environ",
    {
        "AZURE_STORAGE_ACCOUNT_NAME": "myaccount",
        "MODEL_RUNS_TABLE_NAME": "mytable",
    },
)
def test_run_restore_creates_table_if_missing(mock_cred, mock_table_client, mock_tsc):
    table = mock_table_client.return_value
    table.list_entities.return_value = iter([])
    tsc_instance = mock_tsc.return_value

    with patch("builtins.open", mock_open(read_data="[]")):
        run_restore("restore.json")

    tsc_instance.create_table_if_not_exists.assert_called_once_with("mytable")


# ---------------------------------------------------------------------------
# Environment variable handling
# ---------------------------------------------------------------------------

def test_missing_env_var_raises():
    from backup.core import _get_env
    with pytest.raises(EnvironmentError, match="MISSING_VAR"):
        _get_env("MISSING_VAR")
