"""Unit tests for backup.cli interactive behaviors."""

import json
from unittest.mock import MagicMock, mock_open, patch

import pytest

import backup.cli
from backup.cli import (
    _confirm,
    _download_snapshot,
    _find_snapshots,
    _resolve_snapshot,
    main,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_blob(name: str) -> MagicMock:
    b = MagicMock()
    b.name = name
    return b


# ---------------------------------------------------------------------------
# _confirm
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reply", ["y", "yes", "Y", "YES", "Yes"])
def test_confirm_returns_true_for_affirmative(reply):
    with patch("builtins.input", return_value=reply):
        assert _confirm("Proceed?") is True


@pytest.mark.parametrize("reply", ["n", "no", "N", "", "maybe", "nope"])
def test_confirm_returns_false_for_non_affirmative(reply):
    with patch("builtins.input", return_value=reply):
        assert _confirm("Proceed?") is False


# ---------------------------------------------------------------------------
# _find_snapshots
# ---------------------------------------------------------------------------


def test_find_snapshots_excludes_status_json():
    blobs = [
        _make_blob("2026-06-01T00:00Z.json"),
        _make_blob("status.json"),
        _make_blob("2026-05-01T00:00Z.json"),
    ]
    with patch("backup.cli._blob_client") as mock_blob_client:
        blob_service = mock_blob_client.return_value
        container_client = MagicMock()
        blob_service.get_container_client.return_value = container_client
        container_client.list_blobs.return_value = blobs

        result = _find_snapshots(MagicMock(), "my-container")

    assert "status.json" not in result
    assert result == ["2026-05-01T00:00Z.json", "2026-06-01T00:00Z.json"]


def test_find_snapshots_filters_by_prefix():
    blobs = [
        _make_blob("2026-06-01T00:00Z.json"),
        _make_blob("2026-06-02T00:00Z.json"),
        _make_blob("2026-05-01T00:00Z.json"),
    ]
    with patch("backup.cli._blob_client") as mock_blob_client:
        blob_service = mock_blob_client.return_value
        container_client = MagicMock()
        blob_service.get_container_client.return_value = container_client
        container_client.list_blobs.return_value = blobs

        result = _find_snapshots(MagicMock(), "my-container", prefix="2026-06")

    assert result == ["2026-06-01T00:00Z.json", "2026-06-02T00:00Z.json"]


def test_find_snapshots_returns_sorted():
    blobs = [
        _make_blob("2026-06-03T00:00Z.json"),
        _make_blob("2026-06-01T00:00Z.json"),
        _make_blob("2026-06-02T00:00Z.json"),
    ]
    with patch("backup.cli._blob_client") as mock_blob_client:
        blob_service = mock_blob_client.return_value
        container_client = MagicMock()
        blob_service.get_container_client.return_value = container_client
        container_client.list_blobs.return_value = blobs

        result = _find_snapshots(MagicMock(), "my-container")

    assert result == [
        "2026-06-01T00:00Z.json",
        "2026-06-02T00:00Z.json",
        "2026-06-03T00:00Z.json",
    ]


def test_find_snapshots_returns_empty_for_no_matches():
    blobs = [_make_blob("2026-06-01T00:00Z.json"), _make_blob("status.json")]
    with patch("backup.cli._blob_client") as mock_blob_client:
        blob_service = mock_blob_client.return_value
        container_client = MagicMock()
        blob_service.get_container_client.return_value = container_client
        container_client.list_blobs.return_value = blobs

        result = _find_snapshots(MagicMock(), "my-container", prefix="2025-01")

    assert result == []


# ---------------------------------------------------------------------------
# _resolve_snapshot
# ---------------------------------------------------------------------------


def test_resolve_snapshot_reads_status_json_when_no_date():
    status_data = {"latest_snapshot": "2026-06-11T02:00Z.json"}
    with (
        patch("backup.cli._credential"),
        patch("backup.cli._get_env", return_value="my-container"),
        patch("backup.cli._blob_client") as mock_blob_client,
    ):
        blob_service = mock_blob_client.return_value
        status_client = MagicMock()
        blob_service.get_blob_client.return_value = status_client
        status_client.download_blob.return_value.readall.return_value = json.dumps(
            status_data
        ).encode()

        result = _resolve_snapshot(None)

    assert result == "2026-06-11T02:00Z.json"


def test_resolve_snapshot_raises_when_status_json_missing_field():
    with (
        patch("backup.cli._credential"),
        patch("backup.cli._get_env", return_value="my-container"),
        patch("backup.cli._blob_client") as mock_blob_client,
    ):
        blob_service = mock_blob_client.return_value
        status_client = MagicMock()
        blob_service.get_blob_client.return_value = status_client
        status_client.download_blob.return_value.readall.return_value = json.dumps(
            {}
        ).encode()

        with pytest.raises(ValueError, match="status.json has no latest_snapshot"):
            _resolve_snapshot(None)


def test_resolve_snapshot_picks_latest_for_date_prefix():
    with (
        patch("backup.cli._credential"),
        patch("backup.cli._get_env", return_value="my-container"),
        patch(
            "backup.cli._find_snapshots",
            return_value=["2026-06-10T00:00Z.json", "2026-06-10T12:00Z.json"],
        ),
    ):
        result = _resolve_snapshot("2026-06-10")

    assert result == "2026-06-10T12:00Z.json"


def test_resolve_snapshot_raises_when_no_match_for_date():
    with (
        patch("backup.cli._credential"),
        patch("backup.cli._get_env", return_value="my-container"),
        patch("backup.cli._find_snapshots", return_value=[]),
    ):
        with pytest.raises(ValueError, match="No snapshot found matching prefix"):
            _resolve_snapshot("2026-06-10")


# ---------------------------------------------------------------------------
# _download_snapshot
# ---------------------------------------------------------------------------


def test_download_snapshot_writes_file_and_returns_safe_path():
    blob_data = b'[{"PartitionKey": "a"}]'
    with (
        patch("backup.cli._credential"),
        patch("backup.cli._get_env", return_value="my-container"),
        patch("backup.cli._blob_client") as mock_blob_client,
        patch("builtins.open", mock_open()) as mock_file,
    ):
        blob_service = mock_blob_client.return_value
        blob_client = MagicMock()
        blob_service.get_blob_client.return_value = blob_client
        blob_client.download_blob.return_value.readall.return_value = blob_data

        result = _download_snapshot("2026-06-11T02:00Z.json")

    # Colons in filenames are replaced with underscores
    assert result == "2026-06-11T02_00Z.json"
    mock_file.assert_called_once_with("2026-06-11T02_00Z.json", "wb")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_backup_and_restore_skipped_on_no():
    with (
        patch("sys.argv", ["cli"]),
        patch("backup.cli._get_env", return_value="some_table"),
        patch("backup.cli._confirm", return_value=False),
    ):
        result = main()

    assert result == 0


def test_main_returns_130_on_keyboard_interrupt():
    with (
        patch("sys.argv", ["cli"]),
        patch("backup.cli._get_env", return_value="some_table"),
        patch("backup.cli._confirm", side_effect=KeyboardInterrupt),
    ):
        result = main()

    assert result == 130


def test_main_backup_runs_when_confirmed():
    with (
        patch("sys.argv", ["cli"]),
        patch("backup.cli._get_env", return_value="some_table"),
        patch("backup.cli._confirm", side_effect=[True, False]),
        patch("backup.cli.run_backup") as mock_backup,
    ):
        result = main()

    assert result == 0
    mock_backup.assert_called_once()


def test_main_restore_runs_when_confirmed():
    with (
        patch("sys.argv", ["cli"]),
        patch("backup.cli._get_env", return_value="some_table"),
        patch("backup.cli._confirm", side_effect=[False, True]),
        patch("backup.cli._resolve_snapshot", return_value="snap.json"),
        patch("backup.cli._download_snapshot", return_value="local_snap.json"),
        patch.object(backup.cli, "run_restore") as mock_restore,
    ):
        result = main()

    assert result == 0
    mock_restore.assert_called_once_with("local_snap.json", target_table=None)


def test_main_returns_1_when_snapshot_fetch_fails():
    with (
        patch("sys.argv", ["cli"]),
        patch("backup.cli._get_env", return_value="some_table"),
        patch("backup.cli._confirm", side_effect=[False, True]),
        patch("backup.cli._resolve_snapshot", side_effect=ValueError("No snapshot")),
    ):
        result = main()

    assert result == 1
