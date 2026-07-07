"""Unit tests for backup.core using mocked Azure SDK clients."""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, mock_open, patch

import pytest

from backup.core import (
    MAX_DAILY_SNAPSHOTS,
    _fetch_entities,
    _prune_snapshots,
    run_backup,
    run_restore,
)

# ---------------------------------------------------------------------------
# Constants and fixtures
# ---------------------------------------------------------------------------


class MockEntity(dict):
    metadata = None


ENTITIES_PLAIN = [
    MockEntity(PartitionKey="a", RowKey="1"),
    MockEntity(PartitionKey="b", RowKey="2"),
]

ENTITY_A = {
    "PartitionKey": {"__type__": "Edm.String", "value": "a"},
    "RowKey": {"__type__": "Edm.String", "value": "1"},
}
ENTITY_B = {
    "PartitionKey": {"__type__": "Edm.String", "value": "b"},
    "RowKey": {"__type__": "Edm.String", "value": "2"},
}

ENTITIES_TAGGED = [ENTITY_A, ENTITY_B]


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


def test_fetch_entities_returns_tagged_dicts():
    table = MagicMock()
    table.list_entities.return_value = ENTITIES_PLAIN
    result = _fetch_entities(table)
    assert result == ENTITIES_TAGGED


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

    # 14 blobs across 2 months:
    #   old = first 7 blobs (one per month Jan-Jul), monthly_keepers[-6:] keeps Feb-Jul
    #   daily keepers = 7 Aug blobs
    #   Jan blob is pruned (oldest month, outside last 6)
    names = (
        [f"2025-{i:02d}-01T00:00Z.json" for i in range(1, 8)]  # Jan-Jul 2025
        + [f"2025-08-{i:02d}T00:00Z.json" for i in range(1, 8)]  # Aug 1-7 2025
    )
    container_client.list_blobs.return_value = _make_blobs(names)

    _prune_snapshots(blob_service, "my-container")

    # The Jan 2025 blob (names[0]) should have been pruned (only one deleted)
    blob_service.get_blob_client.assert_any_call(
        container="my-container", blob="2025-01-01T00:00Z.json"
    )


def test_prune_does_not_delete_when_at_limit():
    container_client = MagicMock()
    blob_service = MagicMock()
    blob_service.get_container_client.return_value = container_client

    names = [f"2026-01-0{i}T00:00Z.json" for i in range(1, MAX_DAILY_SNAPSHOTS + 1)]
    container_client.list_blobs.return_value = _make_blobs(names)

    _prune_snapshots(blob_service, "my-container")

    blob_service.get_blob_client.assert_not_called()


def test_prune_excludes_status_blob():
    container_client = MagicMock()
    blob_service = MagicMock()
    blob_service.get_container_client.return_value = container_client

    # status.json + 7 snapshots — nothing should be pruned
    names = [f"2026-01-0{i}T00:00Z.json" for i in range(1, MAX_DAILY_SNAPSHOTS + 1)]
    names.append("status.json")
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
        "SOURCE_STORAGE_ACCOUNT_NAME": "myaccount",
        "BACKUP_STORAGE_ACCOUNT_NAME": "backupaccount",
        "PROD_TABLE_NAME": "mytable",
        "BACKUP_CONTAINER_NAME": "mycontainer",
        "MODEL_RUNS_TABLE_NAME": "mytable",
    },
)
def test_run_backup_uploads_snapshot_and_status(
    mock_dt, mock_cred, mock_table_client, mock_blob_client, mock_prune
):
    class MockEntity(dict):
        metadata = None

    mock_table_client.return_value.list_entities.return_value = [
        MockEntity(PartitionKey="a", RowKey="1")
    ]
    blob_service = mock_blob_client.return_value
    blob_client = MagicMock()
    blob_service.get_blob_client.return_value = blob_client

    # Set up download_blob().readall() to return the snapshot that was uploaded
    expected_snapshot = json.dumps([ENTITY_A])
    blob_client.download_blob.return_value.readall.return_value = expected_snapshot

    run_backup()

    uploaded = [c.args[0] for c in blob_client.upload_blob.call_args_list]
    snapshot_payload = json.loads(uploaded[0])
    status_payload = json.loads(uploaded[1])

    assert snapshot_payload == [
        {
            "PartitionKey": {"__type__": "Edm.String", "value": "a"},
            "RowKey": {"__type__": "Edm.String", "value": "1"},
        }
    ]

    assert status_payload["status"] == "success"
    assert status_payload["entity_count"] == 1
    assert status_payload["latest_snapshot"] == "2026-06-11T02:00Z.json"
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
        "SOURCE_STORAGE_ACCOUNT_NAME": "myaccount",
        "PROD_TABLE_NAME": "mytable",
        "DEV_TABLE_NAME": "devtable",
    },
)
def test_run_restore_upserts_all_entities(mock_cred, mock_table_client, mock_tsc):
    entities = [
        {"PartitionKey": "a", "RowKey": "1"},
        {"PartitionKey": "b", "RowKey": "2"},
    ]
    table = mock_table_client.return_value
    table.list_entities.side_effect = lambda: iter(entities)  # Fresh iter on each call

    with patch("builtins.open", mock_open(read_data=json.dumps(entities))):
        run_restore("restore.json")

    assert table.upsert_entity.call_count == 2
    table.upsert_entity.assert_any_call({"PartitionKey": "a", "RowKey": "1"})
    table.upsert_entity.assert_any_call({"PartitionKey": "b", "RowKey": "2"})
    assert table.submit_transaction.call_count == 2
    assert (
        len(table.submit_transaction.call_args[0][0]) == 1
    )  # one delete per PartitionKey batch


@patch("backup.core.TableServiceClient")
@patch("backup.core._table_client")
@patch("backup.core._credential")
@patch.dict(
    "os.environ",
    {
        "AZURE_FUNCTIONS_ENVIRONMENT": "Production",
        "SOURCE_STORAGE_ACCOUNT_NAME": "myaccount",
        "PROD_TABLE_NAME": "mytable",
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


@patch("backup.core.TableServiceClient")
@patch("backup.core._table_client")
@patch("backup.core._credential")
@patch.dict(
    "os.environ",
    {
        "AZURE_STORAGE_ACCOUNT_NAME": "myaccount",
        "PROD_TABLE_NAME": "mytable",
        "DEV_TABLE_NAME": "devtable",
    },
)
def test_run_restore_handles_tagged_entities_with_datetime(
    mock_cred, mock_table_client, mock_tsc
):
    """Verify that EDM-tagged JSON (including DateTime) is deserialized correctly."""
    tagged = [
        {
            "PartitionKey": {"__type__": "Edm.String", "value": "pk1"},
            "RowKey": {"__type__": "Edm.String", "value": "rk1"},
            "Timestamp": {
                "__type__": "Edm.DateTime",
                "value": "2026-06-11T02:00:00+00:00",
            },
            "count": {"__type__": "Edm.Int64", "value": 42},
        }
    ]
    table = mock_table_client.return_value
    table.list_entities.return_value = iter([])

    with patch("builtins.open", mock_open(read_data=json.dumps(tagged))):
        run_restore("dummy.json")

    upserted = table.upsert_entity.call_args[0][0]
    assert upserted["PartitionKey"] == "pk1"
    assert upserted["RowKey"] == "rk1"
    assert upserted["count"] == 42
    assert isinstance(upserted["Timestamp"], datetime)
    assert upserted["Timestamp"] == datetime(2026, 6, 11, 2, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Environment variable handling
# ---------------------------------------------------------------------------


def test_missing_env_var_raises():
    from backup.core import _get_env

    with pytest.raises(EnvironmentError, match="MISSING_VAR"):
        _get_env("MISSING_VAR")
