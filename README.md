# NHP ATS Backup

Back up and restore NHP Azure Table Storage (ATS) via JSON snapshots in Blob Storage.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- [Azure CLI](https://docs.microsoft.com/en-us/cli/azure/install-azure-cli)
- [Azure Functions Core Tools](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local) (optional, for local testing)

## Configuration

Create a `.env` file in the repo root:

```bash
export AZURE_STORAGE_ACCOUNT_NAME=<storage_name>
export PROD_TABLE_NAME=<ats_name>
export BACKUP_CONTAINER_NAME=<backup_container_name>
export DEV_TABLE_NAME=<ats_dev_name>
```

Windows users should omit `export` or use `set` instead.

## Quick deploy

This project uses **Azure Functions on Linux Consumption** (Python 3.13).

### 1. Generate `requirements.txt`

Azure's remote build uses pip, not uv. Compile a lockfile before every deploy:

```bash
uv pip compile pyproject.toml -o requirements.txt
```

### 2. Publish

```bash
func azure functionapp publish <function-app-name>
```

The app contains two functions:

| Function | Trigger | Route / Schedule |
|----------|---------|------------------|
| `nhp_ats_backup` | Timer | `0 0 2 * * *` (02:00 UTC daily) |
| `nhp_ats_backup_dev` | HTTP | `POST /api/nhp-ats-backup-dev` |

## Usage

### Create a backup (local)

```bash
uv run --env-file .env python -m backup.core
```

### Restore (interactive)

```bash
uv run --env-file .env python -m backup.cli
```

### Restore a specific date

```bash
uv run --env-file .env python -m backup.cli --restore-date 2026-06-17
```

### Restore to a specific table

```bash
uv run --env-file .env python -m backup.core --restore snapshot.json --target-table <table_name>
```

### Non-interactive restore

```bash
# Identify latest snapshot
SNAPSHOT=$(az storage blob list --account-name "$AZURE_STORAGE_ACCOUNT_NAME" \
  --container-name "$BACKUP_CONTAINER_NAME" --query "[].name" -o tsv \
  | grep -E '^\d{4}-' | sort | tail -1)

# Download
az storage blob download --account-name "$AZURE_STORAGE_ACCOUNT_NAME" \
  --container-name "$BACKUP_CONTAINER_NAME" --name "$SNAPSHOT" \
  --file snapshot.json

# Restore
uv run --env-file .env python -m backup.core --restore snapshot.json --target-table <table_name>
```

## Details

<details>
<summary><b>Azure Function setup (first time)</b></summary>

We use the standard Consumption plan rather than Flex Consumption because of current Python 3.13 portal support and deployment tooling limitations.

```bash
az functionapp create \
  --resource-group <your-rg> \
  --name <new-app-name> \
  --storage-account <your-storage> \
  --os-type Linux \
  --runtime python \
  --runtime-version 3.13 \
  --functions-version 4 \
  --consumption-plan-location <region>
```

Then set app settings:

```bash
az functionapp config appsettings set \
  --name <your-app-name> \
  --resource-group <your-rg> \
  --settings \
    'AZURE_STORAGE_ACCOUNT_NAME=<your-account>' \
    'PROD_TABLE_NAME=<your-table>' \
    'BACKUP_CONTAINER_NAME=<backup-container-name> ' \
    'DEV_TABLE_NAME=<your-dev-table>'
```

</details>

<details>
<summary><b>Local testing</b></summary>

Create `local.settings.json`:

```json
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "AZURE_STORAGE_ACCOUNT_NAME": "<your-account>",
    "PROD_TABLE_NAME": "<your-table>",
    "BACKUP_CONTAINER_NAME": "<your-container>",
    "DEV_TABLE_NAME": "<your-dev-table>"
  }
}
```

Run the host:

```bash
func start
```

Invoke the timer function manually:

```bash
curl http://localhost:7071/admin/functions/nhp_ats_backup -X POST -d '{}'
```

</details>

<details>
<summary><b>Developer notes</b></summary>

- `backup/core.py` — library functions and non-interactive backup/restore
- `backup/cli.py` — interactive workflow and snapshot resolution
- `function_app.py` — Azure Function entrypoint (timer + HTTP)

### Snapshots
- JSON format with EDM type tags for perfect round-trip fidelity
- Stored as `YYYY-MM-DDTHH:MMZ.json`
- Retention: 7 daily snapshots + 6 monthly keepers (see `_prune_snapshots` in `core.py`)

### Tests
```bash
uv run pytest
```

### Lint / Format
```bash
uv run ruff check .
uv run ruff format .
```

### Status reporting
The backup script emits structured logs (success/failure, entity count, latest snapshot) for consumption by `nhp_ats_tui`.

</details>
