# NHP ATS Backup

Daily backup of Azure Table Storage to blob storage with 7-day retention.

## Quick Start

```bash
# Install dependencies
uv sync

# Run locally (requires env vars)
AZURE_STORAGE_ACCOUNT_NAME=... \
MODEL_RUNS_TABLE_NAME=... \
BACKUP_CONTAINER_NAME=... \
uv run python -m backup.core

# Run tests
uv run pytest -v
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `AZURE_STORAGE_ACCOUNT_NAME` | Storage account name |
| `MODEL_RUNS_TABLE_NAME` | Table to back up |
| `BACKUP_CONTAINER_NAME` | Blob container for backups |

## Deploy

With Azure CLI:

```bash
zip -r function.zip . -x "*.git*" "*tests*" "*.venv__*"
az functionapp deployment source config-zip \
  --resource-group <rg> --name <app> --src function.zip
```

## Restore

1. Download a snapshot: `az storage blob download ...`
2. Run: `uv run python -m backup.core --restore snapshot.json`
