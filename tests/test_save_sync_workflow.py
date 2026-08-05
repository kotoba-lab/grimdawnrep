from __future__ import annotations

import json
import inspect
from dataclasses import replace
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from grim_dawn_sync.config import parse_config
from grim_dawn_sync.errors import EXIT_CONFLICT, EXIT_RECOVERY_REQUIRED, EXIT_VALIDATION, SyncError
from grim_dawn_sync.session_lock import Lock, Session
from grim_dawn_sync.workflow import DomainAdapters, LaunchWorkflow, WorkflowAdapters, WorkflowState
from grim_dawn_sync.selection import ReconcileCase, SelectionRegistry
from grim_dawn_sync.state import SyncState, load_state, save_state
from grim_dawn_sync.version_catalog import ManifestDiff, SaveCandidate, VersionCatalog


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
    def acquire(self, base, *, expected_pre_state=None):
        self._call("acquire"); assert base == self.remote
        assert expected_pre_state is None
        return self.lock
    def align_selection_base(self, base): self._call("align"); assert base == self.remote
    def session_start_snapshot(self, expected_live_root_hash, *, session_id, launched_from_candidate_kind):
        self._call("session_start"); assert session_id == "session-1"
        return "save-session-start-" + "0" * 16 + "-" + "0" * 32
    def release_unmutated_lock(self, lock): self._call("release_unmutated"); assert lock is self.lock
    def release_without_publish(self, lock): self._call("release_without_publish"); assert lock is self.lock
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


def test_acquire_protocol_production_and_fake_share_expected_pre_state_keyword() -> None:
    for owner in (WorkflowAdapters, DomainAdapters, FakeAdapters):
        parameter = inspect.signature(owner.acquire).parameters["expected_pre_state"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None


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


def test_domain_automatic_bookmark_uses_the_acquired_lock_and_original_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = DomainAdapters(config(tmp_path), tmp_path / "local")
    lock = object()
    original = SyncState(
        last_applied_remote_commit="a" * 40,
        last_applied_manifest_root_hash="b" * 64,
        machine_id="test-machine",
    )
    subject._selection_lock = lock
    subject._selection_original_state = original
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "grim_dawn_sync.workflow.create_displaced_head_bookmark_locked",
        lambda vault, passed_lock, passed_original, commit, **kwargs: captured.update(
            vault=vault, lock=passed_lock, original=passed_original, commit=commit, **kwargs,
        ),
    )

    subject.bookmark_displaced_remote("a" * 40, object())  # type: ignore[arg-type]

    assert captured == {
        "vault": subject.vault,
        "lock": lock,
        "original": original,
        "commit": "a" * 40,
        "state_path": tmp_path / "local" / "state.json",
        "created_by": "test-machine",
    }


def test_domain_release_passes_manifest_root_into_atomic_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = DomainAdapters(config(tmp_path), tmp_path / "local")
    lock = object()
    commit, root_hash = "a" * 40, "b" * 64
    committed = SyncState(
        session_id="00000000-0000-4000-8000-000000000001",
        machine_id="test-machine",
        base_commit="c" * 40,
        lock_oid="d" * 40,
        local_tag="grim-dawn-sync-00000000-0000-4000-8000-000000000001",
        phase="committed",
        local_commit="e" * 40,
        last_applied_manifest_root_hash=root_hash,
    )
    subject._committed_state = committed
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
        "expected_state": committed,
    }
    assert subject._committed_state is None


def test_domain_release_without_publish_uses_the_selection_original_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = DomainAdapters(config(tmp_path), tmp_path / "local")
    lock = object()
    original = SyncState(
        last_applied_remote_commit="a" * 40,
        last_applied_manifest_root_hash="b" * 64,
        machine_id="test-machine",
    )
    subject._selection_original_state = original
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "grim_dawn_sync.workflow.release_without_publish",
        lambda vault, passed_lock, passed_original, **kwargs: captured.update(
            vault=vault, lock=passed_lock, original=passed_original, **kwargs,
        ),
    )
    subject.release_without_publish(lock)
    assert captured == {
        "vault": subject.vault, "lock": lock, "original": original,
        "state_path": tmp_path / "local" / "state.json",
    }


def test_domain_release_without_publish_requires_selection_context(tmp_path: Path) -> None:
    subject = DomainAdapters(config(tmp_path), tmp_path / "local")
    with pytest.raises(SyncError) as caught:
        subject.release_without_publish(object())
    assert caught.value.code == "adapter_contract_invalid"


def test_domain_mark_committed_semantic_cas_retains_foreign_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import grim_dawn_sync.workflow as module

    subject = DomainAdapters(config(tmp_path), tmp_path / "local")
    session_id = "00000000-0000-4000-8000-000000000001"
    session = Session(session_id, "test-machine", "a" * 40, "2026-08-01T00:00:00Z")
    lock = Lock(session, "b" * 40, f"grim-dawn-sync-{session_id}")
    locked = SyncState(
        last_applied_remote_commit=session.base_commit,
        last_applied_manifest_root_hash="c" * 64,
        session_id=session_id,
        machine_id=session.machine_id,
        base_commit=session.base_commit,
        lock_oid=lock.oid,
        local_tag=lock.local_tag,
        phase="lock_held",
    )
    foreign = replace(locked, last_applied_manifest_root_hash="d" * 64)
    save_state(subject.state_path, locked)
    actual_cas = module.save_state_if_unchanged

    def inject_race(path: Path, expected: SyncState, replacement: SyncState) -> None:
        assert expected == locked
        save_state(path, foreign)
        actual_cas(path, expected, replacement)

    monkeypatch.setattr(module, "save_state_if_unchanged", inject_race)
    with pytest.raises(SyncError) as caught:
        subject.mark_committed(lock, "e" * 40, "f" * 64)

    assert caught.value.code == "recovery_state_changed"
    assert caught.value.exit_code == EXIT_RECOVERY_REQUIRED
    assert load_state(subject.state_path) == foreign
    assert subject._committed_state is None


def test_domain_mark_committed_binds_exact_release_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = DomainAdapters(config(tmp_path), tmp_path / "local")
    session_id = "00000000-0000-4000-8000-000000000001"
    session = Session(session_id, "test-machine", "a" * 40, "2026-08-01T00:00:00Z")
    lock = Lock(session, "b" * 40, f"grim-dawn-sync-{session_id}")
    locked = SyncState(
        last_applied_remote_commit=session.base_commit,
        last_applied_manifest_root_hash="c" * 64,
        session_id=session_id,
        machine_id=session.machine_id,
        base_commit=session.base_commit,
        lock_oid=lock.oid,
        local_tag=lock.local_tag,
        phase="lock_held",
    )
    save_state(subject.state_path, locked)
    subject.mark_committed(lock, "d" * 40, "e" * 64)
    committed = load_state(subject.state_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "grim_dawn_sync.workflow.release_lock",
        lambda vault, passed_lock, oid, **kwargs: captured.update(
            vault=vault, lock=passed_lock, oid=oid, **kwargs,
        ),
    )

    subject.release(lock, "d" * 40, {"root_hash": "e" * 64})

    assert committed.phase == "committed"
    assert committed.local_commit == "d" * 40
    assert captured["expected_state"] == committed
    assert subject._committed_state is None


def test_domain_snapshot_binds_the_exact_validated_manifest(tmp_path: Path) -> None:
    subject = DomainAdapters(config(tmp_path), tmp_path / "local")
    manifest = {"root_hash": "b" * 64, "character_count": 1, "file_count": 1, "total_bytes": 1}
    captured: dict[str, object] = {}
    subject.vault = SimpleNamespace(snapshot=lambda source, **kwargs: captured.update(source=source, **kwargs) or "a" * 40)

    assert subject.snapshot("session-1", manifest, lambda _state: None) == "a" * 40
    assert captured["expected_manifest"] is manifest


def test_domain_validate_allows_only_total_bytes_to_decrease(tmp_path: Path) -> None:
    subject = DomainAdapters(config(tmp_path), tmp_path / "local")
    baseline = {
        "files": [{"path": "world/data", "size": 10, "sha256": "a" * 64}],
        "character_count": 0,
        "file_count": 1,
        "total_bytes": 10,
    }
    current = {
        "files": [{"path": "world/data", "size": 9, "sha256": "b" * 64}],
        "character_count": 0,
        "file_count": 1,
        "total_bytes": 9,
    }

    assert subject.validate(current, baseline) is current


def test_domain_validate_blocks_removed_file_even_when_count_is_unchanged(
    tmp_path: Path,
) -> None:
    subject = DomainAdapters(config(tmp_path), tmp_path / "local")
    baseline = {
        "files": [{"path": "world/old", "size": 5, "sha256": "a" * 64}],
        "character_count": 0,
        "file_count": 1,
        "total_bytes": 5,
    }
    current = {
        "files": [{"path": "world/new", "size": 5, "sha256": "b" * 64}],
        "character_count": 0,
        "file_count": 1,
        "total_bytes": 5,
    }
    quarantined: list[dict] = []
    subject.quarantine = quarantined.append

    with pytest.raises(SyncError) as caught:
        subject.validate(current, baseline)

    assert caught.value.code == "destructive_change"
    assert quarantined == [current]


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


def test_selection_stale_and_cancel_boundary_do_not_acquire_or_write(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, remote, commit = "1" * 64, "2" * 64, "a" * 40
    item = SaveCandidate("remote", "remote_head", "remote", "x", "m", remote, commit, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("t" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.REMOTE_AHEAD)
    adapters.live_manifest = lambda: {"root_hash": "f" * 64}
    adapters.remote_oid = lambda: commit
    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry)
    assert caught.value.code == "selection_stale" and adapters.calls == ["preflight"] and not subject.mutated


def test_selection_bookmarks_before_restore_and_post_lock_failure_has_recovery(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, remote, commit = "1" * 64, "2" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("u" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.LIVE_AHEAD)
    adapters.live_manifest = lambda: {"root_hash": live}
    adapters.remote_oid = lambda: commit
    adapters.bookmark_displaced_remote = lambda oid, _plan: adapters.calls.append("bookmark")
    adapters.wait_save_stable = lambda: {"root_hash": live, "character_count": 1, "file_count": 1, "total_bytes": 1}
    adapters.validate = lambda *_: (_ for _ in ()).throw(SyncError("save_invalid", "x", EXIT_VALIDATION))
    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry)
    assert caught.value.details["next_command"] == "grim-dawn-sync recover"
    assert adapters.calls.index("bookmark") < adapters.calls.index("launch")


def test_context_digest_and_remote_identity_revalidate_immediately_before_lock(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, remote, commit = "1" * 64, "2" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("v" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    identity = ("test://vault", "test://vault")
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch",
                               case=ReconcileCase.LIVE_AHEAD, expected_context_digest="d" * 64,
                               expected_remote_identity=identity)
    adapters.live_manifest = lambda: {"root_hash": live}
    adapters.remote_oid = lambda: commit
    adapters.bind_remote_identity = lambda value: adapters.calls.append("bind") if value == identity else pytest.fail("identity")
    def bookmark(*_args):
        adapters.calls.append("bookmark")
        raise SyncError("bookmark_push_incomplete", "x", EXIT_RECOVERY_REQUIRED)
    adapters.bookmark_displaced_remote = bookmark
    def context(digest):
        adapters.calls.append("context") if digest == "d" * 64 else pytest.fail("digest")
    def after_lock(digest, lock):
        assert digest == "d" * 64 and lock is adapters.lock
        adapters.calls.append("context_after_lock")
        return {"live_root_hash": live}
    context.after_lock = after_lock
    with pytest.raises(SyncError):
        subject.execute_selection_plan(plan, registry, context_revalidate=context)
    assert adapters.calls[:5] == ["preflight", "context", "bind", "acquire", "context_after_lock"]


def test_post_lock_attestation_reuses_fresh_live_root_before_first_mutation(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, commit = "1" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("v" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(
        catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch",
        case=ReconcileCase.LIVE_AHEAD, expected_context_digest="d" * 64,
        expected_remote_identity=("test://vault", "test://vault"),
    )
    live_calls: list[str] = []
    adapters.live_manifest = lambda: live_calls.append("hash") or {"root_hash": live}
    adapters.remote_oid = lambda: commit
    adapters.bind_remote_identity = lambda _value: None
    adapters.bookmark_displaced_remote = lambda *_args: (_ for _ in ()).throw(
        SyncError("stop_after_attestation", "x", EXIT_RECOVERY_REQUIRED)
    )
    def context(_digest): pass
    def after_lock(_digest, _lock): return {"live_root_hash": live}
    context.after_lock = after_lock

    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry, context_revalidate=context)

    assert caught.value.code == "stop_after_attestation"
    assert live_calls == ["hash", "hash"]


def test_context_change_across_lock_stops_before_plan_execution(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, commit = "1" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("v" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(
        catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch",
        case=ReconcileCase.LIVE_AHEAD, expected_context_digest="d" * 64,
        expected_remote_identity=("test://vault", "test://vault"),
    )
    adapters.live_manifest = lambda: {"root_hash": live}
    adapters.remote_oid = lambda: commit
    adapters.bind_remote_identity = lambda _value: adapters.calls.append("bind")

    def context(_digest):
        adapters.calls.append("context")
    def after_lock(_digest, _lock):
        adapters.calls.append("context_after_lock")
        raise SyncError("catalog_context_changed", "late change", EXIT_CONFLICT)
    context.after_lock = after_lock

    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry, context_revalidate=context)
    assert (caught.value.code, caught.value.exit_code) == ("catalog_context_changed", EXIT_RECOVERY_REQUIRED)
    assert caught.value.details["next_command"] == "grim-dawn-sync recover"
    assert adapters.calls[:5] == ["preflight", "context", "bind", "acquire", "context_after_lock"]
    assert "bookmark" not in adapters.calls and "align" not in adapters.calls and "prepare" not in adapters.calls


def test_acquire_gap_state_race_is_pre_mutation_selection_stale(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, commit = "1" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("v" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(
        catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch",
        case=ReconcileCase.LIVE_AHEAD, expected_context_digest="d" * 64,
        expected_remote_identity=("test://vault", "test://vault"),
    )
    adapters.live_manifest = lambda: {"root_hash": live}
    adapters.remote_oid = lambda: commit
    adapters.bind_remote_identity = lambda _value: adapters.calls.append("bind")
    expected_state = object()

    def acquire(_base, *, expected_pre_state=None):
        assert expected_pre_state is expected_state
        adapters.calls.append("acquire")
        raise SyncError("selection_stale", "state changed", EXIT_CONFLICT)
    adapters.acquire = acquire

    def context(_digest): adapters.calls.append("context")
    context.expected_pre_state = expected_state
    context.after_lock = lambda *_args: pytest.fail("no lock was acquired")

    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry, context_revalidate=context)
    assert caught.value.code == "selection_stale" and caught.value.details == {}
    assert adapters.calls[:4] == ["preflight", "context", "bind", "acquire"]
    assert "bookmark" not in adapters.calls and "align" not in adapters.calls and "prepare" not in adapters.calls


def test_promote_only_live_rechecks_selected_root_immediately_before_snapshot(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, commit = "1" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("v" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="promote-only", case=ReconcileCase.LIVE_AHEAD)
    roots = iter((live, live, live, "9" * 64))
    adapters.live_manifest = lambda: {"root_hash": next(roots)}
    adapters.remote_oid = lambda: commit
    adapters.bookmark_displaced_remote = lambda *_: adapters.calls.append("bookmark")
    adapters.wait_save_stable = lambda: {"root_hash": live, "character_count": 1, "file_count": 1, "total_bytes": 1}
    adapters.validate = lambda manifest, _baseline: adapters.calls.append("validate") or manifest
    adapters.archive_after_game = lambda manifest: adapters.calls.append("archive_after_game") or manifest["root_hash"]
    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry)
    assert caught.value.code == "selected_save_changed"
    assert "snapshot" not in adapters.calls and caught.value.details["next_command"] == "grim-dawn-sync recover"


def test_promote_only_verifies_published_remote_root_and_keeps_safe_handoff(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, commit = "1" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("w" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="promote-only", case=ReconcileCase.LIVE_AHEAD)
    adapters.live_manifest = lambda: {"root_hash": live}
    adapters.remote_oid = lambda: commit
    adapters.bookmark_displaced_remote = lambda *_: adapters.calls.append("bookmark")
    adapters.wait_save_stable = lambda: {"root_hash": live, "character_count": 1, "file_count": 1, "total_bytes": 1}
    adapters.validate = lambda manifest, _baseline: adapters.calls.append("validate") or manifest
    adapters.mark_committed = lambda *_: adapters.calls.append("mark_committed")
    adapters.remote_manifest = lambda *_: {"root_hash": "9" * 64}
    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry)
    assert caught.value.code == "selected_publish_mismatch"
    assert caught.value.details["local_commit"] == "c" * 40 and caught.value.details["root_hash"] == live
    assert "release" not in adapters.calls


def test_selection_post_lock_logging_failure_keeps_recovery_handoff(tmp_path: Path) -> None:
    class FailAfterAcquire:
        def write(self, state, *_args, **_kwargs):
            if state is WorkflowState.BOOKMARK_DISPLACED_REMOTE:
                raise OSError("audit")
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters); subject.logger = FailAfterAcquire()
    live, commit = "1" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("z" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.LIVE_AHEAD)
    adapters.live_manifest = lambda: {"root_hash": live}; adapters.remote_oid = lambda: commit
    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry)
    assert caught.value.code == "logging_failed" and caught.value.exit_code == EXIT_RECOVERY_REQUIRED
    assert caught.value.details["next_command"] == "grim-dawn-sync recover"


def test_selection_unexpected_post_lock_fault_is_normalized_for_recovery(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, commit = "1" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("q" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.LIVE_AHEAD)
    adapters.live_manifest = lambda: {"root_hash": live}; adapters.remote_oid = lambda: commit
    adapters.bookmark_displaced_remote = lambda *_: (_ for _ in ()).throw(OSError("private adapter cause"))
    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry)
    assert caught.value.code == "unexpected_failure" and caught.value.exit_code == EXIT_RECOVERY_REQUIRED
    assert caught.value.details["next_command"] == "grim-dawn-sync recover"
    assert "private adapter cause" not in str(caught.value)


@pytest.mark.parametrize("race", ["pre_lock", "post_lock"])
def test_remote_candidate_live_race_stops_before_any_save_mutation(tmp_path: Path, race: str) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, selected, commit = "1" * 64, "2" * 64, "a" * 40
    item = SaveCandidate("remote", "remote_head", "remote", "x", "m", selected, commit, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("r" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.REMOTE_AHEAD)
    roots = iter((live, "9" * 64)) if race == "pre_lock" else iter((live, live, "9" * 64))
    adapters.live_manifest = lambda: {"root_hash": next(roots)}; adapters.remote_oid = lambda: commit
    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry)
    assert caught.value.code == "selection_stale"
    assert not {"prepare", "archive_before", "apply"}.intersection(adapters.calls)
    if race == "pre_lock":
        assert "acquire" not in adapters.calls and not subject.mutated
    else:
        assert "acquire" in adapters.calls and caught.value.exit_code == EXIT_RECOVERY_REQUIRED
        assert caught.value.details["next_command"] == "grim-dawn-sync recover"


@pytest.mark.parametrize("stage", ["bookmark", "prepare", "archive_before", "apply", "launch", "wait", "validate", "archive_after_game", "snapshot", "mark_committed", "push", "remote_verify", "release"])
def test_every_selection_post_lock_sync_fault_is_recovery_required(tmp_path: Path, stage: str) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, selected, commit = "1" * 64, "2" * 64, "a" * 40
    remote_stage = stage in {"prepare", "archive_before", "apply"}
    kind = "remote_head" if remote_stage else "live"; root = selected if remote_stage else live; chosen = commit if remote_stage else None
    item = SaveCandidate("chosen", kind, kind, "x", "m", root, chosen, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("s" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    mode = "promote-only" if stage == "remote_verify" else "launch"
    case = ReconcileCase.REMOTE_AHEAD if remote_stage else ReconcileCase.LIVE_AHEAD
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode=mode, case=case)
    adapters.live_manifest = lambda: {"root_hash": live}; adapters.remote_oid = lambda: commit
    adapters.wait_save_stable = lambda: {"root_hash": root, "character_count": 1, "file_count": 1, "total_bytes": 1}
    adapters.validate = lambda manifest, _baseline: adapters._call("validate") or manifest
    adapters.mark_committed = lambda *_: adapters._call("mark_committed")
    adapters.bookmark_displaced_remote = lambda *_: adapters._call("bookmark")
    adapters.remote_manifest = lambda *_: adapters._call("remote_verify") or {"root_hash": root}
    fault = SyncError(f"fault_{stage}", "safe failure", EXIT_VALIDATION)
    if stage in {"prepare", "archive_before", "apply", "launch", "validate", "archive_after_game", "snapshot", "mark_committed", "push", "release"}:
        adapters.failure = (stage, fault)
    elif stage == "bookmark":
        adapters.bookmark_displaced_remote = lambda *_: (_ for _ in ()).throw(fault)
    elif stage == "wait":
        adapters.wait_save_stable = lambda: (_ for _ in ()).throw(fault)
        adapters.rescue_raw = lambda *_: "3" * 64
    else:
        adapters.remote_manifest = lambda *_: (_ for _ in ()).throw(fault)
    adapters.last_archive_destination = "save-20260801T000000Z-1111111111111111-" + "a" * 32
    adapters.last_archive_root = live
    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry)
    assert caught.value.code == f"fault_{stage}" and caught.value.message == "safe failure"
    assert caught.value.exit_code == EXIT_RECOVERY_REQUIRED
    assert caught.value.details["next_command"] == "grim-dawn-sync recover"
    assert caught.value.details["archive"].startswith("save-")


def test_session_start_snapshot_runs_immediately_after_lock_before_any_restore(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, commit = "1" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("w" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.LIVE_AHEAD)
    adapters.live_manifest = lambda: {"root_hash": live}
    adapters.remote_oid = lambda: commit
    adapters.bookmark_displaced_remote = lambda *_: adapters.calls.append("bookmark")
    assert subject.execute_selection_plan(plan, registry)["state"] == "COMPLETE"
    assert adapters.calls.index("acquire") < adapters.calls.index("session_start") < adapters.calls.index("launch")


def test_session_start_state_is_logged_between_acquire_lock_and_start_dpyes(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, log = run(tmp_path, adapters)
    live, commit = "1" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("x" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.LIVE_AHEAD)
    adapters.live_manifest = lambda: {"root_hash": live}
    adapters.remote_oid = lambda: commit
    adapters.bookmark_displaced_remote = lambda *_: adapters.calls.append("bookmark")
    assert subject.execute_selection_plan(plan, registry)["state"] == "COMPLETE"
    rows = [json.loads(line)["state"] for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows.index("ACQUIRE_LOCK") < rows.index("SESSION_START_SNAPSHOT") < rows.index("START_DPYES")


def test_session_start_failure_releases_lock_cleanly_without_forcing_recovery(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, commit = "1" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("y" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.LIVE_AHEAD)
    adapters.live_manifest = lambda: {"root_hash": live}
    adapters.remote_oid = lambda: commit
    fault = SyncError("session_start_disk_full", "Session-start archive could not be published.", EXIT_VALIDATION)
    adapters.session_start_snapshot = lambda *_a, **_k: (_ for _ in ()).throw(fault)
    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry)
    # Fail-closed with the *original* code/exit, not forced to recovery_required:
    # nothing mutated live, and the lock was cleanly released.
    assert caught.value.code == "session_start_disk_full"
    assert caught.value.exit_code == EXIT_VALIDATION
    assert "release_unmutated" in adapters.calls
    assert not subject.mutated
    assert {"launch", "archive_after_game", "snapshot", "push", "release"}.isdisjoint(adapters.calls)


def test_session_start_failure_with_unreleasable_lock_forces_recovery(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, commit = "1" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("z" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.LIVE_AHEAD)
    adapters.live_manifest = lambda: {"root_hash": live}
    adapters.remote_oid = lambda: commit
    fault = SyncError("session_start_disk_full", "Session-start archive could not be published.", EXIT_VALIDATION)
    adapters.session_start_snapshot = lambda *_a, **_k: (_ for _ in ()).throw(fault)
    adapters.release_unmutated_lock = lambda _lock: (_ for _ in ()).throw(RuntimeError("cleanup also failed"))
    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry)
    assert caught.value.exit_code == EXIT_RECOVERY_REQUIRED
    assert caught.value.details["next_command"] == "grim-dawn-sync recover"
    assert subject.mutated


def test_missing_session_start_adapter_method_is_a_contract_error(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    del adapters.__class__.session_start_snapshot  # simulate an incomplete adapter
    live, commit = "1" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("q" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.LIVE_AHEAD)
    adapters.live_manifest = lambda: {"root_hash": live}
    adapters.remote_oid = lambda: commit
    try:
        with pytest.raises(SyncError) as caught:
            subject.execute_selection_plan(plan, registry)
        assert caught.value.code == "adapter_contract_invalid"
        # The session-start snapshot is unconditional, so an adapter missing it
        # must be rejected up front like every other required method: before
        # preflight, before the lock, and therefore without needing recovery.
        assert caught.value.exit_code == 2
        assert not subject.mutated
        assert "acquire" not in adapters.calls and "preflight" not in adapters.calls
    finally:
        FakeAdapters.session_start_snapshot = lambda self, expected_live_root_hash, *, session_id, launched_from_candidate_kind: (
            self._call("session_start") or ("save-session-start-" + "0" * 16 + "-" + "0" * 32)
        )


def test_session_start_candidate_restores_from_local_archive_not_vault(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, selected, commit = "1" * 64, "2" * 64, "a" * 40
    archive_id = "save-session-start-" + "3" * 16 + "-" + "4" * 32
    item = SaveCandidate("chosen", "session_start", "session_start", "x", "m", selected, None, 0, 0, 0, (),
                         ManifestDiff(0, 0, 0), source_archive_id=archive_id)
    catalog = VersionCatalog("r" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.LIVE_AHEAD)
    plan = registry.confirm(plan)  # session_start requires the second confirmation
    adapters.live_manifest = lambda: {"root_hash": live}
    adapters.remote_oid = lambda: commit
    adapters.bookmark_displaced_remote = lambda *_: adapters.calls.append("bookmark")
    adapters.wait_save_stable = lambda: {"root_hash": selected, "character_count": 1, "file_count": 1, "total_bytes": 1}
    def prepare_local(passed_archive_id, session):
        adapters.calls.append("prepare_local")
        assert passed_archive_id == archive_id and session == "session-1"
        return "local-plan"
    adapters.prepare_local_restore = prepare_local
    def archive_before(plan_value):
        adapters.calls.append("archive_before"); assert plan_value == "local-plan"; return "archived-local-plan"
    adapters.archive_before_restore = archive_before
    def apply_local(plan_value):
        adapters.calls.append("apply"); assert plan_value == "archived-local-plan"; return {"ok": True}
    adapters.apply_remote_save = apply_local
    adapters.mark_committed = lambda *_: adapters.calls.append("mark_committed")
    adapters.push = lambda oid: adapters.calls.append("push") or "c" * 40
    adapters.release = lambda *_: adapters.calls.append("release")
    assert subject.execute_selection_plan(plan, registry)["state"] == "COMPLETE"
    assert "prepare" not in adapters.calls  # the vault-restore path must never run
    assert adapters.calls.index("prepare_local") < adapters.calls.index("apply")


def _launch_ready(tmp_path: Path):
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, commit = "1" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("s" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    adapters.live_manifest = lambda: {"root_hash": live}
    adapters.remote_oid = lambda: commit
    adapters.bookmark_displaced_remote = lambda *_: adapters.calls.append("bookmark")
    return adapters, subject, registry, catalog, item


def test_exit_disposition_local_only_skips_snapshot_and_push(tmp_path: Path) -> None:
    adapters, subject, registry, catalog, item = _launch_ready(tmp_path)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch",
                               case=ReconcileCase.LIVE_AHEAD, exit_disposition="local-only")
    result = subject.execute_selection_plan(plan, registry)
    assert result == {"state": "COMPLETE", "exit_disposition": "local-only"}
    assert "archive_after_game" in adapters.calls
    assert "release_without_publish" in adapters.calls
    for forbidden in ("snapshot", "mark_committed", "push", "release"):
        assert forbidden not in adapters.calls


def test_exit_disposition_restore_startup_restores_local_archive_and_skips_push(tmp_path: Path) -> None:
    adapters, subject, registry, catalog, item = _launch_ready(tmp_path)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch",
                               case=ReconcileCase.LIVE_AHEAD, exit_disposition="restore-startup")
    captured_archive_id: list[str] = []

    def prepare_local(archive_id, session):
        adapters.calls.append("prepare_local")
        captured_archive_id.append(archive_id)
        assert session.startswith("session-1")
        return SimpleNamespace(source_manifest={"root_hash": "startup-root"})

    adapters.prepare_local_restore = prepare_local
    adapters.archive_before_restore = lambda plan_value: (adapters.calls.append("archive_before_2") or plan_value)
    adapters.apply_remote_save = lambda plan_value: adapters.calls.append("apply_2")
    adapters.wait_save_stable = lambda: {"root_hash": "startup-root", "character_count": 1, "file_count": 1, "total_bytes": 1}

    result = subject.execute_selection_plan(plan, registry)
    assert result == {"state": "COMPLETE", "exit_disposition": "restore-startup"}
    # The session-start archive created right after lock acquisition is the
    # one restored, regardless of what the game session itself produced.
    assert captured_archive_id == ["save-session-start-" + "0" * 16 + "-" + "0" * 32]
    assert "archive_after_game" in adapters.calls
    assert "release_without_publish" in adapters.calls
    for forbidden in ("snapshot", "mark_committed", "push", "release"):
        assert forbidden not in adapters.calls


def test_exit_disposition_restore_startup_mismatch_requires_recovery(tmp_path: Path) -> None:
    adapters, subject, registry, catalog, item = _launch_ready(tmp_path)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch",
                               case=ReconcileCase.LIVE_AHEAD, exit_disposition="restore-startup")
    adapters.prepare_local_restore = lambda archive_id, session: SimpleNamespace(source_manifest={"root_hash": "expected-root"})
    adapters.archive_before_restore = lambda plan_value: plan_value
    adapters.apply_remote_save = lambda plan_value: None
    adapters.wait_save_stable = lambda: {"root_hash": "wrong-root", "character_count": 1, "file_count": 1, "total_bytes": 1}
    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry)
    assert caught.value.code == "selected_restore_mismatch"
    assert caught.value.exit_code == EXIT_RECOVERY_REQUIRED
    assert "release_without_publish" not in adapters.calls


def test_exit_disposition_gate_allows_downgrade_from_publish(tmp_path: Path) -> None:
    adapters, subject, registry, catalog, item = _launch_ready(tmp_path)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch",
                               case=ReconcileCase.LIVE_AHEAD)  # default exit_disposition="publish"
    result = subject.execute_selection_plan(plan, registry, exit_disposition_gate=lambda _current: "local-only")
    assert result == {"state": "COMPLETE", "exit_disposition": "local-only"}
    assert "push" not in adapters.calls and "release_without_publish" in adapters.calls


def test_exit_disposition_gate_cannot_move_away_from_a_downgrade(tmp_path: Path) -> None:
    """Once a plan is local-only/restore-startup, the gate may never move it anywhere else."""
    adapters, subject, registry, catalog, item = _launch_ready(tmp_path)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch",
                               case=ReconcileCase.LIVE_AHEAD, exit_disposition="local-only")
    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry, exit_disposition_gate=lambda _current: "publish")
    assert caught.value.code == "exit_disposition_transition_forbidden"
    # The lock is still held at this point, so this failure needs recover
    # like every other post-lock failure; nothing was published or restored.
    assert caught.value.exit_code == EXIT_RECOVERY_REQUIRED
    assert "push" not in adapters.calls and "release_without_publish" not in adapters.calls


def test_exit_disposition_gate_ignored_for_promote_only(tmp_path: Path) -> None:
    """promote-only always publishes; the gate is never even consulted."""
    adapters, subject, registry, catalog, item = _launch_ready(tmp_path)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="promote-only",
                               case=ReconcileCase.LIVE_AHEAD)
    adapters.wait_save_stable = lambda: {"root_hash": "1" * 64, "character_count": 1, "file_count": 1, "total_bytes": 1}
    adapters.validate = lambda manifest, _baseline: adapters.calls.append("validate") or manifest
    adapters.mark_committed = lambda *_: adapters.calls.append("mark_committed")
    adapters.remote_manifest = lambda *_: {"root_hash": "1" * 64}
    gate_calls: list[str] = []
    def gate(current):
        gate_calls.append(current)
        return "local-only"
    result = subject.execute_selection_plan(plan, registry, exit_disposition_gate=gate)
    assert result["state"] == "COMPLETE" and "commit" in result
    assert gate_calls == []


def test_lock_push_unknown_during_acquire_requires_recovery(tmp_path: Path) -> None:
    adapters = FakeAdapters(); subject, _ = run(tmp_path, adapters)
    live, commit = "1" * 64, "a" * 40
    item = SaveCandidate("live", "live", "live", "x", "m", live, None, 0, 0, 0, (), ManifestDiff(0, 0, 0))
    catalog = VersionCatalog("p" * 32, commit, live, (item,)); registry = SelectionRegistry(); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=item.candidate_id, mode="launch", case=ReconcileCase.LIVE_AHEAD)
    adapters.live_manifest = lambda: {"root_hash": live}; adapters.remote_oid = lambda: commit
    adapters.acquire = lambda _base: (_ for _ in ()).throw(SyncError("lock_push_unknown", "Lock result is unknown.", EXIT_CONFLICT))
    with pytest.raises(SyncError) as caught:
        subject.execute_selection_plan(plan, registry)
    assert caught.value.code == "lock_push_unknown" and caught.value.exit_code == EXIT_RECOVERY_REQUIRED
    assert caught.value.details["next_command"] == "grim-dawn-sync recover"
