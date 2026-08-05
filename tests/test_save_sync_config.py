from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from grim_dawn_sync import cli
from grim_dawn_sync.config import CONFIG_SCHEMA_VERSION, parse_config
from grim_dawn_sync.errors import SyncError
from grim_dawn_sync.state import STATE_SCHEMA_VERSION, SyncState, parse_state


ROOT = Path(__file__).resolve().parents[1]


def config_payload() -> dict:
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "machine_id": "test-machine",
        "save_root": "C:/temporary/save",
        "vault_repo": "C:/temporary/vault",
        "remote": "origin",
        "branch": "main",
        "game_install": "C:/temporary/game",
        "launcher_mode": "dpyes",
        "launcher_path": "C:/temporary/game/DPYes.exe",
        "game_process_names": ["Grim Dawn.exe"],
        "launch_timeout_seconds": 120,
        "stable_window_seconds": 3,
        "stable_scan_retries": 3,
        "offline_policy": "deny",
    }


def test_parse_config_preserves_terminal_local_values() -> None:
    config = parse_config(config_payload())
    assert config.machine_id == "test-machine"
    assert config.public_dict()["launcher_path"].endswith("DPYes.exe")


def test_parse_config_defaults_selection_policy_to_on_diff() -> None:
    payload = config_payload()
    assert "selection_policy" not in payload
    config = parse_config(payload)
    assert config.selection_policy == "on_diff"
    assert config.public_dict()["selection_policy"] == "on_diff"


def test_parse_config_accepts_schema_1_0_0_without_selection_policy() -> None:
    payload = config_payload()
    payload["schema_version"] = "1.0.0"
    config = parse_config(payload)
    assert config.selection_policy == "on_diff"
    # The tool always reports the current schema version once parsed.
    assert config.schema_version == CONFIG_SCHEMA_VERSION


def test_parse_config_accepts_selection_policy_always() -> None:
    payload = config_payload()
    payload["selection_policy"] = "always"
    config = parse_config(payload)
    assert config.selection_policy == "always"


def test_parse_config_rejects_invalid_selection_policy() -> None:
    payload = config_payload()
    payload["selection_policy"] = "never"
    with pytest.raises(SyncError, match="invalid_config"):
        parse_config(payload)


def test_parse_config_rejects_unsupported_schema_version() -> None:
    payload = config_payload()
    payload["schema_version"] = "9.9.9"
    with pytest.raises(SyncError, match="unsupported_config_schema"):
        parse_config(payload)


def test_config_rejects_unsupported_offline_mode() -> None:
    payload = config_payload()
    payload["offline_policy"] = "allow"
    with pytest.raises(SyncError, match="offline_policy"):
        parse_config(payload)


@pytest.mark.parametrize("key", ["access_token", "remote_url"])
def test_config_rejects_unknown_fields_fail_closed(key: str) -> None:
    payload = config_payload()
    payload[key] = "secret-value"
    with pytest.raises(SyncError, match="unsupported fields"):
        parse_config(payload)


def test_state_round_trip_and_schema_guard() -> None:
    assert parse_state(SyncState().as_dict()) == SyncState()
    with pytest.raises(SyncError):
        parse_state({"schema_version": "9.0.0"})
    with pytest.raises(SyncError, match="exactly"):
        parse_state({"schema_version": STATE_SCHEMA_VERSION})
    assert STATE_SCHEMA_VERSION == "1.0.0"


def test_doctor_reads_temp_config_without_touching_save_or_network(tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(config_payload()), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "grim_dawn_sync", "--config", str(config_path), "--json", "doctor"],
        cwd=ROOT,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["read_only"] is True
    assert result["machine_id"] == "test-machine"
    assert any(item["code"] == "save_root_missing" for item in result["warnings"])
    assert result["checks"]["processes"]["status"] in {"clear", "unknown"}


def test_doctor_missing_save_is_warning_and_creates_nothing(tmp_path: Path) -> None:
    missing_save = tmp_path / "missing-save"
    payload = config_payload(); payload["save_root"] = str(missing_save)
    config_path = tmp_path / "config.local.json"; config_path.write_text(json.dumps(payload), encoding="utf-8")
    completed = subprocess.run([sys.executable, "-m", "grim_dawn_sync", "--config", str(config_path), "--json", "doctor"], cwd=ROOT, env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True)
    result = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert any(item["code"] == "save_root_missing" for item in result["warnings"])
    assert not missing_save.exists()


def test_doctor_reports_only_safe_manifest_summary_for_temp_save(tmp_path: Path) -> None:
    save_root = tmp_path / "save"
    (save_root / "main/private").mkdir(parents=True)
    (save_root / "main/private/player.gdc").write_bytes(b"not parsed by doctor")
    payload = config_payload()
    payload["save_root"] = str(save_root)
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "grim_dawn_sync", "--config", str(config_path), "--json", "doctor"],
        cwd=ROOT, env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True,
    )
    result = json.loads(completed.stdout)
    summary = result["checks"]["save_root"]["manifest"]
    assert summary["file_count"] == 1 and summary["character_count"] == 1
    assert "private" not in completed.stdout


def test_doctor_returns_machine_readable_config_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.local.json"
    completed = subprocess.run(
        [sys.executable, "-m", "grim_dawn_sync", "--config", str(missing), "--json", "doctor"],
        cwd=ROOT,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stdout)["error"]["code"] == "config_not_found"


def test_doctor_normalizes_unreadable_config_to_exit_two(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.json"

    def unreadable(_: Path, *, encoding: str) -> str:
        raise PermissionError("access token must not be shown")

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert cli.main(["--config", str(config_path), "--json", "doctor"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "config_unreadable"
    assert "access token" not in json.dumps(payload)
