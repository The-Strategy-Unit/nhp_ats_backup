#!/usr/bin/env python3
"""Create Azure resources and deploy the NHP ATS Backup Function App."""

import argparse
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    logging.error("python-dotenv is required. Install with: uv add --dev python-dotenv")
    raise

try:
    ROOT = Path(__file__).resolve().parent.parent
except NameError:
    ROOT = Path.cwd()

load_dotenv(ROOT / ".env")

class _ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        """Return a formatted log string with ANSI color on both level and message.

        Colours: WARNING=amber, ERROR=red, INFO=soft-white, DEBUG=dim-grey.
        """
        level = record.levelno
        colour = {
            logging.WARNING: "\033[38;5;214m",
            logging.ERROR: "\033[31m",
            logging.INFO: "\033[37m",
            logging.DEBUG: "\033[90m",
        }.get(level, "")
        reset = "\033[0m"

        lvl = f"{colour}{record.levelname}{reset}"
        msg = f"{colour}{record.getMessage()}{reset}"

        fmt = self._fmt
        if fmt is None:
            fmt = "%(levelname)s %(message)s"

        return fmt.replace("%(levelname)s", lvl).replace("%(message)s", msg)


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                    handlers=[logging.StreamHandler()])
# Attach color formatter to the root handler
_root_handler = logging.root.handlers[0]
_root_handler.setFormatter(_ColorFormatter("%(levelname)s %(message)s"))

_CMD_CACHE: dict[tuple[str, ...], dict | None] = {}


FUNCIGNORE_CONTENT = """\
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
"""


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


def _cache_key(cmd):
    return tuple(cmd)


def clear_cache(*cmd_fragments: str) -> None:
    for key in list(_CMD_CACHE):
        if all(fragment in key for fragment in cmd_fragments):
            _CMD_CACHE.pop(key, None)


def _show_json(cmd):
    key = _cache_key(cmd)
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


def _norm_loc(loc: str) -> str:
    return loc.lower().replace(" ", "")


def function_app_in_rg(name, rg, location):
    data = _show_json(
        ["az", "functionapp", "show", "--name", name, "--resource-group", rg]
    )
    if not data:
        return False, None
    in_place = data.get("resourceGroup", "").lower() == rg.lower() and _norm_loc(
        data.get("location", "")
    ) == _norm_loc(location)
    return in_place, data


def storage_account_in_rg(name, rg, location):
    data = _show_json(["az", "storage", "account", "show", "--name", name])
    if not data:
        return False, None
    in_place = data.get("resourceGroup", "").lower() == rg.lower() and _norm_loc(
        data.get("primaryLocation", "")
    ) == _norm_loc(location)
    return in_place, data


def blob_container_exists(account, container, auth_mode):
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
            "--auth-mode",
            auth_mode,
        ]
    )
    return bool(data and data.get("name") == container)


def _validate_storage_name(name: str, label: str) -> None:
    if not re.fullmatch(r"[a-z0-9]{3,24}", name):
        raise SystemExit(
            f"Invalid {label} '{name}': must be 3-24 lowercase letters or digits."
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


def _ensure_funcignore(root: Path) -> None:
    path = root / ".funcignore"
    if path.exists():
        logging.info(".funcignore already exists")
        return
    logging.info("Creating .funcignore")
    path.write_text(FUNCIGNORE_CONTENT)


def _get_function_app_principal_id(app, rg):
    data = _show_json(
        ["az", "functionapp", "identity", "show", "--name", app, "--resource-group", rg]
    )
    return data.get("principalId") if data else None


def _role_assignment_exists(assignee, role, scope):
    data = _show_json(
        [
            "az",
            "role",
            "assignment",
            "list",
            "--assignee",
            assignee,
            "--role",
            role,
            "--scope",
            scope,
        ]
    )
    return bool(data)


def main():
    parser = argparse.ArgumentParser(description="Deploy NHP ATS Backup to Azure")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    args = parser.parse_args()

    env = require_env(
        [
            "FUNCTION_APP_RESOURCE_GROUP_NAME",
            "FUNCTION_APP_STORAGE_ACCOUNT_NAME",
            "BACKUP_RESOURCE_GROUP_NAME",
            "BACKUP_STORAGE_ACCOUNT_NAME",
            "BACKUP_CONTAINER_NAME",
            "SOURCE_RESOURCE_GROUP_NAME",
            "SOURCE_STORAGE_ACCOUNT_NAME",
            "AZURE_LOCATION",
            "AZURE_FUNCTION_APP_NAME",
            "PROD_TABLE_NAME",
            "DEV_TABLE_NAME",
        ]
    )

    app_rg = env["FUNCTION_APP_RESOURCE_GROUP_NAME"]
    app_storage = env["FUNCTION_APP_STORAGE_ACCOUNT_NAME"]
    backup_rg = env["BACKUP_RESOURCE_GROUP_NAME"]
    backup_storage = env["BACKUP_STORAGE_ACCOUNT_NAME"]
    source_rg = env["SOURCE_RESOURCE_GROUP_NAME"]
    source_storage = env["SOURCE_STORAGE_ACCOUNT_NAME"]

    location = env["AZURE_LOCATION"]
    app = env["AZURE_FUNCTION_APP_NAME"]
    container = env["BACKUP_CONTAINER_NAME"]

    sku = os.environ.get("AZURE_SKU", "Standard_LRS")
    kind = os.environ.get("AZURE_STORAGE_KIND", "StorageV2")
    py_version = os.environ.get("AZURE_PYTHON_VERSION", "3.13")
    memory = os.environ.get("AZURE_FUNCTION_INSTANCE_MEMORY", "2048")
    auth_mode = os.environ.get("AZURE_STORAGE_AUTH_MODE", "login")

    _validate_storage_name(app_storage, "FUNCTION_APP_STORAGE_ACCOUNT_NAME")
    _validate_storage_name(backup_storage, "BACKUP_STORAGE_ACCOUNT_NAME")
    _validate_storage_name(source_storage, "SOURCE_STORAGE_ACCOUNT_NAME")

    # Function app resource group
    if resource_group_exists(app_rg):
        logging.warning("Resource group '%s' already exists.", app_rg)
    else:
        if ask(f"Create resource group '{app_rg}' in '{location}'?", args.yes):
            run(["az", "group", "create", "--name", app_rg, "--location", location])
            clear_cache("az", "group", "show")
        else:
            raise SystemExit("Aborted.")

    # Function app storage account
    storage_ok, storage_data = storage_account_in_rg(app_storage, app_rg, location)
    if storage_ok:
        logging.warning(
            "Storage account '%s' already exists in %s/%s.", app_storage, app_rg, location
        )
    elif storage_data:
        raise SystemExit(
            f"Storage account '{app_storage}' exists "
            f"but not in resource group '{app_rg}' "
            f"or location '{location}'. Aborting."
        )
    else:
        if ask(f"Create Function App storage account '{app_storage}'?", args.yes):
            run(
                [
                    "az",
                    "storage",
                    "account",
                    "create",
                    "--name",
                    app_storage,
                    "--resource-group",
                    app_rg,
                    "--location",
                    location,
                    "--sku",
                    sku,
                    "--kind",
                    kind,
                ],
                capture_output=False,
            )
            clear_cache("az", "storage", "account", "show")
        else:
            raise SystemExit("Aborted.")

    # Backup resource group
    if resource_group_exists(backup_rg):
        logging.warning("Resource group '%s' already exists.", backup_rg)
    else:
        if ask(f"Create resource group '{backup_rg}' in '{location}'?", args.yes):
            run(["az", "group", "create", "--name", backup_rg, "--location", location])
            clear_cache("az", "group", "show")
        else:
            raise SystemExit("Aborted.")

    # Backup storage account
    backup_ok, backup_data = storage_account_in_rg(backup_storage, backup_rg, location)
    if backup_ok:
        logging.warning(
            "Storage account '%s' already exists in %s/%s.",
            backup_storage,
            backup_rg,
            location,
        )
    elif backup_data:
        raise SystemExit(
            f"Storage account '{backup_storage}' exists "
            f"but not in resource group '{backup_rg}' "
            f"or location '{location}'. Aborting."
        )
    else:
        if ask(f"Create backup storage account '{backup_storage}'?", args.yes):
            run(
                [
                    "az",
                    "storage",
                    "account",
                    "create",
                    "--name",
                    backup_storage,
                    "--resource-group",
                    backup_rg,
                    "--location",
                    location,
                    "--sku",
                    sku,
                    "--kind",
                    kind,
                ],
                capture_output=False,
            )
            clear_cache("az", "storage", "account", "show")
        else:
            raise SystemExit("Aborted.")

    # Backup container
    if blob_container_exists(backup_storage, container, auth_mode):
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
                    backup_storage,
                    "--auth-mode",
                    auth_mode,
                ],
                capture_output=False,
            )
            clear_cache("az", "storage", "container", "show")
        else:
            raise SystemExit("Aborted.")

    # Function App
    app_ok, app_data = function_app_in_rg(app, app_rg, location)
    if app_ok:
        logging.warning(
            "Function App '%s' already exists in %s/%s.", app, app_rg, location
        )
    elif app_data:
        raise SystemExit(
            f"Function App '{app}' exists but not in resource group '{app_rg}' "
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
                    app_rg,
                    "--name",
                    app,
                    "--storage-account",
                    app_storage,
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
            clear_cache("az", "functionapp", "show")
        else:
            raise SystemExit("Aborted.")

    # App settings
    if ask(f"Configure app settings on '{app}'?", args.yes):
        settings = [
            f"SOURCE_STORAGE_ACCOUNT_NAME={source_storage}",
            f"SOURCE_RESOURCE_GROUP_NAME={source_rg}",
            f"PROD_TABLE_NAME={env['PROD_TABLE_NAME']}",
            f"DEV_TABLE_NAME={env['DEV_TABLE_NAME']}",
            f"BACKUP_STORAGE_ACCOUNT_NAME={backup_storage}",
            f"BACKUP_CONTAINER_NAME={container}",
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
            app_rg,
            "--settings",
            *settings,
        ]
        sensitive_indices = set(range(len(cmd) - len(settings), len(cmd)))
        run(cmd, capture_output=False, _sensitive_indices=sensitive_indices)
        clear_cache("az", "functionapp", "show")
    else:
        raise SystemExit(
            "Aborted: app settings are required for the Function App to run."
        )

    # Role assignments for cross-resource-group access
    principal_id = _get_function_app_principal_id(app, app_rg)
    if not principal_id:
        logging.warning(
            """Could not determine Function App managed identity.  
            Skipping role assignments."""
        )
    else:
        account_data = _show_json(["az", "account", "show"])
        subscription_id = account_data.get("id", "") if account_data else ""

        source_scope = (
            f"/subscriptions/{subscription_id}/resourceGroups/{source_rg}"
            f"/providers/Microsoft.Storage/storageAccounts/{source_storage}"
        )
        backup_scope = (
            f"/subscriptions/{subscription_id}/resourceGroups/{backup_rg}"
            f"/providers/Microsoft.Storage/storageAccounts/{backup_storage}"
        )

        if ask(
            f"Grant Function App access to source table '{source_storage}'?", args.yes
        ):
            if not _role_assignment_exists(
                principal_id, "Storage Table Data Contributor", source_scope
            ):
                run(
                    [
                        "az",
                        "role",
                        "assignment",
                        "create",
                        "--assignee-object-id",
                        principal_id,
                        "--assignee-principal-type",
                        "ServicePrincipal",
                        "--role",
                        "Storage Table Data Contributor",
                        "--scope",
                        source_scope,
                    ],
                    capture_output=False,
                )
                logging.info("Role assignment created. Waiting for propagation...")
                time.sleep(30)
            else:
                logging.warning("Role assignment already exists for source table.")

        if ask(
            f"Grant Function App access to backup storage '{backup_storage}'?", args.yes
        ):
            if not _role_assignment_exists(
                principal_id, "Storage Blob Data Contributor", backup_scope
            ):
                run(
                    [
                        "az",
                        "role",
                        "assignment",
                        "create",
                        "--assignee-object-id",
                        principal_id,
                        "--assignee-principal-type",
                        "ServicePrincipal",
                        "--role",
                        "Storage Blob Data Contributor",
                        "--scope",
                        backup_scope,
                    ],
                    capture_output=False,
                )
                logging.info("Role assignment created. Waiting for propagation...")
                time.sleep(30)
            else:
                logging.warning("Role assignment already exists for backup storage.")

    # Publish
    _ensure_requirements_txt(ROOT)
    _ensure_funcignore(ROOT)
    if ask(f"Publish functions to '{app}'?", args.yes):
        run(["func", "azure", "functionapp", "publish", app], capture_output=False)

    logging.info("Done.")


if __name__ == "__main__":
    main()
