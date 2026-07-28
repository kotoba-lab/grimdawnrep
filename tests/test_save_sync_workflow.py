from __future__ import annotations

import json
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from grim_dawn_sync.config import parse_config
from grim_dawn_sync.errors import EXIT_CONFLICT, EXIT_RECOVERY_REQUIRED, EXIT_VALIDATION, SyncError
from grim_dawn_sync.workflow import DomainAdapters, LaunchWorkflow, WorkflowState


def config(tmp_path: Path):
    return parse_config({
        "schema_version": "1.0.0", "machine_id": "test-machine",
        "save_root": str(tmp_path / "live-save"), "vault_repo": str(tmp_path / "vault"),
        "remote": "origin", "branch": "main", "game_install": str(tmp_path / "game"),
        "launcher_mode": "dpyes", "launcher_path": str(tmp_path / "game" / "DPYes.exe"),
        "game_process_names": ["Grim Dawn.exe"], "launch_timeout_seconds": 1,
        "stable_window_seconds": 1, "stable_scan_retries": 1, "offline_policy": "deny",
    })


class FakeAdapters:
    """Recording adapter: workflow tests never start processes, swap saves, or use Git."""

    def __init__(self, *, relation: str = "equal", remote: str | None = "a" * 40, failure: tuple[str, SyncError] | None = None) -> None:
        self.relation, self.remote, self.failure = relation, remote, failure
        self.calls: list[str] = []
        self.lock = SimpleNamespace(session=SimpleNamespace(session_id="session-1"))

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.failure and self.failure[0] == name:
            raise self.failure[1]

    def preflight(self): self._call("preflight")
    def state(self): return SimpleNamespace(last_applied_remote_commit="b" * 40, last_applied_manifest_root_hash="old")
    def live_manifest(self): return {"root_hash":"old", "character_count":1, "file_count":1, "total_bytes":1}
    def remote_manifest(self, base, session): return {"root_hash":"new"}
    def fetch_and_reconcile(self): self._call("fetch"); return SimpleNamespace(relation=self.relation)
    def remote_oid(self): self._call("remote_oid"); return self.remote
    def acquire(self, base): self._call("acquire"); assert base == self.remote; return self.lock
    def prepare_remote_restore(self, base, session): self._call("prepare"); assert (base, session) == (self.remote, "session-1"); return "plan"
    def archive_before_restore(self, plan): self._call("archive_before"); assert plan == "plan"; return "archived-plan"
    def apply_remote_save(self, plan): self._call("apply"); assert plan == "archived-plan"; return {"ok": True}
    def launch(self, hook):
        self._call("launch")
        hook("START_DPYES"); hook("WAIT_GAME_START"); hook("WAIT_GAME_EXIT")
    def wait_save_stable(self): return {"root_hash":"new", "character_count":1, "file_count":1, "total_bytes":1}
    def validate(self, manifest, baseline): self._call("validate"); return manifest
    def archive_after_game(self, manifest): self._call("archive_after_game"); return manifest["root_hash"]
    def rescue_raw(self, manifest): self._call("rescue_raw"); return (manifest or {"root_hash":"raw"})["root_hash"]
    def quarantine(self, manifest): self._call("quarantine"); return manifest["root_hash"]
    def snapshot(self, session, manifest, hook):
        self._call("snapshot"); assert session == "session-1"; hook("UPDATE_VAULT"); hook("COMMIT"); return "b" * 40
    def mark_committed(self, lock, oid, root_hash): self._call("mark_committed"); assert lock is self.lock and oid == "b" * 40 and root_hash == "new"
    def push(self, oid): self._call("push"); assert oid == "b" * 40; return "c" * 40
    def release(self, lock, oid, manifest): self._call("release"); assert lock is self.lock and oid == "c" * 40


def run(tmp_path: Path, adapters: FakeAdapters) -> tuple[LaunchWorkflow, Path]:
    root = tmp_path / "local"
    return LaunchWorkflow(config(tmp_path), root, adapters=adapters), root / "logs" / "launch.jsonl"


def test_domain_remote_manifest_reads_commit_without_creating_staging(tmp_path: Path) -> None:
    subject = DomainAdapters(config(tmp_path), tmp_path / "local")
    commit = "a" * 40
    expected = {"root_hash": "b" * 64}
    calls: list[str] = []
    subject.vault = SimpleNamespace(
        read_manifest=lambda value: calls.append(value) or expected,
    )

    assert subject.remote_manifest(commit, "reconcile") is expected
    assert calls == [commit]
    assert not (tmp_path / "local").exists()


def test_domain_release_passes_manifest_root_into_atomic_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = DomainAdapters(config(tmp_path), tmp_path / "local")
    lock = object()
    commit, root_hash = "a" * 40, "b" * 64
    captured: dict[str, object] = {}

    def fake_release(vault, passed_lock, pushed_commit, **kwargs):
        captured.update(
            vault=vault,
            lock=passed_lock,
            commit=pushed_commit,
            **kwargs,
        )

    monkeypatch.setattr("grim_dawn_sync.workflow.release_lock", fake_release)
    subject.release(lock, commit, {"root_hash": root_hash})

    assert captured == {
        "vault": subject.vault,
        "lock": lock,
        "commit": commit,
        "state_path": tmp_path / "local" / "state.json",
        "confirmed_root_hash": root_hash,
    }


def test_domain_snapshot_binds_the_exact_validated_manifest(tmp_path: Path) -> None:
    subject = DomainAdapters(config(tmp_path), tmp_path / "local")
    manifest = {"root_hash": "b" * 64, "character_count": 1, "file_count": 1, "total_bytes": 1}
    captured: dict[str, object] = {}
    subject.vault = SimpleNamespace(snapshot=lambda source, **kwargs: captured.update(source=source, **kwargs) or "a" * 40)

    assert subject.snapshot("session-1", manifest, lambda _state: None) == "a" * 40
    assert captured["expected_manifest"] is manifest


def test_destination_root_rejects_mocked_reparse_ancestor_before_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ancestor = tmp_path / "redirect"
    ancestor.mkdir()
    subject = DomainAdapters(config(tmp_path), ancestor / "local")
    original_lstat = Path.lstat

    def fake_lstat(path: Path):
        if path == ancestor:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_file_attributes=0x400,
                FILE_ATTRIBUTE_REPARSE_POINT=0x400,
            )
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(SyncError) as caught:
        subject._destination_root("archives")
    assert caught.value.code == "unsafe_save_tree"
    assert not (ancestor / "local").exists()


def test_destination_root_rejects_symlink_ancestor_without_creating_outside(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_local = tmp_path / "linked-local"
    try:
        linked_local.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable for this account")
    subject = DomainAdapters(config(tmp_path), linked_local)

    with pytest.raises(SyncError) as caught:
        subject._destination_root("quarantine")
    assert caught.value.code == "unsafe_save_tree"
    assert not (outside / "quarantine").exists()


def test_lock_held_validation_failure_has_recovery_details_and_log(tmp_path: Path) -> None:
    failure = SyncError("save_invalid", "x", EXIT_VALIDATION)
    adapters = FakeAdapters(failure=("validate", failure)); subject, log = run(tmp_path, adapters)
    with pytest.raises(SyncError) as caught:
        subject.run()
    details = caught.value.details
    assert details["machine_id"] == "test-machine"
    assert details["session_id"] == "session-1"
    assert details["last_state"] == "VALIDATE_SAVE"
    assert details["next_command"] == "grim-dawn-sync recover"
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert row["next_command"] == "grim-dawn-sync recover" and row["session_id"] == "session-1"


def test_full_workflow_archives_before_apply_and_logs_only_safe_state_data(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, log = run(tmp_path, adapters)
    assert subject.run() == {"state": "COMPLETE", "commit": "c" * 40}
    assert adapters.calls == ["preflight", "fetch", "remote_oid", "acquire", "prepare", "archive_before", "apply", "launch", "validate", "archive_after_game", "snapshot", "mark_committed", "push", "release"]
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [row["state"] for row in rows] == [
        "PREFLIGHT", "FETCH_REMOTE", "RECONCILE", "ACQUIRE_LOCK", "ARCHIVE_BEFORE_RESTORE",
        "APPLY_REMOTE_SAVE", "START_DPYES", "WAIT_GAME_START", "WAIT_GAME_EXIT", "WAIT_SAVE_STABLE",
        "VALIDATE_SAVE", "ARCHIVE_AFTER_GAME", "UPDATE_VAULT", "COMMIT", "PUSH", "RELEASE_LOCK", "COMPLETE",
    ]
    assert rows[-1]["state"] == "COMPLETE" and rows[-1]["event"] == "entered"
    assert {"timestamp_utc", "machine_id", "session_id", "last_successful_state", "safe_oid"} <= set(rows[-1])
    serialized = log.read_text(encoding="utf-8")
    assert str(tmp_path) not in serialized and "a" * 40 not in serialized


@pytest.mark.parametrize(
    ("relation", "remote", "code", "exit_code", "expected_calls"),
    [
        ("ahead", "a" * 40, "vault_not_reconciled", EXIT_CONFLICT, ["preflight", "fetch"]),
        ("equal", None, "remote_main_missing", EXIT_CONFLICT, ["preflight", "fetch", "remote_oid"]),
    ],
)
def test_prelaunch_conflicts_do_not_acquire_or_start_launcher(tmp_path: Path, relation: str, remote: str | None, code: str, exit_code: int, expected_calls: list[str]) -> None:
    adapters = FakeAdapters(relation=relation, remote=remote); subject, log = run(tmp_path, adapters)
    with pytest.raises(SyncError) as caught: subject.run()
    assert (caught.value.code, caught.value.exit_code) == (code, exit_code)
    assert adapters.calls == expected_calls
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert row["state"] == "RECONCILE" and row["event"] == "failed" and row["code"] == code
    assert row["next_command"] == "grim-dawn-sync status" and {"timestamp_utc", "machine_id", "last_successful_state"} <= set(row)


def test_archive_failure_stops_before_apply_or_launcher_and_keeps_actionable_error(tmp_path: Path) -> None:
    failure = SyncError("archive_publish_failed", "Archive could not be published; live save was unchanged.", EXIT_VALIDATION)
    adapters = FakeAdapters(failure=("archive_before", failure)); subject, log = run(tmp_path, adapters)
    with pytest.raises(SyncError) as caught: subject.run()
    assert caught.value is failure and caught.value.exit_code == EXIT_VALIDATION
    assert adapters.calls == ["preflight", "fetch", "remote_oid", "acquire", "prepare", "archive_before"]
    assert "live save was unchanged" in caught.value.message
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert row["state"] == "ARCHIVE_BEFORE_RESTORE" and row["code"] == "archive_publish_failed" and row["session_id"] == "session-1"


def test_unexpected_adapter_failure_is_recovery_error_without_private_exception_text(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, log = run(tmp_path, adapters)
    def explode():
        adapters.calls.append("preflight")
        raise RuntimeError(f"private path {tmp_path} and token")
    adapters.preflight = explode
    with pytest.raises(SyncError) as caught: subject.run()
    assert (caught.value.code, caught.value.exit_code) == ("unexpected_failure", 2)
    assert "recover" in caught.value.message.lower()
    assert str(tmp_path) not in log.read_text(encoding="utf-8")
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert row["state"] == "PREFLIGHT" and row["code"] == "unexpected_failure" and "timestamp_utc" in row


@pytest.mark.parametrize(
    ("boundary", "error"),
    [
        ("acquire", SyncError("lock_race_lost", "x", EXIT_CONFLICT)),
        ("apply", SyncError("swap_failed_rolled_back", "x", EXIT_VALIDATION)),
        ("launch", SyncError("game_start_timeout", "x", 5)),
        ("validate", SyncError("save_invalid", "x", EXIT_VALIDATION)),
        ("snapshot", SyncError("git_commit_failed", "x", EXIT_RECOVERY_REQUIRED)),
        ("mark_committed", SyncError("state_write_failed", "x", EXIT_RECOVERY_REQUIRED)),
        ("push", SyncError("push_incomplete", "x", EXIT_RECOVERY_REQUIRED)),
        ("release", SyncError("release_incomplete", "x", EXIT_RECOVERY_REQUIRED)),
    ],
)
def test_every_mutation_boundary_stops_at_its_own_failure(tmp_path: Path, boundary: str, error: SyncError) -> None:
    adapters = FakeAdapters(failure=(boundary, error)); subject, _ = run(tmp_path, adapters)
    with pytest.raises(SyncError) as caught:
        subject.run()
    assert caught.value is error
    assert adapters.calls[-1] == ("rescue_raw" if boundary == "validate" else boundary)
    if boundary in {"acquire", "apply", "launch"}:
        assert "snapshot" not in adapters.calls and "push" not in adapters.calls
    if boundary in {"validate", "snapshot", "mark_committed"}:
        assert "push" not in adapters.calls and "release" not in adapters.calls
    if boundary == "push":
        assert "release" not in adapters.calls


@pytest.mark.parametrize(("live", "remote", "last", "base", "previous", "action"), [
    ("r", "r", "old", "a" * 40, "a" * 40, "noop"),
    ("old", "r", "old", "b" * 40, "a" * 40, "apply"),
    ("new", "old", "old", "a" * 40, "a" * 40, "noop"),
])
def test_three_way_reconcile_fake_matrix(tmp_path: Path, live: str, remote: str, last: str, base: str, previous: str, action: str) -> None:
    subject, _ = run(tmp_path, FakeAdapters())
    subject.adapters.state = lambda: SimpleNamespace(last_applied_remote_commit=previous, last_applied_manifest_root_hash=last)
    subject.adapters.live_manifest = lambda: {"root_hash": live}
    subject.adapters.remote_manifest = lambda base, session: {"root_hash": remote}
    assert subject._reconcile_three_way(base) == action


def test_three_way_missing_state_requires_bootstrap_without_mutation(tmp_path: Path) -> None:
    subject, _ = run(tmp_path, FakeAdapters())
    subject.adapters.state = lambda: (_ for _ in ()).throw(SyncError("state_missing", "x", 6))
    subject.adapters.live_manifest = lambda: {"root_hash": "x"}
    subject.adapters.remote_manifest = lambda *_: {"root_hash": "y"}
    with pytest.raises(SyncError) as got: subject._reconcile_three_way("a" * 40)
    assert (got.value.code, got.value.exit_code) == ("bootstrap_required", EXIT_CONFLICT)


def test_logger_failure_before_and_after_lock_has_safe_exit_codes(tmp_path: Path) -> None:
    class Broken:
        def write(self, *args, **kwargs): raise OSError("private")
    subject, _ = run(tmp_path, FakeAdapters()); subject.logger = Broken()
    with pytest.raises(SyncError) as before: subject.run()
    assert before.value.exit_code == 2
    subject, _ = run(tmp_path, FakeAdapters()); subject.mutated = True; subject.logger = Broken()
    with pytest.raises(SyncError) as after: subject._at(WorkflowState.PUSH)
    assert after.value.exit_code == EXIT_RECOVERY_REQUIRED


def test_incomplete_adapter_is_rejected_before_preflight_or_lock(tmp_path: Path) -> None:
    calls: list[str] = []
    adapter = SimpleNamespace(preflight=lambda: calls.append("preflight"))
    subject = LaunchWorkflow(config(tmp_path), tmp_path / "local", adapters=adapter)
    with pytest.raises(SyncError) as got:
        subject.run()
    assert (got.value.code, got.value.exit_code) == ("adapter_contract_invalid", 2)
    assert calls == []


def test_validation_failure_rescues_only_locally_and_retains_lock(tmp_path: Path) -> None:
    adapters = FakeAdapters(failure=("validate", SyncError("unsupported_save_version", "x", EXIT_VALIDATION)))
    adapters.state = lambda: SimpleNamespace(last_applied_remote_commit="b" * 40, last_applied_manifest_root_hash="old")
    adapters.live_manifest = lambda: {"root_hash":"old", "character_count":1, "file_count":1, "total_bytes":1}
    adapters.remote_manifest = lambda *_: {"root_hash":"new"}
    adapters.wait_save_stable = lambda: {"root_hash":"new", "character_count":1, "file_count":1, "total_bytes":1}
    adapters.validate = lambda *args: (_ for _ in ()).throw(SyncError("unsupported_save_version", "x", EXIT_VALIDATION))
    adapters.rescue_raw = lambda manifest: adapters.calls.append("rescue") or manifest["root_hash"]
    subject, _ = run(tmp_path, adapters)
    with pytest.raises(SyncError) as got: subject.run()
    assert got.value.code == "unsupported_save_version"
    assert "rescue" in adapters.calls and not {"snapshot", "push", "release"}.intersection(adapters.calls)
