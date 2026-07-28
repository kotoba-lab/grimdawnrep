from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from grim_dawn_sync import cli
from grim_dawn_sync.errors import EXIT_CONFIGURATION, EXIT_RECOVERY_REQUIRED, SyncError
from grim_dawn_sync.shortcut import LEGACY_SHORTCUT_NAME, SHORTCUT_NAME, ShortcutAdapter, install_shortcut


def test_parser_requires_apply_and_explicit_restore_bootstrap_arguments() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["restore"])
    with pytest.raises(SystemExit):
        parser.parse_args(["bootstrap"])
    assert parser.parse_args(["restore", "--commit", "abc"]).apply is False
    assert parser.parse_args(["bootstrap", "--source-cloud", "cloud"]).apply is False
    assert parser.parse_args(["install-shortcut"]).apply is False


def test_main_routes_commands_to_their_semantic_handlers(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def handler(name: str):
        def invoke(*args: object, **kwargs: object) -> dict[str, object]:
            calls.append((name, args, kwargs))
            return {"schema_version": "1.0.0", "command": name}
        return invoke

    for name in ("doctor", "status", "recover", "restore", "snapshot", "bootstrap"):
        monkeypatch.setattr(cli, name, handler(name))
    config = tmp_path / "config.local.json"
    commands = [
        ["doctor"], ["status"], ["recover"], ["snapshot"],
        ["restore", "--commit", "c0ffee", "--apply"],
        ["bootstrap", "--source-cloud", str(tmp_path / "cloud"), "--apply"],
    ]
    for command in commands:
        assert cli.main(["--config", str(config), "--json", *command]) == 0
        json.loads(capsys.readouterr().out)
    assert [name for name, _, _ in calls] == ["doctor", "status", "recover", "snapshot", "restore", "bootstrap"]
    assert calls[-2][2] == {"apply": True}
    assert calls[-1][2] == {"apply": True}


def test_install_shortcut_dry_run_never_calls_adapter(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "install_shortcut", lambda _: pytest.fail("dry run must not create a shortcut"))
    assert cli.main(["--json", "install-shortcut"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_restore_and_bootstrap_dry_runs_do_not_write_live_save(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.json"
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="test", stable_scan_retries=1, stable_window_seconds=0)
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_inspect_restore", lambda *_: {"dry_run": True})
    restored: list[bool] = []

    class Vault:
        def preflight(self) -> None: pass
        def extract_save(self, *args: object, **kwargs: object) -> None: pass

    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "restore_from_directory", lambda *args, **kwargs: restored.append(kwargs["apply"]) or {"dry_run": not kwargs["apply"]})
    assert cli.restore(config_path, "commit", apply=False)["dry_run"] is True
    source = tmp_path / "cloud"; source.mkdir()
    assert cli.bootstrap(config_path, source, apply=False)["dry_run"] is True
    assert restored == [False]
    assert not config.save_root.exists()
    assert not (tmp_path / "archives").exists()


def test_shortcut_refuses_same_destination_and_legacy_can_coexist(tmp_path: Path) -> None:
    (tmp_path / SHORTCUT_NAME).touch()
    with pytest.raises(SyncError, match="already exists"):
        install_shortcut(tmp_path, adapter=ShortcutAdapter())
    (tmp_path / SHORTCUT_NAME).unlink()
    (tmp_path / LEGACY_SHORTCUT_NAME).touch()
    calls: list[tuple[Path, str, str]] = []
    class FakeAdapter:
        def create(self, destination: Path, target: str, arguments: str) -> None:
            calls.append((destination, target, arguments))
    assert install_shortcut(tmp_path, adapter=FakeAdapter()) == tmp_path / SHORTCUT_NAME  # type: ignore[arg-type]
    assert calls == [(tmp_path / SHORTCUT_NAME, "grim-dawn-sync", "launch")]


def test_shortcut_propagates_adapter_failure_without_creating_file(tmp_path: Path) -> None:
    class FailingAdapter:
        def create(self, destination: Path, target: str, arguments: str) -> None:
            raise SyncError("shortcut_unavailable", "adapter unavailable", EXIT_CONFIGURATION)

    with pytest.raises(SyncError, match="adapter unavailable"):
        install_shortcut(tmp_path, adapter=FailingAdapter())  # type: ignore[arg-type]
    assert not (tmp_path / SHORTCUT_NAME).exists()


@pytest.mark.parametrize(
    ("error", "next_command"),
    [
        (SyncError("bad_config", "bad", EXIT_CONFIGURATION), "grim-dawn-sync status"),
        (SyncError("recovery_required", "recover", EXIT_RECOVERY_REQUIRED), "grim-dawn-sync recover"),
    ],
)
def test_cli_error_has_exit_code_and_next_command(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: SyncError, next_command: str) -> None:
    monkeypatch.setattr(cli, "status", lambda _: (_ for _ in ()).throw(error))
    assert cli.main(["--json", "status"]) == error.exit_code
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["next_command"] == next_command
