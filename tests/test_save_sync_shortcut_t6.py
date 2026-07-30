from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess
import stat
from types import SimpleNamespace
import sys

import pytest

from grim_dawn_sync.errors import EXIT_CONFIGURATION, SyncError
from grim_dawn_sync.shortcut import LEGACY_SHORTCUT_NAME, SHORTCUT_NAME, ShortcutAdapter, _launch_arguments, install_shortcut


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({"schema_version":"1.0.0", "machine_id":"desktop-a", "save_root":"C:/save", "vault_repo":"C:/vault", "remote":"origin", "branch":"main", "game_install":"C:/game", "launcher_mode":"dpyes", "launcher_path":"C:/dpyes.exe", "game_process_names":["Grim Dawn.exe"], "launch_timeout_seconds":1, "stable_window_seconds":1, "stable_scan_retries":1, "offline_policy":"deny"}), encoding="utf-8")
    return path


class FakeAdapter:
    def __init__(self) -> None: self.calls: list[tuple[Path, str, str, str]] = []
    def create(self, destination: Path, target: str, arguments: str, working_directory: str) -> None:
        self.calls.append((destination, target, arguments, working_directory))


def test_install_shortcut_is_create_only_and_self_contained(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"; desktop.mkdir()
    source = tmp_path / "source"; source.mkdir()
    adapter = FakeAdapter()
    destination = install_shortcut(desktop, config_path=_config(tmp_path), source_root=source, adapter=adapter)
    assert destination == desktop / SHORTCUT_NAME
    call = adapter.calls[0]
    assert call[1] == str(Path(sys.executable).resolve())
    assert call[3] == str(source.resolve())
    assert call[2].startswith('-c "') and "grim-dawn-sync" not in call[2]
    assert "launch" in call[2] and "--config" in call[2]


def test_arguments_encode_untrusted_paths_as_data(tmp_path: Path) -> None:
    source = tmp_path / "x';__import__('os').system('bad')"; source.mkdir()
    config = tmp_path / "config '; bad.json"; config.write_text("{}", encoding="utf-8")
    args = _launch_arguments(source.resolve(), config.resolve())
    assert str(source) not in args and str(config) not in args and '"' not in args[4:-1]
    encoded = {base64.b64encode(str(item.resolve()).encode()).decode() for item in (source, config)}
    assert all(value in args for value in encoded)


def test_arguments_execute_with_only_embedded_source_and_config(tmp_path: Path) -> None:
    source = tmp_path / "source"; package = source / "grim_dawn_sync"; package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text("def main(argv):\n    return 0 if argv[0:1] == ['--config'] and argv[-1:] == ['launch'] else 9\n", encoding="utf-8")
    config = tmp_path / "safe config.json"; config.write_text("{}", encoding="utf-8")
    arguments = _launch_arguments(source.resolve(), config.resolve())
    # Split only the fixed two Python arguments; no shell or environment
    # imports are involved in this roundtrip check.
    code = arguments.removeprefix('-c "').removesuffix('"')
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env={}, check=False)
    assert result.returncode == 0


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_existing_current_shortcut_is_left_untouched(tmp_path: Path, kind: str) -> None:
    desktop = tmp_path / "Desktop"; desktop.mkdir(); config = _config(tmp_path)
    existing = desktop / SHORTCUT_NAME
    if kind == "file": existing.write_text("original", encoding="utf-8")
    elif kind == "directory": existing.mkdir()
    else:
        try: existing.symlink_to(tmp_path / "missing")
        except OSError: pytest.skip("symlinks unavailable")
    adapter = FakeAdapter()
    with pytest.raises(SyncError) as raised:
        install_shortcut(desktop, config_path=config, source_root=tmp_path, adapter=adapter)
    assert raised.value.code == "shortcut_exists" and raised.value.exit_code == EXIT_CONFIGURATION
    assert adapter.calls == []


def test_legacy_dpyes_shortcut_coexists_with_new_save_sync_shortcut(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"; desktop.mkdir(); config = _config(tmp_path)
    legacy = desktop / LEGACY_SHORTCUT_NAME; legacy.write_text("original", encoding="utf-8")
    adapter = FakeAdapter()
    assert install_shortcut(desktop, config_path=config, source_root=tmp_path, adapter=adapter) == desktop / SHORTCUT_NAME
    assert legacy.read_text(encoding="utf-8") == "original"


def test_unsafe_inputs_are_rejected_before_adapter(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"; desktop.mkdir(); adapter = FakeAdapter()
    with pytest.raises(SyncError, match="safe path"):
        install_shortcut(desktop, config_path=tmp_path / "missing.json", source_root=tmp_path, adapter=adapter)
    assert adapter.calls == []


@pytest.mark.parametrize("which", ["leaf", "ancestor"])
def test_reparse_desktop_or_ancestor_is_rejected_before_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, which: str,
) -> None:
    desktop = tmp_path / "Desktop"; desktop.mkdir(); adapter = FakeAdapter()
    original_lstat = Path.lstat
    marked = desktop.absolute() if which == "leaf" else tmp_path.absolute()

    def reparse_lstat(path: Path):
        if path.absolute() == marked:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    with pytest.raises(SyncError) as raised:
        install_shortcut(desktop, config_path=_config(tmp_path), source_root=tmp_path, adapter=adapter)
    assert raised.value.code == "shortcut_path_unsafe" and adapter.calls == []


def test_windows_adapter_failure_never_writes_shortcut(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import grim_dawn_sync.shortcut as shortcut
    monkeypatch.setattr(shortcut.subprocess, "run", lambda *args, **kwargs: type("Result", (), {"returncode": 1})())
    with pytest.raises(SyncError, match="unavailable"):
        ShortcutAdapter().create(tmp_path / SHORTCUT_NAME, str(Path(sys.executable)), "-c \"pass\"", str(tmp_path))
    assert not (tmp_path / SHORTCUT_NAME).exists()


def test_windows_adapter_uses_lnk_stage_and_captures_undecoded_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import grim_dawn_sync.shortcut as shortcut

    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(shortcut.subprocess, "run", run)
    ShortcutAdapter().create(tmp_path / SHORTCUT_NAME, str(Path(sys.executable)), '-c "pass"', str(tmp_path))
    command, kwargs = calls[0]
    script = command[-1]
    assert "NewGuid().ToString('N')+'.lnk'" in script
    assert kwargs["capture_output"] is True
    assert "text" not in kwargs and "encoding" not in kwargs


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows WScript.Shell")
def test_windows_adapter_creates_valid_shortcut_via_com(tmp_path: Path) -> None:
    destination = tmp_path / SHORTCUT_NAME
    ShortcutAdapter().create(destination, str(Path(sys.executable)), '-c "pass"', str(tmp_path))
    assert destination.is_file()
    assert not list(tmp_path.glob(f"{SHORTCUT_NAME}.new-*"))
