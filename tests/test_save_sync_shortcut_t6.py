from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from grim_dawn_sync.errors import EXIT_CONFIGURATION, SyncError
from grim_dawn_sync.shortcut import LEGACY_SHORTCUT_NAME, SHORTCUT_NAME, ShortcutAdapter, install_shortcut


class FakeAdapter:
    def __init__(self) -> None: self.calls: list[tuple[Path, str, str]] = []
    def create(self, destination: Path, target: str, arguments: str) -> None: self.calls.append((destination, target, arguments))


def test_install_shortcut_is_create_only_and_targets_cli_launch(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    destination = install_shortcut(tmp_path, adapter=adapter)
    assert destination == tmp_path / SHORTCUT_NAME
    assert adapter.calls == [(destination, "grim-dawn-sync", "launch")]
    assert not tmp_path.joinpath(SHORTCUT_NAME).exists(), "fake adapter must not require a real COM shortcut"


def test_existing_current_shortcut_is_left_untouched(tmp_path: Path) -> None:
    name, code = SHORTCUT_NAME, "shortcut_exists"
    existing = tmp_path / name; existing.write_text("original", encoding="utf-8")
    adapter = FakeAdapter()
    with pytest.raises(SyncError) as raised:
        install_shortcut(tmp_path, adapter=adapter)
    assert raised.value.code == code and raised.value.exit_code == EXIT_CONFIGURATION
    assert existing.read_text(encoding="utf-8") == "original"
    assert adapter.calls == []


def test_legacy_dpyes_shortcut_coexists_with_new_save_sync_shortcut(tmp_path: Path) -> None:
    legacy = tmp_path / LEGACY_SHORTCUT_NAME
    legacy.write_text("original", encoding="utf-8")
    adapter = FakeAdapter()
    assert install_shortcut(tmp_path, adapter=adapter) == tmp_path / SHORTCUT_NAME
    assert legacy.read_text(encoding="utf-8") == "original"
    assert adapter.calls == [(tmp_path / SHORTCUT_NAME, "grim-dawn-sync", "launch")]


def test_adapter_failure_does_not_write_or_replace_shortcut(tmp_path: Path) -> None:
    class FailingAdapter:
        def create(self, destination: Path, target: str, arguments: str) -> None:
            raise SyncError("shortcut_unavailable", "COM unavailable", EXIT_CONFIGURATION)

    with pytest.raises(SyncError, match="COM unavailable"):
        install_shortcut(tmp_path, adapter=FailingAdapter())  # type: ignore[arg-type]
    assert not (tmp_path / SHORTCUT_NAME).exists()


def test_windows_adapter_failure_never_writes_shortcut(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import grim_dawn_sync.shortcut as shortcut
    monkeypatch.setattr(shortcut.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(SyncError, match="unavailable"):
        ShortcutAdapter().create(tmp_path / SHORTCUT_NAME, "grim-dawn-sync", "launch")
    assert not (tmp_path / SHORTCUT_NAME).exists()


def test_default_adapter_never_invokes_com_when_destination_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / SHORTCUT_NAME).touch()
    monkeypatch.setattr(ShortcutAdapter, "create", lambda *args: pytest.fail("existing shortcut must be rejected before adapter use"))
    with pytest.raises(SyncError, match="already exists"):
        install_shortcut(tmp_path)
