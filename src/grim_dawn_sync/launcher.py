"""Safe DPYes launch and Grim Dawn process lifetime monitoring."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import time
from typing import Callable, Protocol, Sequence

from grim_dawn_sync.config import SyncConfig
from grim_dawn_sync.errors import EXIT_LAUNCH, SyncError
from grim_dawn_sync.process_monitor import ProcessMonitor, ProcessScan, WindowsProcessMonitor


class ProcessRunner(Protocol):
    def start(self, args: Sequence[str], *, cwd: Path, shell: bool) -> object:
        """Start the configured launcher only."""


class SubprocessRunner:
    def start(self, args: Sequence[str], *, cwd: Path, shell: bool) -> subprocess.Popen[bytes]:
        return subprocess.Popen(args, cwd=cwd, shell=shell)


@dataclass(frozen=True)
class LaunchResult:
    game_pids: tuple[int, ...]


class DPYesLauncher:
    def __init__(self, config: SyncConfig, monitor: ProcessMonitor | None = None, runner: ProcessRunner | None = None, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep, poll_seconds: float = 0.1) -> None:
        self.config = config
        self.monitor = WindowsProcessMonitor() if monitor is None else monitor
        self.runner = SubprocessRunner() if runner is None else runner
        self.clock, self.sleep, self.poll_seconds = clock, sleep, poll_seconds

    def run(self) -> LaunchResult:
        before = self._scan()
        configured = {name.casefold() for name in self.config.game_process_names}
        existing = [item for item in before.processes if item.name.casefold() in configured]
        if existing:
            raise self._error("game_already_running", "Configured Grim Dawn process already exists.")
        # Defensive validation; config owns the normal invariant, but launching
        # an arbitrary executable is never an acceptable fallback.
        launcher = self.config.launcher_path
        if launcher.name.casefold() != "dpyes.exe" or not self._under(launcher, self.config.game_install):
            raise self._error("unsafe_launcher_path", "DPYes launcher path is outside the configured game installation.")
        try:
            self.runner.start([str(launcher)], cwd=launcher.parent, shell=False)
        except OSError as error:
            raise self._error("dpyes_start_failed", "DPYes could not be started.") from error
        watched = self._wait_for_start(before, configured)
        identities_before = {
            (item.pid, item.creation_marker)
            for item in before.processes
            if item.creation_marker is not None
        }
        game_pids = self._wait_for_exit(watched, configured, identities_before)
        return LaunchResult(tuple(sorted(game_pids)))

    def _wait_for_start(self, before: ProcessScan, configured: set[str]) -> dict[int, int]:
        deadline = self.clock() + self.config.launch_timeout_seconds
        previous = {(item.pid, item.creation_marker) for item in before.processes}
        while self.clock() <= deadline:
            scan = self._scan()
            matches = [item for item in scan.processes if item.name.casefold() in configured]
            watched: dict[int, int] = {}
            for item in matches:
                if item.executable_path is None or item.creation_marker is None:
                    raise self._error("game_identity_unknown", "A configured game process could not be identified safely.")
                if not self._under(item.executable_path, self.config.game_install):
                    continue
                identity = (item.pid, item.creation_marker)
                if identity not in previous:
                    watched[item.pid] = item.creation_marker
            if watched:
                return watched
            self.sleep(self.poll_seconds)
        raise self._error("game_start_timeout", "Grim Dawn did not start before the configured timeout.")

    def _wait_for_exit(
        self,
        watched: dict[int, int],
        configured: set[str],
        identities_before: set[tuple[int, int]],
    ) -> set[int]:
        active = {(pid, marker) for pid, marker in watched.items()}
        observed_pids = set(watched)
        while watched:
            scan = self._scan()
            current: set[tuple[int, int]] = set()
            for item in scan.processes:
                if item.name.casefold() not in configured:
                    continue
                if item.executable_path is None or item.creation_marker is None:
                    raise self._error("game_identity_unknown", "A configured game process could not be identified safely.")
                if not self._under(item.executable_path, self.config.game_install):
                    continue
                identity = (item.pid, item.creation_marker)
                if identity not in identities_before:
                    current.add(identity)

            # Processes may be started in stages by DPYes.  Adopt every new,
            # safely identified game instance seen while any game is active.
            active = current
            observed_pids.update(pid for pid, _ in current)
            watched = dict(active)
            if active:
                self.sleep(self.poll_seconds)
        return observed_pids

    def _scan(self) -> ProcessScan:
        scan = self.monitor.scan()
        if not scan.complete:
            raise self._error("process_monitor_unknown", "Process monitoring access is incomplete; launch was refused.")
        return scan

    @staticmethod
    def _under(path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
            return True
        except ValueError:
            return False

    @staticmethod
    def _error(code: str, message: str) -> SyncError:
        return SyncError(code, message, exit_code=EXIT_LAUNCH)


def launch_dpyes(config: SyncConfig, **kwargs: object) -> LaunchResult:
    """Convenience entry point used by the future workflow state machine."""
    return DPYesLauncher(config, **kwargs).run()
