"""Desktop shortcut adapter.  Creation is deliberately create-only."""
from __future__ import annotations
from pathlib import Path
import subprocess
from grim_dawn_sync.errors import EXIT_CONFIGURATION, SyncError

SHORTCUT_NAME = "Grim Dawn (DPYes + Save Sync).lnk"
LEGACY_SHORTCUT_NAME = "Grim Dawn (DPYes).lnk"

class ShortcutAdapter:
    def create(self, destination: Path, target: str, arguments: str) -> None:
        if destination.exists() or destination.is_symlink():
            raise SyncError("shortcut_exists", "Save Sync shortcut already exists; it was not replaced.", EXIT_CONFIGURATION)
        try:
            # WScript is provided by Windows.  Keeping it behind PowerShell
            # avoids an undeclared pywin32 runtime dependency.
            script = (
                "$ErrorActionPreference='Stop'; "
                f"$p=[IO.Path]::GetFullPath('{str(destination).replace("'", "''")}'); "
                "if (Test-Path -LiteralPath $p) { exit 17 }; "
                "$tmp=$p+'.new-'+[guid]::NewGuid().ToString('N'); "
                "try { $s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut($tmp); "
                f"$l.TargetPath='{target.replace("'", "''")}'; $l.Arguments='{arguments.replace("'", "''")}'; $l.Save(); "
                "[IO.File]::Move($tmp,$p) } finally { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force } }"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                check=False, capture_output=True, text=True, timeout=20,
            )
            if result.returncode == 17:
                raise SyncError("shortcut_exists", "Save Sync shortcut already exists; it was not replaced.", EXIT_CONFIGURATION)
            if result.returncode:
                raise SyncError("shortcut_unavailable", "Windows shortcut support is unavailable.", EXIT_CONFIGURATION)
        except (OSError, subprocess.SubprocessError) as error:
            raise SyncError("shortcut_unavailable", "Windows shortcut support is unavailable.", EXIT_CONFIGURATION) from error

def install_shortcut(desktop: Path, *, adapter: ShortcutAdapter | None = None, executable: str = "grim-dawn-sync") -> Path:
    destination = Path(desktop) / SHORTCUT_NAME
    if destination.exists() or destination.is_symlink():
        raise SyncError("shortcut_exists", "Save Sync shortcut already exists; it was not replaced.", EXIT_CONFIGURATION)
    (adapter or ShortcutAdapter()).create(destination, executable, "launch")
    return destination
