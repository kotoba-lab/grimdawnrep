"""Terminal-local configuration loading and validation for the sync tool."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from typing import Any

from grim_dawn_sync.errors import SyncError


CONFIG_SCHEMA_VERSION = "1.0.0"
DEFAULT_CONFIG_FILE_NAME = "config.local.json"
CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "machine_id",
        "save_root",
        "vault_repo",
        "remote",
        "branch",
        "game_install",
        "launcher_mode",
        "launcher_path",
        "game_process_names",
        "launch_timeout_seconds",
        "stable_window_seconds",
        "stable_scan_retries",
        "offline_policy",
    }
)


@dataclass(frozen=True)
class SyncConfig:
    schema_version: str
    machine_id: str
    save_root: Path
    vault_repo: Path
    remote: str
    branch: str
    game_install: Path
    launcher_mode: str
    launcher_path: Path
    game_process_names: tuple[str, ...]
    launch_timeout_seconds: int
    stable_window_seconds: int
    stable_scan_retries: int
    offline_policy: str

    def public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("save_root", "vault_repo", "game_install", "launcher_path"):
            result[key] = str(result[key])
        result["game_process_names"] = list(self.game_process_names)
        return result


def default_config_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "GrimDawnSaveSync" / DEFAULT_CONFIG_FILE_NAME
    return Path.home() / "AppData" / "Local" / "GrimDawnSaveSync" / DEFAULT_CONFIG_FILE_NAME


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SyncError("invalid_config", f"Configuration field '{key}' must be a non-empty string.")
    return value


def _positive_integer(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SyncError("invalid_config", f"Configuration field '{key}' must be a positive integer.")
    return value


def parse_config(payload: dict[str, Any]) -> SyncConfig:
    if not isinstance(payload, dict):
        raise SyncError("invalid_config", "Configuration must be a JSON object.")
    unknown_keys = sorted(set(payload) - CONFIG_KEYS)
    if unknown_keys:
        raise SyncError("invalid_config", "Configuration contains unsupported fields.")
    if payload.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise SyncError("unsupported_config_schema", f"schema_version must be {CONFIG_SCHEMA_VERSION}.")
    names = payload.get("game_process_names")
    if not isinstance(names, list) or not names or any(not isinstance(name, str) or not name.strip() for name in names):
        raise SyncError("invalid_config", "Configuration field 'game_process_names' must be a non-empty string array.")
    launcher_mode = _required_string(payload, "launcher_mode")
    if launcher_mode != "dpyes":
        raise SyncError("invalid_config", "T0 supports only launcher_mode 'dpyes'.")
    offline_policy = _required_string(payload, "offline_policy")
    if offline_policy != "deny":
        raise SyncError("invalid_config", "T0 supports only offline_policy 'deny'.")
    return SyncConfig(
        schema_version=CONFIG_SCHEMA_VERSION,
        machine_id=_required_string(payload, "machine_id"),
        save_root=Path(_required_string(payload, "save_root")).expanduser(),
        vault_repo=Path(_required_string(payload, "vault_repo")).expanduser(),
        remote=_required_string(payload, "remote"),
        branch=_required_string(payload, "branch"),
        game_install=Path(_required_string(payload, "game_install")).expanduser(),
        launcher_mode=launcher_mode,
        launcher_path=Path(_required_string(payload, "launcher_path")).expanduser(),
        game_process_names=tuple(names),
        launch_timeout_seconds=_positive_integer(payload, "launch_timeout_seconds"),
        stable_window_seconds=_positive_integer(payload, "stable_window_seconds"),
        stable_scan_retries=_positive_integer(payload, "stable_scan_retries"),
        offline_policy=offline_policy,
    )


def load_config(path: Path) -> SyncConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SyncError("config_not_found", "Configuration file was not found.", details={"config_path": str(path)}) from error
    except json.JSONDecodeError as error:
        raise SyncError("invalid_config_json", "Configuration file is not valid JSON.", details={"config_path": str(path)}) from error
    except (OSError, UnicodeError) as error:
        raise SyncError("config_unreadable", "Configuration file could not be read.", details={"config_path": str(path)}) from error
    return parse_config(payload)
