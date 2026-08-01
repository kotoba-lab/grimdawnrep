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
    assert parser.parse_args(["preserve"]).apply is False
    assert parser.parse_args(["archive-live", "--apply"]).command == "archive-live"


def test_main_routes_commands_to_their_semantic_handlers(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def handler(name: str):
        def invoke(*args: object, **kwargs: object) -> dict[str, object]:
            calls.append((name, args, kwargs))
            return {"schema_version": "1.0.0", "command": name}
        return invoke

    for name in ("doctor", "status", "recover", "restore", "snapshot", "preserve", "bootstrap"):
        monkeypatch.setattr(cli, name, handler(name))
    config = tmp_path / "config.local.json"
    commands = [
        ["doctor"], ["status"], ["recover"], ["snapshot"], ["preserve", "--apply"],
        ["restore", "--commit", "c0ffee", "--apply"],
        ["restore", "--commit", "c0ffee", "--drill", "--apply"],
        ["bootstrap", "--source-cloud", str(tmp_path / "cloud"), "--apply"],
    ]
    for command in commands:
        assert cli.main(["--config", str(config), "--json", *command]) == 0
        json.loads(capsys.readouterr().out)
    assert [name for name, _, _ in calls] == ["doctor", "status", "recover", "snapshot", "preserve", "restore", "restore", "bootstrap"]
    assert calls[-4][2] == {"apply": True}
    assert calls[-3][2] == {"apply": True, "drill": False}
    assert calls[-2][2] == {"apply": True, "drill": True}
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


def test_preserve_dry_run_only_scans_and_validates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="test", stable_scan_retries=1, stable_window_seconds=0)
    manifest = {"root_hash": "a" * 64, "file_count": 2, "character_count": 1}
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: manifest)
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True})
    monkeypatch.setattr(cli, "_process_preflight", lambda *_a: pytest.fail("dry run must not scan processes"))
    monkeypatch.setattr(cli, "_safe_archive_parent", lambda *_a: pytest.fail("dry run must not create archives"))
    monkeypatch.setattr(cli, "_vault", lambda *_a: pytest.fail("preserve must not use vault"))
    assert cli.preserve(tmp_path / "config.local.json", apply=False) == {
        "schema_version": "1.0.0", "command": "preserve", "file_count": 2,
        "character_count": 1, "dry_run": True, "verified": False,
    }
    assert not (tmp_path / "archives").exists()


def test_preserve_apply_creates_verified_local_archive_without_sync_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="test", stable_scan_retries=1, stable_window_seconds=0)
    manifest = {"root_hash": "b" * 64, "file_count": 1, "character_count": 1}
    copied: list[Path] = []
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: manifest)
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True})
    monkeypatch.setattr(cli, "_process_preflight", lambda *_a: {"status": "stopped"})
    monkeypatch.setattr(cli, "_safe_archive_parent", lambda path: path.mkdir(exist_ok=True))
    def copy(_source: Path, destination: Path, *_args: object) -> None:
        copied.append(destination); destination.mkdir()
    monkeypatch.setattr(cli, "_copy_verified", copy)
    monkeypatch.setattr(cli, "_vault", lambda *_a: pytest.fail("preserve must not use vault"))
    monkeypatch.setattr(cli, "load_state", lambda *_a: pytest.fail("preserve must not load state"))
    monkeypatch.setattr(cli, "save_state_if_unchanged", lambda *_a: pytest.fail("preserve must not save state"))
    result = cli.preserve(tmp_path / "config.local.json", apply=True)
    assert result["verified"] is True and result["dry_run"] is False
    assert copied[0].name.startswith(".preserve-incomplete-")
    assert result["archive_id"] != copied[0].name
    assert (tmp_path / "archives" / result["archive_id"]).is_dir()
    assert copied[0].parent == tmp_path / "archives"


def test_preserve_rejects_running_process_and_invalid_save(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="test", stable_scan_retries=1, stable_window_seconds=0)
    manifest = {"root_hash": "c" * 64, "file_count": 1, "character_count": 1}
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: manifest)
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": False, "classification": "invalid_save"})
    monkeypatch.setattr(cli, "_process_preflight", lambda *_a: None)
    with pytest.raises(SyncError) as error: cli.preserve(tmp_path / "config", apply=True)
    assert error.value.code == "invalid_save"
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True})
    monkeypatch.setattr(cli, "_process_preflight", lambda *_a: (_ for _ in ()).throw(SyncError("game_already_running", "running", 5)))
    with pytest.raises(SyncError) as error: cli.preserve(tmp_path / "config", apply=True)
    assert error.value.code == "game_already_running"


def test_preserve_retains_only_incomplete_stage_when_source_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="test", stable_scan_retries=1, stable_window_seconds=0)
    manifest = {"root_hash": "d" * 64, "file_count": 1, "character_count": 1}
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: manifest)
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True}); monkeypatch.setattr(cli, "_process_preflight", lambda *_a: None)
    monkeypatch.setattr(cli, "_safe_archive_parent", lambda path: path.mkdir(exist_ok=True))
    def changed(_source: Path, destination: Path, *_args: object) -> None:
        destination.mkdir(); (destination / "partial").write_text("x")
        raise SyncError("source_changed", "changed", 3)
    monkeypatch.setattr(cli, "_copy_verified", changed)
    with pytest.raises(SyncError) as error: cli.preserve(tmp_path / "config", apply=True)
    assert error.value.code == "source_changed"
    children = list((tmp_path / "archives").iterdir())
    assert len(children) == 1 and children[0].name.startswith(".preserve-incomplete-")


def test_preserve_process_race_keeps_only_incomplete_stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="test", stable_scan_retries=1, stable_window_seconds=0)
    manifest = {"root_hash": "e" * 64, "file_count": 1, "character_count": 1}
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: manifest)
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True})
    calls = 0
    def preflight(*_args: object) -> None:
        nonlocal calls; calls += 1
        if calls == 3: raise SyncError("game_already_running", "running", 5)
    monkeypatch.setattr(cli, "_process_preflight", preflight); monkeypatch.setattr(cli, "_safe_archive_parent", lambda path: path.mkdir())
    monkeypatch.setattr(cli, "_copy_verified", lambda _source, destination, *_args: destination.mkdir())
    with pytest.raises(SyncError) as error: cli.preserve(tmp_path / "config", apply=True)
    assert error.value.code == "game_already_running" and error.value.details == {}
    children = list((tmp_path / "archives").iterdir())
    assert len(children) == 1 and children[0].name.startswith(".preserve-incomplete-")


def test_preserve_blocks_unsafe_archive_parent_before_copy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="test", stable_scan_retries=1, stable_window_seconds=0)
    manifest = {"root_hash": "f" * 64, "file_count": 1, "character_count": 1}
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: manifest)
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True}); monkeypatch.setattr(cli, "_process_preflight", lambda *_a: None)
    monkeypatch.setattr(cli, "_safe_archive_parent", lambda *_a: (_ for _ in ()).throw(SyncError("unsafe_archive_path", "link", 6)))
    monkeypatch.setattr(cli, "_copy_verified", lambda *_a: pytest.fail("unsafe archive parent must block copy"))
    with pytest.raises(SyncError) as error: cli.preserve(tmp_path / "config", apply=True)
    assert error.value.code == "unsafe_archive_path"


def test_preserve_refuses_replaced_stage_before_publish(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="test", stable_scan_retries=1, stable_window_seconds=0)
    manifest = {"root_hash": "a" * 64, "file_count": 1, "character_count": 1}; stage: list[Path] = []
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: manifest)
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True}); monkeypatch.setattr(cli, "_safe_archive_parent", lambda path: path.mkdir(exist_ok=True))
    monkeypatch.setattr(cli, "_copy_verified", lambda _source, destination, *_args: (stage.append(destination), destination.mkdir()))
    calls = 0
    def preflight(*_args: object) -> None:
        nonlocal calls; calls += 1
        if calls == 3:
            stage[0].rmdir(); stage[0].mkdir()
    monkeypatch.setattr(cli, "_process_preflight", preflight)
    with pytest.raises(SyncError) as error: cli.preserve(tmp_path / "config", apply=True)
    assert error.value.code == "unsafe_archive_path"
    assert not list((tmp_path / "archives").glob("save-preserved-*"))


def test_preserve_revalidates_archive_parent_before_publish(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="test", stable_scan_retries=1, stable_window_seconds=0)
    manifest = {"root_hash": "b" * 64, "file_count": 1, "character_count": 1}; calls = 0
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: manifest)
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True}); monkeypatch.setattr(cli, "_process_preflight", lambda *_a: None)
    def parent(path: Path) -> None:
        nonlocal calls; calls += 1
        if calls == 2: raise SyncError("unsafe_archive_path", "swapped", 6)
        path.mkdir(exist_ok=True)
    monkeypatch.setattr(cli, "_safe_archive_parent", parent)
    monkeypatch.setattr(cli, "_copy_verified", lambda _source, destination, *_args: destination.mkdir())
    with pytest.raises(SyncError) as error: cli.preserve(tmp_path / "config", apply=True)
    assert error.value.code == "unsafe_archive_path"


def test_shortcut_refuses_same_destination_and_legacy_can_coexist(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"; desktop.mkdir()
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"schema_version":"1.0.0","machine_id":"desktop-a","save_root":"C:/save","vault_repo":"C:/vault","remote":"origin","branch":"main","game_install":"C:/game","launcher_mode":"dpyes","launcher_path":"C:/dpyes.exe","game_process_names":["Grim Dawn.exe"],"launch_timeout_seconds":1,"stable_window_seconds":1,"stable_scan_retries":1,"offline_policy":"deny"}), encoding="utf-8")
    (desktop / SHORTCUT_NAME).touch()
    with pytest.raises(SyncError, match="already exists"):
        install_shortcut(desktop, config_path=config, source_root=tmp_path, adapter=ShortcutAdapter())
    (desktop / SHORTCUT_NAME).unlink()
    (desktop / LEGACY_SHORTCUT_NAME).touch()
    calls: list[tuple[Path, str, str, str]] = []
    class FakeAdapter:
        def create(self, destination: Path, target: str, arguments: str, working_directory: str) -> None:
            calls.append((destination, target, arguments, working_directory))
    assert install_shortcut(desktop, config_path=config, source_root=tmp_path, adapter=FakeAdapter()) == desktop / SHORTCUT_NAME  # type: ignore[arg-type]
    assert calls[0][0] == desktop / SHORTCUT_NAME and "launch" in calls[0][2]


def test_shortcut_propagates_adapter_failure_without_creating_file(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"; desktop.mkdir()
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"schema_version":"1.0.0","machine_id":"desktop-a","save_root":"C:/save","vault_repo":"C:/vault","remote":"origin","branch":"main","game_install":"C:/game","launcher_mode":"dpyes","launcher_path":"C:/dpyes.exe","game_process_names":["Grim Dawn.exe"],"launch_timeout_seconds":1,"stable_window_seconds":1,"stable_scan_retries":1,"offline_policy":"deny"}), encoding="utf-8")
    class FailingAdapter:
        def create(self, destination: Path, target: str, arguments: str, working_directory: str) -> None:
            raise SyncError("shortcut_unavailable", "adapter unavailable", EXIT_CONFIGURATION)

    with pytest.raises(SyncError, match="adapter unavailable"):
        install_shortcut(desktop, config_path=config, source_root=tmp_path, adapter=FailingAdapter())  # type: ignore[arg-type]
    assert not (desktop / SHORTCUT_NAME).exists()


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
