from __future__ import annotations

from pathlib import Path

import pytest

from grim_dawn_sync.config import parse_config
from grim_dawn_sync.errors import EXIT_LAUNCH, SyncError
from grim_dawn_sync.launcher import DPYesLauncher
from grim_dawn_sync.process_monitor import ProcessInfo, ProcessScan


def config(tmp_path: Path, *, timeout: int = 3):
    game = tmp_path / "game"
    return parse_config({"schema_version": "1.0.0", "machine_id": "test", "save_root": str(tmp_path / "save"), "vault_repo": str(tmp_path / "vault"), "remote": "origin", "branch": "main", "game_install": str(game), "launcher_mode": "dpyes", "launcher_path": str(game / "DPYes.exe"), "game_process_names": ["Grim Dawn.exe"], "launch_timeout_seconds": timeout, "stable_window_seconds": 1, "stable_scan_retries": 1, "offline_policy": "deny"})


class FakeMonitor:
    def __init__(self, scans: list[ProcessScan]): self.scans = iter(scans)
    def scan(self) -> ProcessScan:
        try: return next(self.scans)
        except StopIteration: return ProcessScan(())


class FakeRunner:
    def __init__(self): self.calls = []
    def start(self, args, *, cwd, shell): self.calls.append((list(args), cwd, shell)); return object()


class Clock:
    def __init__(self): self.value = 0.0
    def __call__(self): return self.value
    def sleep(self, value): self.value += value


def game(path: Path, pid: int = 10, marker: int | None = 100) -> ProcessInfo:
    return ProcessInfo(pid, "Grim Dawn.exe", path / "x64" / "Grim Dawn.exe", marker)


def launcher(tmp_path, scans, *, timeout=3):
    clock = Clock(); runner = FakeRunner(); cfg = config(tmp_path, timeout=timeout)
    return DPYesLauncher(cfg, FakeMonitor(scans), runner, clock=clock, sleep=clock.sleep, poll_seconds=1), runner, cfg


def test_normal_game_exit_waits_for_game_not_dpyes(tmp_path: Path) -> None:
    subject, runner, cfg = launcher(tmp_path, [ProcessScan(()), ProcessScan((game(cfg_path := tmp_path / "game"),)), ProcessScan((game(cfg_path),)), ProcessScan(())])
    result = subject.run()
    assert result.game_pids == (10,)
    assert runner.calls == [([str(cfg.launcher_path)], cfg.launcher_path.parent, False)]


def test_dpyes_early_exit_does_not_end_game_wait(tmp_path: Path) -> None:
    # DPYes is deliberately absent from every scan: only the game matters.
    subject, _, root = launcher(tmp_path, [ProcessScan(()), ProcessScan((game(tmp_path / "game"),)), ProcessScan((game(tmp_path / "game"),)), ProcessScan(())])
    assert subject.run().game_pids == (10,)


def test_game_crash_is_observed_as_game_process_exit(tmp_path: Path) -> None:
    # The monitor cannot distinguish a crash from a normal process exit. Both
    # are the same safe session boundary for the later snapshot workflow.
    subject, _, _ = launcher(tmp_path, [ProcessScan(()), ProcessScan((game(tmp_path / "game"),)), ProcessScan(())])
    result = subject.run()
    assert result.game_pids == (10,)
    assert not hasattr(result, "crashed")


def test_game_start_timeout(tmp_path: Path) -> None:
    subject, runner, _ = launcher(tmp_path, [ProcessScan(())], timeout=2)
    with pytest.raises(SyncError) as raised: subject.run()
    assert raised.value.code == "game_start_timeout" and raised.value.exit_code == EXIT_LAUNCH
    assert len(runner.calls) == 1


def test_existing_configured_game_fails_before_launch(tmp_path: Path) -> None:
    subject, runner, _ = launcher(tmp_path, [ProcessScan((game(tmp_path / "game"),))])
    with pytest.raises(SyncError, match="already exists"): subject.run()
    assert runner.calls == []


@pytest.mark.parametrize("scan", [ProcessScan((), complete=False), ProcessScan((ProcessInfo(1, "Grim Dawn.exe", None, None),))])
def test_unknown_monitor_or_game_identity_fails_closed(tmp_path: Path, scan: ProcessScan) -> None:
    subject, runner, _ = launcher(tmp_path, [scan])
    with pytest.raises(SyncError): subject.run()
    assert runner.calls == []


def test_unrelated_or_wrong_install_game_is_not_adopted_and_times_out(tmp_path: Path) -> None:
    clock = Clock(); cfg = config(tmp_path, timeout=1); runner = FakeRunner()
    foreign = ProcessInfo(4, "Grim Dawn.exe", tmp_path / "other" / "Grim Dawn.exe", 9)
    subject = DPYesLauncher(cfg, FakeMonitor([ProcessScan(()), ProcessScan((foreign,))]), runner, clock=clock, sleep=clock.sleep, poll_seconds=1)
    with pytest.raises(SyncError, match="did not start"): subject.run()


def test_pid_reuse_is_not_treated_as_game_still_running(tmp_path: Path) -> None:
    root = tmp_path / "game"
    subject, _, _ = launcher(tmp_path, [ProcessScan(()), ProcessScan((game(root, 10, 100),)), ProcessScan((game(root, 10, 101),))])
    assert subject.run().game_pids == (10,)


def test_multiple_new_game_pids_wait_for_all(tmp_path: Path) -> None:
    root = tmp_path / "game"; first = game(root, 10, 100); second = game(root, 11, 101)
    subject, _, _ = launcher(tmp_path, [ProcessScan(()), ProcessScan((first, second)), ProcessScan((second,)), ProcessScan(())])
    assert subject.run().game_pids == (10, 11)


def test_later_game_pid_is_adopted_until_it_exits(tmp_path: Path) -> None:
    root = tmp_path / "game"; first = game(root, 10, 100); later = game(root, 11, 101)
    subject, _, _ = launcher(
        tmp_path,
        [
            ProcessScan(()),
            ProcessScan((first,)),
            ProcessScan((first, later)),
            ProcessScan((later,)),
            ProcessScan(()),
        ],
    )
    assert subject.run().game_pids == (10, 11)


def test_unknown_later_game_identity_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "game"; first = game(root, 10, 100)
    unknown = ProcessInfo(11, "Grim Dawn.exe", None, None)
    subject, _, _ = launcher(tmp_path, [ProcessScan(()), ProcessScan((first,)), ProcessScan((first, unknown))])
    with pytest.raises(SyncError) as raised:
        subject.run()
    assert raised.value.code == "game_identity_unknown"


def test_incomplete_later_process_scan_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "game"; first = game(root, 10, 100)
    subject, _, _ = launcher(tmp_path, [ProcessScan(()), ProcessScan((first,)), ProcessScan((), complete=False)])
    with pytest.raises(SyncError) as raised:
        subject.run()
    assert raised.value.code == "process_monitor_unknown"


def test_identity_present_before_launch_is_never_adopted_later(tmp_path: Path) -> None:
    root = tmp_path / "game"
    preexisting = ProcessInfo(11, "unrelated.exe", root / "unrelated.exe", 101)
    first = game(root, 10, 100)
    same_identity_now_named_game = game(root, 11, 101)
    subject, _, _ = launcher(
        tmp_path,
        [
            ProcessScan((preexisting,)),
            ProcessScan((first,)),
            ProcessScan((first, same_identity_now_named_game)),
            ProcessScan((same_identity_now_named_game,)),
        ],
    )
    assert subject.run().game_pids == (10,)


def test_runner_failure_is_launch_error(tmp_path: Path) -> None:
    class BadRunner:
        def start(self, *args, **kwargs): raise OSError("no start")
    clock = Clock(); cfg = config(tmp_path)
    subject = DPYesLauncher(cfg, FakeMonitor([ProcessScan(())]), BadRunner(), clock=clock, sleep=clock.sleep)
    with pytest.raises(SyncError, match="could not be started"): subject.run()
