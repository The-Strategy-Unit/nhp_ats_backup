# NHP ATS Backup

Back up and restore NHP Azure Table Storage (ATS) via JSON snapshots in Blob Storage.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- [Azure CLI](https://docs.microsoft.com/en-us/cli/azure/install-azure-cli)
- [Azure Functions Core Tools](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local) (optional, for local testing)

## Configuration

Create a `.env` file in the repo root by copying and filling in the values below:

```bash
# Azure resources
AZURE_STORAGE_ACCOUNT_NAME=<storage-name>      # Globally unique, lowercase letters+numbers, 3-24 chars
AZURE_RESOURCE_GROUP_NAME=<resource-group>     # Existing resource group where resources are created
AZURE_LOCATION=<region>                        # e.g. australiaeast; pick one near your users

# Storage configuration
AZURE_SKU=Standard_LRS                         # Cheapest; fine for dev/test and small prod workloads
AZURE_STORAGE_KIND=StorageV2                   # General-purpose v2; supports blobs, queues, tables

# Backup targets
PROD_TABLE_NAME=<ats-name>                     # Production Azure Table Storage table to back up
BACKUP_CONTAINER_NAME=<backup-container-name>  # Blob container for JSON snapshots
DEV_TABLE_NAME=<ats-dev-name>                  # Dev/test table for safe restore experiments

# Function App
AZURE_FUNCTION_APP_NAME=<function-app-name>    # Globally unique, used in URLs and deployment
AZURE_PYTHON_VERSION=3.13                      # Max version supported by Azure Functions; 3.14 is still in preview
AZURE_FUNCTION_INSTANCE_MEMORY=2048            # Flex Consumption memory in MB; 2048 is a good default
```

Windows users should omit `export` or use `set` instead.
Notice that `AZURE_SKU` and `AZURE_STORAGE_KIND` default to `Standard_LRS` and `StorageV2` if omitted.

## Quick deploy


### Automated deploy

Run `uv run deploy.py` to interactively create the resource group, storage account,
Function App, backup container, app settings, and publish the functions.
Add `--yes` to skip confirmation prompts.

This project uses **Azure Functions on Flex Consumption** (Python 3.13).

### 1. Generate `requirements.txt`

Azure's remote build uses pip, not uv. Compile a lockfile before every deploy:

```bash
uv pip compile pyproject.toml -o requirements.txt
```

### 2. Prepare `.funcignore`

Ensure `.funcignore` exists so the publish step skips your local venv and build artefacts:

```text
.venv
__pycache__
.git
.env
local.settings.json
.ruff_cache
.pytest_cache
tests/
*.egg-info
*.pyc
.github/
```

### 3. Azure resources and app settings

<details>
<summary><b>Click to expand: create storage account, Function App, and configure app settings</b></summary>

Create the storage account. The name must be globally unique, lowercase letters and numbers only, 3–24 characters.

```bash
az storage account create \
  --name "$AZURE_STORAGE_ACCOUNT_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP_NAME" \
  --location "$AZURE_LOCATION" \
  --sku "$AZURE_SKU" \
  --kind "$AZURE_STORAGE_KIND"
```

Create the backup container:

```bash
az storage container create \
  --name "$BACKUP_CONTAINER_NAME" \
  --account-name "$AZURE_STORAGE_ACCOUNT_NAME" \
  --auth-mode login
```

Create the Function App:

```bash
az functionapp create \
  --resource-group "$AZURE_RESOURCE_GROUP_NAME" \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --storage-account "$AZURE_STORAGE_ACCOUNT_NAME" \
  --flexconsumption-location "$AZURE_LOCATION" \
  --runtime python \
  --runtime-version "$AZURE_PYTHON_VERSION" \
  --functions-version 4 \
  --instance-memory "$AZURE_FUNCTION_INSTANCE_MEMORY"
```

Configure app settings:

```bash
az functionapp config appsettings set \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --resource-group "$AZURE_RESOURCE_GROUP_NAME" \
  --settings \
    "AZURE_STORAGE_ACCOUNT_NAME=$AZURE_STORAGE_ACCOUNT_NAME" \
    "PROD_TABLE_NAME=$PROD_TABLE_NAME" \
    "BACKUP_CONTAINER_NAME=$BACKUP_CONTAINER_NAME" \
    "DEV_TABLE_NAME=$DEV_TABLE_NAME"
```

</details>

### 4. Publish

```bash
func azure functionapp publish "$AZURE_FUNCTION_APP_NAME"
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

## Local testing

<details>
<summary><b>Click to expand</b></summary>

Create `local.settings.json`:

```python
import json
import re

env = {}
with open(".env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")

settings = {
    "IsEncrypted": False,
    "Values": {
        "AzureWebJobsStorage": "UseDevelopmentStorage=true",
        "FUNCTIONS_WORKER_RUNTIME": "python",
        "AZURE_FUNCTIONS_ENVIRONMENT": "Development",
        "AZURE_STORAGE_ACCOUNT_NAME": env["AZURE_STORAGE_ACCOUNT_NAME"],
        "PROD_TABLE_NAME": env["PROD_TABLE_NAME"],
        "BACKUP_CONTAINER_NAME": env["BACKUP_CONTAINER_NAME"],
        "DEV_TABLE_NAME": env["DEV_TABLE_NAME"],
    }
}

with open("local.settings.json", "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
```

Save the above Python code in `local_settings.py` and run it: `uv run local_settings.py`

Run the host:

```bash
func start
```

Invoke the timer function manually:

```bash
curl http://localhost:7071/admin/functions/nhp_ats_backup -X POST -d '{}'
```

</details>

## Developer notes

<details>
<summary><b>Click to expand</b></summary>

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
