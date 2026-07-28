from __future__ import annotations

from pathlib import Path

import pytest

from grim_dawn_sync import cli
from grim_dawn_sync.errors import EXIT_CONFIGURATION, SyncError
from grim_dawn_sync.shortcut import LEGACY_SHORTCUT_NAME, SHORTCUT_NAME, install_shortcut


def test_parser_requires_explicit_bootstrap_source_and_restore_commit() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit): parser.parse_args(["bootstrap"])
    with pytest.raises(SystemExit): parser.parse_args(["restore"])
    assert parser.parse_args(["bootstrap", "--source-cloud", "cloud"]).apply is False
    assert parser.parse_args(["restore", "--commit", "a" * 40]).apply is False


def test_install_shortcut_dry_run_does_not_create_or_call_adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called: list[object] = []
    monkeypatch.setattr(cli, "install_shortcut", lambda *args, **kwargs: called.append(args))
    assert cli.main(["--json", "install-shortcut"]) == 0
    assert called == [] and not (tmp_path / SHORTCUT_NAME).exists()


class FakeShortcut:
    def __init__(self): self.calls: list[tuple[Path, str, str]] = []
    def create(self, destination: Path, target: str, arguments: str) -> None: self.calls.append((destination, target, arguments))


def test_shortcut_apply_uses_launch_target_and_refuses_only_same_name(tmp_path: Path) -> None:
    fake = FakeShortcut()
    destination = install_shortcut(tmp_path, adapter=fake, executable="grim-dawn-sync")
    assert destination == tmp_path / SHORTCUT_NAME
    assert fake.calls == [(destination, "grim-dawn-sync", "launch")]
    destination.touch()
    with pytest.raises(SyncError) as exists: install_shortcut(tmp_path, adapter=fake)
    assert exists.value.code == "shortcut_exists" and exists.value.exit_code == EXIT_CONFIGURATION
    destination.unlink(); (tmp_path / LEGACY_SHORTCUT_NAME).touch()
    assert install_shortcut(tmp_path, adapter=fake) == destination


def test_shortcut_adapter_failure_cannot_create_a_replacement(tmp_path: Path) -> None:
    class Failing:
        def create(self, destination: Path, target: str, arguments: str) -> None:
            raise SyncError("shortcut_unavailable", "x", EXIT_CONFIGURATION)
    with pytest.raises(SyncError, match="x"):
        install_shortcut(tmp_path, adapter=Failing())
    assert not (tmp_path / SHORTCUT_NAME).exists()


def test_cli_expected_error_has_actionable_next_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "status", lambda path: (_ for _ in ()).throw(SyncError("push_incomplete", "x", 6)))
    assert cli.main(["--json", "status"]) == 6
