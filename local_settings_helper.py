"""Generate local.settings.json from .env for local Azure Functions development."""

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
    },
}

with open("local.settings.json", "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
