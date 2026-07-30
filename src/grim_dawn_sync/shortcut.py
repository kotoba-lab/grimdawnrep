"""Create-only, self-contained Windows desktop launch shortcuts."""
from __future__ import annotations

import base64
from pathlib import Path
import stat
import subprocess
import sys

from grim_dawn_sync.config import load_config
from grim_dawn_sync.errors import EXIT_CONFIGURATION, SyncError


SHORTCUT_NAME = "Grim Dawn (DPYes + Save Sync).lnk"
LEGACY_SHORTCUT_NAME = "Grim Dawn (DPYes).lnk"
_REPARSE_POINT = 0x400


def _safe_existing(path: Path, *, directory: bool, label: str) -> Path:
    """Return an absolute ordinary path, rejecting links and reparse points."""
    # ``absolute`` is lexical (unlike ``resolve``), so it cannot hide an
    # ancestor junction before the lstat walk below.
    candidate = Path(path).absolute()
    try:
        info = candidate.lstat()
    except (FileNotFoundError, OSError) as error:
        raise SyncError("shortcut_path_unsafe", f"Shortcut {label} is not an existing safe path.", EXIT_CONFIGURATION) from error
    if candidate.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT):
        raise SyncError("shortcut_path_unsafe", f"Shortcut {label} is not an existing safe path.", EXIT_CONFIGURATION)
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected:
        raise SyncError("shortcut_path_unsafe", f"Shortcut {label} is not an existing safe path.", EXIT_CONFIGURATION)
    # ``Path.resolve`` follows ancestor junctions too.  Inspect each existing
    # ancestor first so a benign-looking leaf cannot escape through one.
    parent = candidate.parent
    while parent != parent.parent:
        try:
            parent_info = parent.lstat()
        except OSError as error:
            raise SyncError("shortcut_path_unsafe", f"Shortcut {label} has an unsafe parent.", EXIT_CONFIGURATION) from error
        if parent.is_symlink() or bool(getattr(parent_info, "st_file_attributes", 0) & _REPARSE_POINT) or not stat.S_ISDIR(parent_info.st_mode):
            raise SyncError("shortcut_path_unsafe", f"Shortcut {label} has an unsafe parent.", EXIT_CONFIGURATION)
        parent = parent.parent
    try:
        return candidate.resolve(strict=True)
    except OSError as error:
        raise SyncError("shortcut_path_unsafe", f"Shortcut {label} could not be resolved safely.", EXIT_CONFIGURATION) from error


def _assert_destination_absent(destination: Path) -> None:
    """Reject every existing object, including links, directories, and reparse points."""
    try:
        destination.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise SyncError("shortcut_path_unsafe", "Save Sync shortcut could not be inspected safely.", EXIT_CONFIGURATION) from error
    raise SyncError("shortcut_exists", "Save Sync shortcut already exists; it was not replaced.", EXIT_CONFIGURATION)


def _encoded(value: Path) -> str:
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _launch_arguments(source_root: Path, config_path: Path) -> str:
    """Fixed ASCII ``-c`` payload; user paths are data, never Python syntax."""
    source = _encoded(source_root)
    config = _encoded(config_path)
    code = (
        "import base64,sys;"
        "d=lambda x:base64.b64decode(x).decode('utf-8');"
        f"sys.path.insert(0,d('{source}'));"
        "from grim_dawn_sync.cli import main;"
        f"raise SystemExit(main(['--config',d('{config}'),'launch']))"
    )
    # The payload is ASCII and contains no double quote.  This is the exact
    # CommandLineToArgvW-safe representation used by python.exe on Windows.
    return f'-c "{code}"'


class ShortcutAdapter:
    def create(self, destination: Path, target: str, arguments: str, working_directory: str) -> None:
        _assert_destination_absent(destination)
        try:
            # WScript is part of Windows; PowerShell avoids a pywin32 dependency.
            script = (
                "$ErrorActionPreference='Stop'; "
                f"$p=[IO.Path]::GetFullPath('{str(destination).replace("'", "''")}'); "
                "if (Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue) { exit 17 }; "
                # WScript.Shell rejects temporary names whose final extension
                # is not .lnk, even when the eventual destination is a .lnk.
                "$tmp=$p+'.new-'+[guid]::NewGuid().ToString('N')+'.lnk'; "
                "try { $s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut($tmp); "
                f"$l.TargetPath='{target.replace("'", "''")}'; $l.Arguments='{arguments.replace("'", "''")}'; "
                f"$l.WorkingDirectory='{working_directory.replace("'", "''")}'; $l.Save(); "
                "[IO.File]::Move($tmp,$p) } finally { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force } }"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                # The captured streams are intentionally bytes.  Windows
                # PowerShell can emit UTF-8 diagnostics while Python's locale
                # decoder is cp932, and the diagnostics are not exposed.
                check=False, capture_output=True, timeout=20,
            )
            if result.returncode == 17:
                raise SyncError("shortcut_exists", "Save Sync shortcut already exists; it was not replaced.", EXIT_CONFIGURATION)
            if result.returncode:
                raise SyncError("shortcut_unavailable", "Windows shortcut support is unavailable.", EXIT_CONFIGURATION)
        except (OSError, subprocess.SubprocessError) as error:
            raise SyncError("shortcut_unavailable", "Windows shortcut support is unavailable.", EXIT_CONFIGURATION) from error


def install_shortcut(
    desktop: Path,
    *,
    config_path: Path,
    source_root: Path | None = None,
    adapter: ShortcutAdapter | None = None,
    executable: str | None = None,
) -> Path:
    """Create a new shortcut without relying on PATH, console scripts, or PYTHONPATH."""
    safe_desktop = _safe_existing(Path(desktop), directory=True, label="desktop")
    safe_config = _safe_existing(Path(config_path), directory=False, label="configuration")
    # Config validation is deliberately before any COM write attempt.
    load_config(safe_config)
    # This project uses a src layout; the import root is ``.../src``, not the
    # checkout root containing it.
    root = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[1]
    safe_root = _safe_existing(root, directory=True, label="source root")
    target = Path(executable) if executable is not None else Path(sys.executable)
    safe_target = _safe_existing(target, directory=False, label="Python executable")
    destination = safe_desktop / SHORTCUT_NAME
    _assert_destination_absent(destination)
    (adapter or ShortcutAdapter()).create(destination, str(safe_target), _launch_arguments(safe_root, safe_config), str(safe_root))
    return destination
