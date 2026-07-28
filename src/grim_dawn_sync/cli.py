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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grim-dawn-sync", allow_abbrev=False)
    parser.add_argument("--config", type=Path, default=default_config_path(), help="terminal-local config.local.json")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="read configuration and report T0 readiness")
    return parser


def doctor(config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    return {
        "schema_version": "1.0.0",
        "tool_version": __version__,
        "command": "doctor",
        "read_only": True,
        "config_path": str(config_path),
        "machine_id": config.machine_id,
        "checks": {
            "config": {"ok": True},
            "save_sync_implementation": {"ok": False, "detail": "T0 boundary only; discovery and synchronization are not implemented."},
        },
        "warnings": [
            {"code": "t0_only", "message": "No save, process, Git, or network operation was performed."}
        ],
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
