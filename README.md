# NHP ATS Backup

Backup and restore NHP Azure Table Storage (ATS) table via JSON snapshots in Blob Storage.

## Quick ops

```bash
# Log in (required for both login and managed identity flows)
az login

# Backup PROD table
uv run --env-file .env python -m backup.core

# Restore (interactive - prompts before each step)
uv run --env-file .env python -m backup.cli

# Restore from a specific snapshot
uv run --env-file .env python -m backup.cli --restore-date 2026-06-17

# Back up from a specific table
uv run --env-file .env python -m backup.cli --source-table <source_table_name>

# Restore to a specific table
uv run --env-file .env python -m backup.cli --target-table <target_table_name> # table must exist
```

**Note on restore flows:**
- `backup.cli` is the convenient path: it downloads the snapshot from Blob Storage and restores it in one command.
- `backup.core --restore <file>` is the low-level path for restoring a snapshot you already have on disk (e.g. downloaded with `az storage blob download`).

## Contents

- [Backup](#backup)
- [Restore](#restore)
- [Configuration](#configuration)
- [Deploy a new Function App](#deploy-a-new-function-app)
- [Local development](#local-development)
- [Developer notes](#developer-notes)

---

## Backup

The Function App runs automatically at **02:00 UTC daily**. A snapshot is a full JSON copy with EDM type tags (DateTime, Int64 - ensures round-trip fidelity).

### Trigger manually (cloud)

```bash
curl -X POST https://<AZURE_FUNCTION_APP_NAME>.azurewebsites.net/api/nhp-ats-backup-dev \
  -H "Content-Type: application/json"
```

### Run locally

```bash
# PROD table (default)
uv run --env-file .env python -m backup.core

# Different table
uv run --env-file .env python -m backup.core --source-table other-table
```

**What happens:** fetch entities → tag EDM types → upload `YYYY-MM-DDTHH:MMZ.json` → validate count → prune old snapshots (7 daily, 6 monthly) → write `status.json`.

---

## Restore

### Interactive (recommended)

Prompts you first, picks the latest snapshot from `status.json`:

```bash
uv run --env-file .env python -m backup.cli
```

### From a specific snapshot

```bash
# Download from Blob Storage
SNAPSHOT=$(az storage blob list \
  --account-name "$BACKUP_STORAGE_ACCOUNT_NAME" \
  --container-name "$BACKUP_CONTAINER_NAME" \
  --query "[].name" -o tsv | grep -E '^\d{4}-' | sort | tail -1)

az storage blob download \
  --account-name "$BACKUP_STORAGE_ACCOUNT_NAME" \
  --container-name "$BACKUP_CONTAINER_NAME" \
  --name "$SNAPSHOT" --file snapshot.json

# Restore into a table
uv run --env-file .env python -m backup.core \
  --restore snapshot.json --target-table <table_name>
```

**Note:** Before restoring, all existing entities are deleted and the snapshot is upserted. The result is identical to the snapshot - no partial merges.

---

## Configuration

Create `.env` in the repo root:

```bash
# Source (your ATS)
SOURCE_RESOURCE_GROUP_NAME=<rg>
SOURCE_STORAGE_ACCOUNT_NAME=<storage-name>     # 3-24 lowercase chars

# Backup destination
BACKUP_RESOURCE_GROUP_NAME=<rg>
BACKUP_STORAGE_ACCOUNT_NAME=<storage-name>     # 3-24 lowercase chars
BACKUP_CONTAINER_NAME=<container-name>

# Function App
FUNCTION_APP_RESOURCE_GROUP_NAME=<rg>
FUNCTION_APP_STORAGE_ACCOUNT_NAME=<storage-name> # 3-24 lowercase chars
AZURE_FUNCTION_APP_NAME=<app-name>             # globally unique
AZURE_LOCATION=<region>                          # e.g. ukwest

# Tables
PROD_TABLE_NAME=<prod-table>
DEV_TABLE_NAME=<dev-table>

# Optional (defaults shown)
# AZURE_STORAGE_AUTH_MODE=login
# AZURE_SKU=Standard_LRS
# AZURE_STORAGE_KIND=StorageV2
# AZURE_PYTHON_VERSION=3.13
# AZURE_FUNCTION_INSTANCE_MEMORY=2048
```

---

## Deploy a new Function App

### One command (recommended)

```bash
uv run deploy.py --yes
```

Creates resource groups, storage accounts, Function App, app settings, role assignments, and publishes.

### Manual (step by step)

#### 1. Requirements

```bash
uv pip compile pyproject.toml -o requirements.txt
```

Create `.funcignore`:

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

#### 2. Function App resources

```bash
az group create --name "$FUNCTION_APP_RESOURCE_GROUP_NAME" --location "$AZURE_LOCATION"

az storage account create \
  --name "$FUNCTION_APP_STORAGE_ACCOUNT_NAME" \
  --resource-group "$FUNCTION_APP_RESOURCE_GROUP_NAME" \
  --location "$AZURE_LOCATION" \
  --sku "$AZURE_SKU" --kind "$AZURE_STORAGE_KIND"

az functionapp create \
  --resource-group "$FUNCTION_APP_RESOURCE_GROUP_NAME" \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --storage-account "$FUNCTION_APP_STORAGE_ACCOUNT_NAME" \
  --flexconsumption-location "$AZURE_LOCATION" \
  --runtime python --runtime-version "$AZURE_PYTHON_VERSION" \
  --functions-version 4 \
  --instance-memory "$AZURE_FUNCTION_INSTANCE_MEMORY"
```

#### 3. Backup resources

```bash
az group create --name "$BACKUP_RESOURCE_GROUP_NAME" --location "$AZURE_LOCATION"

az storage account create \
  --name "$BACKUP_STORAGE_ACCOUNT_NAME" \
  --resource-group "$BACKUP_RESOURCE_GROUP_NAME" \
  --location "$AZURE_LOCATION" \
  --sku "$AZURE_SKU" --kind "$AZURE_STORAGE_KIND"

az storage container create \
  --name "$BACKUP_CONTAINER_NAME" \
  --account-name "$BACKUP_STORAGE_ACCOUNT_NAME" --auth-mode login
```

#### 4. App settings

```bash
az functionapp config appsettings set \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --resource-group "$FUNCTION_APP_RESOURCE_GROUP_NAME" \
  --settings \
    "SOURCE_STORAGE_ACCOUNT_NAME=$SOURCE_STORAGE_ACCOUNT_NAME" \
    "SOURCE_RESOURCE_GROUP_NAME=$SOURCE_RESOURCE_GROUP_NAME" \
    "PROD_TABLE_NAME=$PROD_TABLE_NAME" \
    "DEV_TABLE_NAME=$DEV_TABLE_NAME" \
    "BACKUP_STORAGE_ACCOUNT_NAME=$BACKUP_STORAGE_ACCOUNT_NAME" \
    "BACKUP_CONTAINER_NAME=$BACKUP_CONTAINER_NAME"
```

#### 5. Permissions (cross-RG access)

```bash
PRINCIPAL_ID=$(az functionapp identity show \
  --name "$AZURE_FUNCTION_APP_NAME" \
  --resource-group "$FUNCTION_APP_RESOURCE_GROUP_NAME" \
  --query principalId -o tsv)

SUBSCRIPTION_ID=$(az account show --query id -o tsv)

az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Table Data Contributor" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$SOURCE_RESOURCE_GROUP_NAME/providers/Microsoft.Storage/storageAccounts/$SOURCE_STORAGE_ACCOUNT_NAME"

az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$BACKUP_RESOURCE_GROUP_NAME/providers/Microsoft.Storage/storageAccounts/$BACKUP_STORAGE_ACCOUNT_NAME"
```

#### 6. Publish

```bash
func azure functionapp publish "$AZURE_FUNCTION_APP_NAME"
```

The app exposes:

| Function | Trigger | Route / Schedule |
|-----|-----|----|
| `nhp_ats_backup` | Timer | `0 0 2 * * *` |
| `nhp_ats_backup_dev` | HTTP | `POST /api/nhp-ats-backup-dev` |

---

## Local development

### Generate `local.settings.json`

```python
import json

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
        "SOURCE_STORAGE_ACCOUNT_NAME": env["SOURCE_STORAGE_ACCOUNT_NAME"],
        "BACKUP_STORAGE_ACCOUNT_NAME": env["BACKUP_STORAGE_ACCOUNT_NAME"],
        "PROD_TABLE_NAME": env["PROD_TABLE_NAME"],
        "BACKUP_CONTAINER_NAME": env["BACKUP_CONTAINER_NAME"],
        "DEV_TABLE_NAME": env["DEV_TABLE_NAME"],
    }
}

with open("local.settings.json", "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
```

Run it: `uv run local_settings_helper.py`

### Run locally

Start Azurite first (the Functions runtime needs local blob/queue/table emulation):

```bash
azurite
```

Then:

```bash
func start
curl http://localhost:7071/admin/functions/nhp_ats_backup -X POST -d '{}'
```

---

## Developer notes

### Files

| File | Purpose |
|-----|-----|
| `backup/core.py` | Core backup/restore (non-interactive) |
| `backup/cli.py` | Interactive CLI |
| `backup/deploy.py` | One-command deployment |
| `function_app.py` | Azure Functions entrypoint |

Snapshots are full copies (7 daily + 6 monthly keepers) - cheaper and simpler than deltas at ~180 KB/snapshot.

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

