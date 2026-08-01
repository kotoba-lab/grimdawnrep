from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from grim_dawn_sync.process_monitor import ProcessInfo, WindowsProcessMonitor


def test_watched_name_filter_casefolds_and_skips_details_for_unrelated_pids(monkeypatch) -> None:
    monitor = WindowsProcessMonitor(("Grim Dawn.exe", "dpyes.exe"))
    detailed: list[tuple[int, str]] = []

    def details(_kernel32: object, pid: int, name: str) -> ProcessInfo:
        detailed.append((pid, name))
        return ProcessInfo(pid, name, Path(name), pid)

    monkeypatch.setattr(monitor, "_details", details)
    rows = [(pid, f"unrelated-{pid}.exe") for pid in range(5000)]
    rows.extend(((6001, "GRIM DAWN.EXE"), (6002, "DPYes.Exe")))
    found = [monitor._details_if_watched(object(), pid, name) for pid, name in rows]

    assert detailed == [(6001, "GRIM DAWN.EXE"), (6002, "DPYes.Exe")]
    assert len([item for item in found if item is not None]) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows Toolhelp process identity test")
def test_windows_monitor_identifies_an_isolated_child_process() -> None:
    executable_name = Path(sys.executable).name
    monitor = WindowsProcessMonitor((executable_name,))
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        observed = None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            scan = monitor.scan()
            assert scan.complete
            observed = next((item for item in scan.processes if item.pid == child.pid), None)
            if observed is not None:
                break
            time.sleep(0.05)
        assert observed is not None
        assert observed.name.casefold() == executable_name.casefold()
        assert observed.executable_path is not None
        assert observed.creation_marker is not None
    finally:
        child.terminate()
        child.wait(timeout=5)
