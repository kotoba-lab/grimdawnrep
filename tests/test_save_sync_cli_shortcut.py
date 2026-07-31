from __future__ import annotations

from pathlib import Path
import json

import pytest

from grim_dawn_sync import cli
from grim_dawn_sync.errors import EXIT_CONFIGURATION, SyncError
from grim_dawn_sync.shortcut import (
    LEGACY_SHORTCUT_NAME, SELECTION_SHORTCUT_NAME, SHORTCUT_NAME, ShortcutShape,
    _launch_arguments, install_shortcut, migrate_shortcut, shortcut_matches,
)


def test_parser_requires_explicit_bootstrap_source_and_restore_commit() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit): parser.parse_args(["bootstrap"])
    with pytest.raises(SystemExit): parser.parse_args(["restore"])
    assert parser.parse_args(["bootstrap", "--source-cloud", "cloud"]).apply is False
    assert parser.parse_args(["restore", "--commit", "a" * 40]).apply is False
    assert parser.parse_args(["enroll"]).apply is False
    assert parser.parse_args(["join"]).command == "join"


def test_install_shortcut_dry_run_does_not_create_or_call_adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called: list[object] = []
    monkeypatch.setattr(cli, "install_shortcut", lambda *args, **kwargs: called.append(args))
    assert cli.main(["--json", "install-shortcut"]) == 0
    assert called == [] and not (tmp_path / SHORTCUT_NAME).exists()


def test_cli_apply_passes_the_explicit_config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cli, "install_shortcut", lambda *args, **kwargs: calls.append((args, kwargs)))
    config = tmp_path / "relative-config.json"
    assert cli.main(["--config", str(config), "--json", "install-shortcut", "--apply"]) == 0
    assert calls == [((tmp_path / "Desktop",), {"config_path": config})]


class FakeShortcut:
    def __init__(self): self.calls: list[tuple[Path, str, str, str]] = []
    def create(self, destination: Path, target: str, arguments: str, working_directory: str) -> None: self.calls.append((destination, target, arguments, working_directory))


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({"schema_version":"1.0.0", "machine_id":"desktop-a", "save_root":"C:/save", "vault_repo":"C:/vault", "remote":"origin", "branch":"main", "game_install":"C:/game", "launcher_mode":"dpyes", "launcher_path":"C:/dpyes.exe", "game_process_names":["Grim Dawn.exe"], "launch_timeout_seconds":1, "stable_window_seconds":1, "stable_scan_retries":1, "offline_policy":"deny"}), encoding="utf-8")
    return path


def test_shortcut_apply_uses_launch_target_and_refuses_only_same_name(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"; desktop.mkdir()
    fake = FakeShortcut()
    destination = install_shortcut(desktop, config_path=_config(tmp_path), source_root=tmp_path, adapter=fake)
    assert destination == desktop / SHORTCUT_NAME
    assert len(fake.calls) == 1 and "launch" in fake.calls[0][2]
    destination.touch()
    with pytest.raises(SyncError) as exists:
        install_shortcut(desktop, config_path=_config(tmp_path), source_root=tmp_path, adapter=fake)
    assert exists.value.code == "shortcut_exists" and exists.value.exit_code == EXIT_CONFIGURATION
    destination.unlink(); (desktop / LEGACY_SHORTCUT_NAME).touch()
    assert install_shortcut(desktop, config_path=_config(tmp_path), source_root=tmp_path, adapter=fake) == destination


def test_shortcut_adapter_failure_cannot_create_a_replacement(tmp_path: Path) -> None:
    class Failing:
        def create(self, destination: Path, target: str, arguments: str, working_directory: str) -> None:
            raise SyncError("shortcut_unavailable", "x", EXIT_CONFIGURATION)
    with pytest.raises(SyncError, match="x"):
        desktop = tmp_path / "Desktop"; desktop.mkdir()
        install_shortcut(desktop, config_path=_config(tmp_path), source_root=tmp_path, adapter=Failing())
    assert not (desktop / SHORTCUT_NAME).exists()


def test_cli_expected_error_has_actionable_next_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "status", lambda path: (_ for _ in ()).throw(SyncError("push_incomplete", "x", 6)))
    assert cli.main(["--json", "status"]) == 6


def test_migrate_shortcut_inspects_old_shape_and_creates_new_name_without_replacement(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"; desktop.mkdir()
    config = _config(tmp_path); source = tmp_path / "src"; source.mkdir()
    executable = tmp_path / "python.exe"; executable.touch()
    old = desktop / SHORTCUT_NAME; old.touch(); before = old.stat()
    created: list[Path] = []
    class Adapter:
        def inspect(self, source_path: Path) -> ShortcutShape:
            assert source_path == old
            return ShortcutShape(str(executable), _launch_arguments(source, config), str(tmp_path / "old-checkout"))
        def create(self, destination: Path, target: str, arguments: str, working_directory: str) -> None:
            created.append(destination); destination.touch()
    destination, old_current = migrate_shortcut(desktop, config_path=config, source_root=source,
                                                 executable=str(executable), adapter=Adapter())  # type: ignore[arg-type]
    assert destination == desktop / SELECTION_SHORTCUT_NAME and old_current is False
    assert created == [destination]
    after = old.stat(); assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    with pytest.raises(SyncError) as error:
        migrate_shortcut(desktop, config_path=config, source_root=source,
                         executable=str(executable), adapter=Adapter())  # type: ignore[arg-type]
    assert error.value.code == "shortcut_exists"


def test_shortcut_shape_requires_exact_target_arguments_and_working_checkout(tmp_path: Path) -> None:
    target, source, config = tmp_path / "python.exe", tmp_path / "src", tmp_path / "config.json"
    shape = ShortcutShape(str(target), _launch_arguments(source, config), str(source))
    assert shortcut_matches(shape, target=target, source_root=source, config_path=config)
    assert not shortcut_matches(ShortcutShape(str(target), shape.arguments, str(tmp_path / "old")),
                                target=target, source_root=source, config_path=config)
    assert not shortcut_matches(ShortcutShape(str(target), shape.arguments + "x", str(source)),
                                target=target, source_root=source, config_path=config)
