#!/usr/bin/env python3
"""Create Azure resources and deploy the NHP ATS Backup Function App."""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    logging.error("python-dotenv is required. Install with: uv add --dev python-dotenv")
    raise

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_CMD_CACHE: dict[tuple[str, ...], dict | None] = {}


def run(cmd, check=True, capture_output=False, _sensitive_indices=None, **kwargs):
    _sensitive_indices = _sensitive_indices or set()
    logged = ["****" if i in _sensitive_indices else arg for i, arg in enumerate(cmd)]
    logging.info("$ %s", " ".join(logged))
    result = subprocess.run(
        cmd, text=True, capture_output=capture_output, check=False, **kwargs
    )
    if capture_output:
        if result.stdout:
            for line in result.stdout.splitlines():
                logging.info(line)
        if result.stderr:
            for line in result.stderr.splitlines():
                logging.error(line)
    if check and result.returncode != 0:
        raise SystemExit(result.returncode)
    return result


def require_env(names: list[str]) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}")
    return {n: os.environ[n] for n in names}


def ask(prompt, auto_yes):
    if auto_yes:
        logging.info("%s [y/N]: --yes -> y", prompt)
        return True
    resp = input(f"{prompt} [y/N]: ").strip().lower()
    return resp in {"y", "yes"}


def _show_json(cmd):
    key = tuple(cmd)
    if key in _CMD_CACHE:
        return _CMD_CACHE[key]
    result = run(cmd + ["--output", "json"], check=False, capture_output=True)
    if result.returncode != 0:
        _CMD_CACHE[key] = None
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        data = None
    _CMD_CACHE[key] = data
    return data


def resource_group_exists(name):
    return bool(_show_json(["az", "group", "show", "--name", name]))


def storage_account_in_rg(name, rg, location):
    data = _show_json(["az", "storage", "account", "show", "--name", name])
    if not data:
        return False, None
    in_place = (
        data.get("resourceGroup", "").lower() == rg.lower()
        and data.get("primaryLocation", "").lower() == location.lower()
    )
    return in_place, data


def function_app_in_rg(name, rg, location):
    data = _show_json(
        ["az", "functionapp", "show", "--name", name, "--resource-group", rg]
    )
    if not data:
        return False, None
    in_place = (
        data.get("resourceGroup", "").lower() == rg.lower()
        and data.get("location", "").lower() == location.lower()
    )
    return in_place, data


def blob_container_exists(account, container):
    data = _show_json(
        [
            "az",
            "storage",
            "container",
            "show",
            "--name",
            container,
            "--account-name",
            account,
        ]
    )
    return bool(data and data.get("name") == container)


def _validate_storage_name(name: str) -> None:
    if not re.fullmatch(r"[a-z0-9]{3,24}", name):
        raise SystemExit(
            f"Invalid AZURE_STORAGE_ACCOUNT_NAME '{name}': "
            "must be 3-24 lowercase letters or digits."
        )


def _ensure_requirements_txt(root: Path) -> None:
    req = root / "requirements.txt"
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        raise SystemExit("pyproject.toml not found")
    if req.exists() and req.stat().st_mtime >= pyproject.stat().st_mtime:
        logging.info("requirements.txt is up to date")
        return
    logging.info("Compiling requirements.txt from pyproject.toml")
    run(["uv", "pip", "compile", str(pyproject), "-o", str(req)], capture_output=False)


def main():
    parser = argparse.ArgumentParser(description="Deploy NHP ATS Backup to Azure")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    args = parser.parse_args()

    env = require_env(
        [
            "AZURE_RESOURCE_GROUP_NAME",
            "AZURE_LOCATION",
            "AZURE_STORAGE_ACCOUNT_NAME",
            "BACKUP_CONTAINER_NAME",
            "AZURE_FUNCTION_APP_NAME",
            "PROD_TABLE_NAME",
            "DEV_TABLE_NAME",
        ]
    )

    rg = env["AZURE_RESOURCE_GROUP_NAME"]
    location = env["AZURE_LOCATION"]
    storage = env["AZURE_STORAGE_ACCOUNT_NAME"]
    _validate_storage_name(storage)

    sku = os.environ.get("AZURE_SKU", "Standard_LRS")
    kind = os.environ.get("AZURE_STORAGE_KIND", "StorageV2")
    container = env["BACKUP_CONTAINER_NAME"]
    app = env["AZURE_FUNCTION_APP_NAME"]
    py_version = os.environ.get("AZURE_PYTHON_VERSION", "3.13")
    memory = os.environ.get("AZURE_FUNCTION_INSTANCE_MEMORY", "2048")

    # Resource group
    if resource_group_exists(rg):
        logging.warning("Resource group '%s' already exists.", rg)
    else:
        if ask(f"Create resource group '{rg}' in '{location}'?", args.yes):
            run(["az", "group", "create", "--name", rg, "--location", location])
        else:
            raise SystemExit("Aborted.")

    # Storage account
    storage_ok, storage_data = storage_account_in_rg(storage, rg, location)
    if storage_ok:
        logging.warning(
            "Storage account '%s' already exists in %s/%s.", storage, rg, location
        )
    elif storage_data:
        raise SystemExit(
            f"Storage account '{storage}' exists but not in resource group '{rg}' "
            f"or location '{location}'. Aborting."
        )
    else:
        if ask(f"Create storage account '{storage}'?", args.yes):
            run(
                [
                    "az",
                    "storage",
                    "account",
                    "create",
                    "--name",
                    storage,
                    "--resource-group",
                    rg,
                    "--location",
                    location,
                    "--sku",
                    sku,
                    "--kind",
                    kind,
                ],
                capture_output=False,
            )
        else:
            raise SystemExit("Aborted.")

    # Backup container
    if blob_container_exists(storage, container):
        logging.warning("Blob container '%s' already exists.", container)
    else:
        if ask(f"Create blob container '{container}'?", args.yes):
            run(
                [
                    "az",
                    "storage",
                    "container",
                    "create",
                    "--name",
                    container,
                    "--account-name",
                    storage,
                    "--auth-mode",
                    "login",
                ],
                capture_output=False,
            )
        else:
            raise SystemExit("Aborted.")

    # Function App
    app_ok, app_data = function_app_in_rg(app, rg, location)
    if app_ok:
        logging.warning("Function App '%s' already exists in %s/%s.", app, rg, location)
    elif app_data:
        raise SystemExit(
            f"Function App '{app}' exists but not in resource group '{rg}' "
            f"or location '{location}'. Aborting."
        )
    else:
        if ask(f"Create Function App '{app}'?", args.yes):
            run(
                [
                    "az",
                    "functionapp",
                    "create",
                    "--resource-group",
                    rg,
                    "--name",
                    app,
                    "--storage-account",
                    storage,
                    "--flexconsumption-location",
                    location,
                    "--runtime",
                    "python",
                    "--runtime-version",
                    py_version,
                    "--functions-version",
                    "4",
                    "--instance-memory",
                    memory,
                ],
                capture_output=False,
            )
        else:
            raise SystemExit("Aborted.")

    # App settings
    if ask(f"Configure app settings on '{app}'?", args.yes):
        settings = [
            f"AZURE_STORAGE_ACCOUNT_NAME={storage}",
            f"PROD_TABLE_NAME={env['PROD_TABLE_NAME']}",
            f"BACKUP_CONTAINER_NAME={container}",
            f"DEV_TABLE_NAME={env['DEV_TABLE_NAME']}",
        ]
        cmd = [
            "az",
            "functionapp",
            "config",
            "appsettings",
            "set",
            "--name",
            app,
            "--resource-group",
            rg,
            "--settings",
            *settings,
        ]
        sensitive_indices = set(range(len(cmd) - len(settings), len(cmd)))
        run(cmd, capture_output=False, _sensitive_indices=sensitive_indices)

    # Publish
    _ensure_requirements_txt(ROOT)
    if ask(f"Publish functions to '{app}'?", args.yes):
        run(["func", "azure", "functionapp", "publish", app], capture_output=False)

    logging.info("Done.")


if __name__ == "__main__":
    main()
