"""CLI boundary for save synchronization; T0 performs no save or network mutation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from grim_dawn_sync import __version__
from grim_dawn_sync.config import default_config_path, load_config
from grim_dawn_sync.errors import EXIT_OK, SyncError
from grim_dawn_sync.manifest import stable_manifest
from grim_dawn_sync.discovery import cloud_candidates, game_candidates, inspect_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grim-dawn-sync", allow_abbrev=False)
    parser.add_argument("--config", type=Path, default=default_config_path(), help="terminal-local config.local.json")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="read configuration and report T0 readiness")
    return parser


def doctor(config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    save = {"configured": True, "exists": config.save_root.exists(), "manifest": None}
    warnings = [{"code": "t0_only", "message": "No process, Git, or network operation was performed."}]
    if config.save_root.is_dir():
        try:
            manifest = stable_manifest(config.save_root, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
            save["manifest"] = {key: manifest[key] for key in ("root_hash", "file_count", "total_bytes", "character_count")}
        except SyncError as error:
            save["validation"] = error.code
    else:
        warnings.append({"code": "save_root_missing", "message": "Configured save root was not found; nothing was created."})
    return {
        "schema_version": "1.0.0",
        "tool_version": __version__,
        "command": "doctor",
        "read_only": True,
        "config_path": str(config_path),
        "machine_id": config.machine_id,
        "checks": {
            "config": {"ok": True},
            "save_root": save,
            "cloud": cloud_candidates(config.game_install),
            "vault": inspect_path(config.vault_repo),
            "launcher": game_candidates(config.game_install, config.launcher_path),
            "processes": {"status": "unknown", "detail": "T5 process detection is not implemented."},
            "save_sync_implementation": {"ok": False, "detail": "T1 read-only discovery and validation only."},
        },
        "warnings": warnings,
    }


def _render(payload: dict[str, Any], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if "error" in payload:
        return f"{payload['error']['code']}: {payload['error']['message']}\n"
    return "doctor: configuration valid; T0 boundary only (no save, Git, process, or network access).\n"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = doctor(args.config)
        exit_code = EXIT_OK
    except SyncError as error:
        payload = error.as_dict()
        exit_code = error.exit_code
    sys.stdout.write(_render(payload, as_json=args.json))
    return exit_code
