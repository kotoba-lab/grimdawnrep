from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from grim_dawn_sync.errors import SyncError
from grim_dawn_sync.git_vault import GitResult, GitVault
from grim_dawn_sync.session_lock import (
    Lock,
    acquire_lock,
    inspect_remote_lock,
    inspect_remote_lock_readonly,
    mark_bootstrap_live_applied,
    prepare_bootstrap,
    push_bootstrap,
    recover_session,
    release_lock,
)
from grim_dawn_sync.state import SyncState, load_state, save_state


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, text=True, capture_output=True).stdout.strip()


def _vaults(tmp_path: Path) -> tuple[GitVault, GitVault]:
    remote = tmp_path / "remote.git"; _git(tmp_path, "init", "--bare", str(remote))
    seed = tmp_path / "seed"; _git(tmp_path, "clone", str(remote), str(seed))
    _git(seed, "config", "user.email", "test@example.invalid"); _git(seed, "config", "user.name", "test")
    (seed / "README").write_text("x", encoding="utf-8"); _git(seed, "add", "README"); _git(seed, "commit", "-m", "seed"); _git(seed, "branch", "-M", "main"); _git(seed, "push", "origin", "main")
    a, b = tmp_path / "a", tmp_path / "b"; _git(tmp_path, "clone", str(remote), str(a)); _git(tmp_path, "clone", str(remote), str(b))
    for clone in (a, b):
        _git(clone, "checkout", "-b", "main", "origin/main")
        _git(clone, "config", "user.email", "test@example.invalid"); _git(clone, "config", "user.name", "test")
    return GitVault(a), GitVault(b)


def _unborn_bootstrap(tmp_path: Path) -> tuple[GitVault, str]:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    clone = tmp_path / "bootstrap"
    _git(tmp_path, "clone", str(remote), str(clone))
    _git(clone, "config", "user.email", "test@example.invalid")
    _git(clone, "config", "user.name", "test")
    (clone / "save").write_text("bootstrap", encoding="utf-8")
    _git(clone, "add", "save")
    _git(clone, "commit", "-m", "bootstrap")
    return GitVault(clone), _git(clone, "rev-parse", "HEAD")


def _prepare_applied(
    vault: GitVault,
    commit: str,
    state_path: Path,
    root_hash: str = "d" * 64,
) -> SyncState:
    prepare_bootstrap(
        vault,
        "machine-a",
        commit,
        root_hash,
        state_path=state_path,
    )
    return mark_bootstrap_live_applied(
        "machine-a",
        commit,
        root_hash,
        root_hash,
        state_path=state_path,
    )


def _commit_session_snapshot(
    vault: GitVault,
    lock: Lock,
    *,
    machine_id: str = "machine-a",
    session_id: str | None = None,
    metadata_root: str | None = None,
) -> tuple[str, str]:
    relative = "main/a/player.gdc"
    content = b"recovered snapshot"
    digest = hashlib.sha256(content).hexdigest()
    root_hash = hashlib.sha256(
        f"{relative}\0{len(content)}\0{digest}".encode()
    ).hexdigest()
    target = vault.repo / "save" / "main" / "a"
    target.mkdir(parents=True, exist_ok=True)
    (target / "player.gdc").write_bytes(content)
    sync = vault.repo / ".sync"
    sync.mkdir(exist_ok=True)
    manifest = {
        "schema_version": "1.0.0",
        "created_at": "2026-07-29T00:00:00+00:00",
        "machine_id": machine_id,
        "root_hash": root_hash,
        "file_count": 1,
        "total_bytes": len(content),
        "character_count": 1,
        "files": [{"path": relative, "size": len(content), "sha256": digest}],
    }
    metadata = {
        "schema_version": "1.0.0",
        "machine_id": machine_id,
        "session_id": session_id or lock.session.session_id,
        "root_hash": metadata_root or root_hash,
    }
    (sync / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (sync / "vault.json").write_text(json.dumps(metadata), encoding="utf-8")
    _git(vault.repo, "add", "save", ".sync/manifest.json", ".sync/vault.json")
    _git(vault.repo, "commit", "-m", "session snapshot")
    return _git(vault.repo, "rev-parse", "HEAD"), root_hash


def _forged_lock_tag(
    vault: GitVault,
    tmp_path: Path,
    *,
    name: str,
    session_id: str,
    machine_id: str,
    tag_target: str,
    payload_base: str,
    canonical: bool = True,
) -> str:
    payload = {
        "schema_version": "1.0.0",
        "session_id": session_id,
        "machine_id": machine_id,
        "base_commit": payload_base,
        "started_at": "2026-07-28T00:00:00Z",
    }
    message = tmp_path / f"{name}.json"
    if canonical:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    else:
        body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    message.write_text(body, encoding="utf-8", newline="\n")
    _git(vault.repo, "tag", "-a", name, tag_target, "-F", str(message))
    return _git(vault.repo, "rev-parse", name)


def test_annotated_lock_is_exclusive_and_releases_only_its_session(tmp_path: Path) -> None:
    a, b = _vaults(tmp_path); base = a.remote_oid(); assert base
    state_a, state_b = tmp_path / "a-state.json", tmp_path / "b-state.json"
    lock = acquire_lock(a, "machine-a", base, state_path=state_a)
    assert lock.oid != _git(a.repo, "rev-parse", f"{lock.local_tag}^{{commit}}")
    assert inspect_remote_lock(b).session == lock.session  # type: ignore[union-attr]
    with pytest.raises(SyncError) as caught: acquire_lock(b, "machine-b", base, state_path=state_b)
    assert caught.value.exit_code == 4
    with pytest.raises(SyncError): release_lock(b, replace(lock, session=replace(lock.session, session_id="00000000-0000-4000-8000-000000000000")), base, state_path=state_b)
    release_lock(a, lock, base, state_path=state_a)
    assert inspect_remote_lock(a) is None


def test_readonly_lock_inspection_preserves_refs_objects_and_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import grim_dawn_sync.session_lock as module

    owner, observer = _vaults(tmp_path)
    base = owner.remote_oid()
    assert base
    lock = acquire_lock(owner, "machine-a", base, state_path=tmp_path / "owner-state.json")
    temp_parent = tmp_path / "inspection-temp"
    temp_parent.mkdir()
    monkeypatch.setattr(module.tempfile, "tempdir", str(temp_parent))
    before = {
        "refs": _git(observer.repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        "objects": _git(observer.repo, "count-objects", "-v"),
        "status": _git(observer.repo, "status", "--porcelain=v1", "--untracked-files=all"),
    }
    command_index = len(observer.runner.commands)

    inspected = inspect_remote_lock_readonly(observer)

    assert inspected == Lock(lock.session, lock.oid)
    assert {
        "refs": _git(observer.repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        "objects": _git(observer.repo, "count-objects", "-v"),
        "status": _git(observer.repo, "status", "--porcelain=v1", "--untracked-files=all"),
    } == before
    assert list(temp_parent.iterdir()) == []
    assert observer.runner.commands[command_index:] == [
        ("git", "ls-remote", "--refs", "origin", "refs/tags/grim-dawn-sync-active"),
        ("git", "remote", "get-url", "origin"),
    ]


def test_state_atomic_round_trip_and_corruption_fail_closed(tmp_path: Path) -> None:
    sid = "00000000-0000-4000-8000-000000000001"
    path = tmp_path / "state.json"; value = SyncState(session_id=sid, machine_id="m", base_commit="a" * 40, lock_oid="b" * 40, local_tag=f"grim-dawn-sync-{sid}", phase="lock_held")
    save_state(path, value); assert load_state(path) == value
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(SyncError) as caught: load_state(path)
    assert caught.value.exit_code == 6


def test_bootstrap_push_initializes_unborn_remote_without_lock_or_force(tmp_path: Path) -> None:
    vault, commit = _unborn_bootstrap(tmp_path)
    state_path = tmp_path / "state.json"
    root_hash = "d" * 64
    _prepare_applied(vault, commit, state_path, root_hash)

    assert push_bootstrap(
        vault,
        "machine-a",
        commit,
        root_hash,
        state_path=state_path,
    ) == commit

    assert vault.remote_oid() == commit
    assert inspect_remote_lock(vault) is None
    state = load_state(state_path)
    assert state == SyncState(
        last_applied_remote_commit=commit,
        last_applied_manifest_root_hash=root_hash,
        machine_id="machine-a",
    )
    pushes = [command for command in vault.runner.commands if len(command) > 1 and command[1] == "push"]
    assert pushes == [("git", "push", "origin", f"{commit}:refs/heads/main")]


def test_bootstrap_prepared_but_not_live_applied_never_inspects_or_pushes_remote(
    tmp_path: Path
) -> None:
    vault, commit = _unborn_bootstrap(tmp_path)
    state_path = tmp_path / "state.json"
    pending = prepare_bootstrap(
        vault,
        "machine-a",
        commit,
        "d" * 64,
        state_path=state_path,
    )
    command_index = len(vault.runner.commands)
    before = state_path.read_bytes()

    with pytest.raises(SyncError) as caught:
        recover_session(vault, pending, "machine-a", state_path=state_path)

    assert caught.value.code == "bootstrap_apply_required"
    assert caught.value.exit_code == 6
    assert vault.runner.commands[command_index:] == []
    assert state_path.read_bytes() == before


def test_bootstrap_live_marker_requires_exact_observed_root(tmp_path: Path) -> None:
    vault, commit = _unborn_bootstrap(tmp_path)
    state_path = tmp_path / "state.json"
    prepared = prepare_bootstrap(
        vault,
        "machine-a",
        commit,
        "d" * 64,
        state_path=state_path,
    )

    with pytest.raises(SyncError) as caught:
        mark_bootstrap_live_applied(
            "machine-a",
            commit,
            "d" * 64,
            "e" * 64,
            state_path=state_path,
        )

    assert caught.value.code == "bootstrap_live_mismatch"
    assert load_state(state_path) == prepared


def test_bootstrap_failed_push_stays_pending_then_recovery_pushes_exact_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, commit = _unborn_bootstrap(tmp_path)
    state_path = tmp_path / "state.json"
    root_hash = "d" * 64
    _prepare_applied(vault, commit, state_path, root_hash)
    original = vault.runner.run

    def fail_push(*args, **kwargs):
        if args and args[0] == "push":
            raise SyncError("git_unavailable", "injected failure", 2)
        return original(*args, **kwargs)

    monkeypatch.setattr(vault.runner, "run", fail_push)
    with pytest.raises(SyncError) as caught:
        push_bootstrap(vault, "machine-a", commit, root_hash, state_path=state_path)

    pending = load_state(state_path)
    assert caught.value.code == "bootstrap_push_incomplete"
    assert caught.value.exit_code == 6
    assert pending.phase == "bootstrap_pending"
    assert pending.local_commit == commit
    assert pending.last_applied_manifest_root_hash == root_hash

    monkeypatch.setattr(vault.runner, "run", original)
    assert recover_session(vault, pending, "machine-a", state_path=state_path) == "bootstrap_complete"
    assert vault.remote_oid() == commit
    assert load_state(state_path).last_applied_remote_commit == commit


def test_bootstrap_ambiguous_push_success_is_confirmed_and_finalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, commit = _unborn_bootstrap(tmp_path)
    state_path = tmp_path / "state.json"
    _prepare_applied(vault, commit, state_path)
    original = vault.runner.run

    def ambiguous_push(*args, **kwargs):
        result = original(*args, **kwargs)
        if args and args[0] == "push":
            return GitResult(tuple(args), result.stdout, "lost success response", 1)
        return result

    monkeypatch.setattr(vault.runner, "run", ambiguous_push)
    assert push_bootstrap(
        vault,
        "machine-a",
        commit,
        "d" * 64,
        state_path=state_path,
    ) == commit
    assert vault.remote_oid() == commit
    assert load_state(state_path).phase is None


def test_bootstrap_recovery_refuses_other_machine_and_competing_remote(
    tmp_path: Path
) -> None:
    vault, commit = _unborn_bootstrap(tmp_path)
    state_path = tmp_path / "state.json"
    pending = SyncState(
        last_applied_manifest_root_hash="d" * 64,
        machine_id="machine-a",
        phase="bootstrap_pending",
        local_commit=commit,
        bootstrap_live_applied=True,
    )
    save_state(state_path, pending)
    before = state_path.read_bytes()

    with pytest.raises(SyncError) as wrong_machine:
        recover_session(vault, pending, "machine-b", state_path=state_path)
    assert wrong_machine.value.code == "bootstrap_recovery_mismatch"
    assert vault.remote_oid() is None and state_path.read_bytes() == before

    competitor = tmp_path / "competitor"
    _git(tmp_path, "clone", str(tmp_path / "remote.git"), str(competitor))
    _git(competitor, "config", "user.email", "test@example.invalid")
    _git(competitor, "config", "user.name", "test")
    (competitor / "other").write_text("other", encoding="utf-8")
    _git(competitor, "add", "other")
    _git(competitor, "commit", "-m", "other")
    other = _git(competitor, "rev-parse", "HEAD")
    _git(competitor, "push", "origin", f"{other}:refs/heads/main")

    with pytest.raises(SyncError) as conflict:
        recover_session(vault, pending, "machine-a", state_path=state_path)
    assert conflict.value.code == "bootstrap_remote_conflict"
    assert vault.remote_oid() == other and state_path.read_bytes() == before


def test_bootstrap_final_state_failure_remains_pending_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import grim_dawn_sync.session_lock as module

    vault, commit = _unborn_bootstrap(tmp_path)
    state_path = tmp_path / "state.json"
    root_hash = "d" * 64
    _prepare_applied(vault, commit, state_path, root_hash)
    actual_save = module.save_state
    writes = 0

    def fail_final(path: Path, state: SyncState) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            raise SyncError("state_write_failed", "injected failure", 6)
        actual_save(path, state)

    monkeypatch.setattr(module, "save_state", fail_final)
    with pytest.raises(SyncError) as caught:
        push_bootstrap(vault, "machine-a", commit, root_hash, state_path=state_path)
    pending = load_state(state_path)
    assert caught.value.code == "state_write_failed"
    assert pending.phase == "bootstrap_pending"
    assert vault.remote_oid() == commit

    monkeypatch.setattr(module, "save_state", actual_save)
    assert recover_session(vault, pending, "machine-a", state_path=state_path) == "bootstrap_complete"
    assert load_state(state_path) == SyncState(
        last_applied_remote_commit=commit,
        last_applied_manifest_root_hash=root_hash,
        machine_id="machine-a",
    )


def test_recovery_pushes_exact_local_commit_then_releases(tmp_path: Path) -> None:
    vault, _ = _vaults(tmp_path); base = vault.remote_oid(); assert base
    state_path = tmp_path / "state.json"; lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    (vault.repo / "after").write_text("new", encoding="utf-8"); _git(vault.repo, "add", "after"); _git(vault.repo, "commit", "-m", "after")
    local = _git(vault.repo, "rev-parse", "HEAD")
    save_state(state_path, SyncState(session_id=lock.session.session_id, machine_id="machine-a", base_commit=base, lock_oid=lock.oid, local_tag=lock.local_tag, local_commit=local, phase="committed"))
    assert recover_session(vault, load_state(state_path), "machine-a", state_path=state_path) == "released"
    assert vault.remote_oid() == local and inspect_remote_lock(vault) is None


def test_recovery_adopts_verified_snapshot_head_then_pushes_and_releases(tmp_path: Path) -> None:
    vault, _ = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    state_path = tmp_path / "state.json"
    lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    commit, root_hash = _commit_session_snapshot(vault, lock)

    assert recover_session(
        vault,
        load_state(state_path),
        "machine-a",
        state_path=state_path,
    ) == "released"

    assert vault.remote_oid() == commit
    assert inspect_remote_lock_readonly(vault) is None
    assert load_state(state_path) == SyncState(
        last_applied_remote_commit=commit,
        last_applied_manifest_root_hash=root_hash,
        machine_id="machine-a",
    )


def test_recovery_adopts_snapshot_when_remote_already_has_exact_head(tmp_path: Path) -> None:
    vault, _ = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    state_path = tmp_path / "state.json"
    lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    commit, root_hash = _commit_session_snapshot(vault, lock)
    _git(vault.repo, "push", "origin", f"{commit}:refs/heads/main")

    assert recover_session(
        vault,
        load_state(state_path),
        "machine-a",
        state_path=state_path,
    ) == "released"
    assert load_state(state_path) == SyncState(
        last_applied_remote_commit=commit,
        last_applied_manifest_root_hash=root_hash,
        machine_id="machine-a",
    )


@pytest.mark.parametrize("tamper", ["machine", "session", "root"])
def test_recovery_rejects_snapshot_provenance_mismatch_without_mutation(
    tmp_path: Path, tamper: str
) -> None:
    vault, _ = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    state_path = tmp_path / "state.json"
    lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    changes = {
        "machine_id": "machine-b" if tamper == "machine" else "machine-a",
        "session_id": (
            "00000000-0000-4000-8000-000000000999"
            if tamper == "session"
            else lock.session.session_id
        ),
        "metadata_root": "e" * 64 if tamper == "root" else None,
    }
    _commit_session_snapshot(vault, lock, **changes)
    before = state_path.read_bytes()

    with pytest.raises(SyncError) as caught:
        recover_session(vault, load_state(state_path), "machine-a", state_path=state_path)

    assert caught.value.code == "recovery_commit_unproven"
    assert vault.remote_oid() == base
    assert inspect_remote_lock_readonly(vault).oid == lock.oid  # type: ignore[union-attr]
    assert state_path.read_bytes() == before


@pytest.mark.parametrize("tamper", ["wrong_parent", "unrelated_head", "dirty"])
def test_recovery_rejects_unproven_or_dirty_head_without_mutation(
    tmp_path: Path, tamper: str
) -> None:
    vault, _ = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    state_path = tmp_path / "state.json"
    lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    if tamper == "unrelated_head":
        (vault.repo / "unrelated").write_text("not a snapshot", encoding="utf-8")
        _git(vault.repo, "add", "unrelated")
        _git(vault.repo, "commit", "-m", "unrelated")
    else:
        _commit_session_snapshot(vault, lock)
        if tamper == "wrong_parent":
            (vault.repo / "extra").write_text("extra commit", encoding="utf-8")
            _git(vault.repo, "add", "extra")
            _git(vault.repo, "commit", "-m", "extra")
        else:
            (vault.repo / "dirty").write_text("untracked", encoding="utf-8")
    before = state_path.read_bytes()

    with pytest.raises(SyncError) as caught:
        recover_session(vault, load_state(state_path), "machine-a", state_path=state_path)

    assert caught.value.code == "recovery_commit_unproven"
    assert vault.remote_oid() == base
    assert inspect_remote_lock_readonly(vault).oid == lock.oid  # type: ignore[union-attr]
    assert state_path.read_bytes() == before


def test_recovery_adoption_state_failure_keeps_lock_state_and_never_pushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import grim_dawn_sync.session_lock as module

    vault, _ = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    state_path = tmp_path / "state.json"
    lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    commit, root_hash = _commit_session_snapshot(vault, lock)
    previous = state_path.read_bytes()
    actual_save = module.save_state
    monkeypatch.setattr(
        module,
        "save_state",
        lambda *_: (_ for _ in ()).throw(
            SyncError("state_write_failed", "injected adoption failure", 6)
        ),
    )

    with pytest.raises(SyncError) as caught:
        recover_session(vault, load_state(state_path), "machine-a", state_path=state_path)

    assert caught.value.code == "state_write_failed"
    assert vault.remote_oid() == base
    assert inspect_remote_lock_readonly(vault).oid == lock.oid  # type: ignore[union-attr]
    assert state_path.read_bytes() == previous

    monkeypatch.setattr(module, "save_state", actual_save)
    assert recover_session(
        vault,
        load_state(state_path),
        "machine-a",
        state_path=state_path,
    ) == "released"
    assert load_state(state_path) == SyncState(
        last_applied_remote_commit=commit,
        last_applied_manifest_root_hash=root_hash,
        machine_id="machine-a",
    )


def test_stale_base_and_state_write_failure_never_create_remote_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, _ = _vaults(tmp_path); base = vault.remote_oid(); assert base
    with pytest.raises(SyncError) as caught:
        acquire_lock(vault, "machine-a", "0" * 40, state_path=tmp_path / "stale.json")
    assert caught.value.code == "stale_lock_base" and inspect_remote_lock(vault) is None
    monkeypatch.setattr("grim_dawn_sync.session_lock.save_state", lambda *_: (_ for _ in ()).throw(SyncError("state_write_failed", "x", 6)))
    with pytest.raises(SyncError) as caught:
        acquire_lock(vault, "machine-a", base, state_path=tmp_path / "broken.json")
    assert caught.value.code == "state_write_failed" and inspect_remote_lock(vault) is None


def test_simultaneous_acquire_has_exactly_one_winner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = _vaults(tmp_path); base = a.remote_oid(); assert base
    import grim_dawn_sync.session_lock as module
    original = module._remote_lock; barrier = threading.Barrier(2); seen = threading.local()
    def synchronized(vault):
        if not getattr(seen, "done", False):
            seen.done = True
            result = original(vault)
            barrier.wait(timeout=10)
            return result
        return original(vault)
    monkeypatch.setattr(module, "_remote_lock", synchronized)
    def attempt(item):
        vault, machine, path = item
        try: return acquire_lock(vault, machine, base, state_path=path)
        except SyncError as error: return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [(a, "machine-a", tmp_path / "a.json"), (b, "machine-b", tmp_path / "b.json")]))
    assert sum(not isinstance(value, SyncError) for value in results) == 1
    assert sum(isinstance(value, SyncError) and value.code == "lock_race_lost" for value in results) == 1


def test_ambiguous_push_that_created_own_lock_is_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, _ = _vaults(tmp_path); base = vault.remote_oid(); assert base
    original = vault.runner.run
    def ambiguous(*args, **kwargs):
        result = original(*args, **kwargs)
        if args and args[0] == "push" and args[-1].endswith("grim-dawn-sync-active"):
            return GitResult(tuple(args), result.stdout, "transport lost", 1)
        return result
    monkeypatch.setattr(vault.runner, "run", ambiguous)
    lock = acquire_lock(vault, "machine-a", base, state_path=tmp_path / "state.json")
    assert inspect_remote_lock(vault).oid == lock.oid  # type: ignore[union-attr]


def test_recovery_rejects_machine_and_local_oid_mismatch_without_remote_change(tmp_path: Path) -> None:
    vault, _ = _vaults(tmp_path); base = vault.remote_oid(); assert base
    state_path = tmp_path / "state.json"; lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    state = load_state(state_path)
    with pytest.raises(SyncError) as caught:
        recover_session(vault, state, "machine-b", state_path=state_path)
    assert caught.value.code == "recovery_state_invalid" and inspect_remote_lock(vault).oid == lock.oid  # type: ignore[union-attr]
    with pytest.raises(SyncError) as caught:
        recover_session(vault, replace(state, lock_oid="0" * 40), "machine-a", state_path=state_path)
    assert caught.value.code == "recovery_local_tag_mismatch" and inspect_remote_lock(vault).oid == lock.oid  # type: ignore[union-attr]


@pytest.mark.parametrize("tamper", ["header", "json", "base"])
def test_inspection_rejects_noncanonical_header_json_and_base(
    tmp_path: Path, tamper: str
) -> None:
    vault, _ = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    other = base
    canonical = tamper != "json"
    payload_base = base
    if tamper in {"header", "base"}:
        (vault.repo / "other").write_text("other", encoding="utf-8")
        _git(vault.repo, "add", "other")
        _git(vault.repo, "commit", "-m", "other")
        other = _git(vault.repo, "rev-parse", "HEAD")
    if tamper == "header":
        tag_target = _git(vault.repo, "hash-object", "README")
    else:
        tag_target = base
    if tamper == "base":
        payload_base = other
    oid = _forged_lock_tag(
        vault,
        tmp_path,
        name=f"forged-{tamper}",
        session_id="00000000-0000-4000-8000-000000000101",
        machine_id="machine-a",
        tag_target=tag_target,
        payload_base=payload_base,
        canonical=canonical,
    )
    _git(vault.repo, "push", "--force", "origin", f"{oid}:refs/tags/grim-dawn-sync-active")

    with pytest.raises(SyncError) as caught:
        inspect_remote_lock(vault)

    assert caught.value.code == "invalid_lock"
    assert _git(vault.repo, "ls-remote", "--refs", "origin", "refs/tags/grim-dawn-sync-active").startswith(oid)


def test_release_rejects_machine_mismatch_without_mutating_remote(tmp_path: Path) -> None:
    vault, _ = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    state_path = tmp_path / "state.json"
    lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    forged = replace(lock, session=replace(lock.session, machine_id="machine-b"))

    with pytest.raises(SyncError) as caught:
        release_lock(vault, forged, base, state_path=state_path)

    assert caught.value.code == "release_lock_mismatch"
    assert inspect_remote_lock(vault).oid == lock.oid  # type: ignore[union-attr]
    assert load_state(state_path).machine_id == "machine-a"


def test_release_lease_replacement_race_preserves_other_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, other = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    state_path = tmp_path / "state.json"
    lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    replacement_oid = _forged_lock_tag(
        other,
        tmp_path,
        name="replacement",
        session_id="00000000-0000-4000-8000-000000000102",
        machine_id="machine-b",
        tag_target=base,
        payload_base=base,
    )
    original = vault.runner.run
    replaced = False

    def replace_before_delete(*args, **kwargs):
        nonlocal replaced
        if args and args[0] == "push" and len(args) > 1 and str(args[1]).startswith("--force-with-lease="):
            _git(other.repo, "push", "--force", "origin", f"{replacement_oid}:refs/tags/grim-dawn-sync-active")
            replaced = True
        return original(*args, **kwargs)

    monkeypatch.setattr(vault.runner, "run", replace_before_delete)
    with pytest.raises(SyncError) as caught:
        release_lock(vault, lock, base, state_path=state_path)

    assert replaced
    assert caught.value.code == "release_incomplete"
    remote_row = _git(vault.repo, "ls-remote", "--refs", "origin", "refs/tags/grim-dawn-sync-active")
    assert remote_row.startswith(replacement_oid)
    assert load_state(state_path).phase == "release_pending"


def test_release_delete_push_failure_persists_pending_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _ = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    state_path = tmp_path / "state.json"
    lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    original = vault.runner.run

    def fail_delete(*args, **kwargs):
        if args and args[0] == "push" and len(args) > 1 and str(args[1]).startswith("--force-with-lease="):
            return GitResult(tuple(args), "", "injected delete failure", 1)
        return original(*args, **kwargs)

    monkeypatch.setattr(vault.runner, "run", fail_delete)
    with pytest.raises(SyncError) as caught:
        release_lock(vault, lock, base, state_path=state_path)
    pending = load_state(state_path)
    assert caught.value.code == "release_incomplete"
    assert pending.phase == "release_pending"
    assert inspect_remote_lock(vault).oid == lock.oid  # type: ignore[union-attr]

    monkeypatch.setattr(vault.runner, "run", original)
    assert recover_session(vault, pending, "machine-a", state_path=state_path) == "released"
    assert inspect_remote_lock(vault) is None


def test_release_delete_success_but_confirmation_failure_recovers_absent_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _ = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    state_path = tmp_path / "state.json"
    lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    original = vault.runner.run
    deleted = False
    failed_confirmation = False

    def lose_confirmation(*args, **kwargs):
        nonlocal deleted, failed_confirmation
        result = original(*args, **kwargs)
        if args and args[0] == "push" and len(args) > 1 and str(args[1]).startswith("--force-with-lease="):
            deleted = True
            return result
        if deleted and not failed_confirmation and args and args[0] == "ls-remote" and args[-1] == "refs/tags/grim-dawn-sync-active":
            failed_confirmation = True
            raise SyncError("git_command_failed", "injected ls-remote failure", 4)
        return result

    monkeypatch.setattr(vault.runner, "run", lose_confirmation)
    with pytest.raises(SyncError) as caught:
        release_lock(vault, lock, base, state_path=state_path)
    pending = load_state(state_path)
    assert caught.value.code == "release_incomplete"
    assert failed_confirmation and pending.phase == "release_pending"

    monkeypatch.setattr(vault.runner, "run", original)
    assert inspect_remote_lock(vault) is None
    assert recover_session(vault, pending, "machine-a", state_path=state_path) == "complete"
    assert load_state(state_path).last_applied_remote_commit == base


@pytest.mark.parametrize("phase", ["committed", "pushed"])
def test_recovery_handles_already_pushed_commit_for_both_state_phases(
    tmp_path: Path, phase: str
) -> None:
    vault, _ = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    state_path = tmp_path / "state.json"
    lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    (vault.repo / "already-pushed").write_text(phase, encoding="utf-8")
    _git(vault.repo, "add", "already-pushed")
    _git(vault.repo, "commit", "-m", f"already pushed {phase}")
    commit = _git(vault.repo, "rev-parse", "HEAD")
    _git(vault.repo, "push", "origin", f"{commit}:refs/heads/main")
    state = SyncState(
        session_id=lock.session.session_id,
        machine_id="machine-a",
        base_commit=base,
        lock_oid=lock.oid,
        local_tag=lock.local_tag,
        local_commit=commit,
        pushed_commit=commit if phase == "pushed" else None,
        phase=phase,
    )
    save_state(state_path, state)

    assert recover_session(vault, state, "machine-a", state_path=state_path) == "released"
    assert vault.remote_oid() == commit
    assert inspect_remote_lock(vault) is None


def test_recovery_remote_main_diverged_does_not_mutate_any_ref_or_state(tmp_path: Path) -> None:
    vault, other = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    state_path = tmp_path / "state.json"
    lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    (vault.repo / "local").write_text("local", encoding="utf-8")
    _git(vault.repo, "add", "local")
    _git(vault.repo, "commit", "-m", "local")
    local = _git(vault.repo, "rev-parse", "HEAD")
    (other.repo / "remote").write_text("remote", encoding="utf-8")
    _git(other.repo, "add", "remote")
    _git(other.repo, "commit", "-m", "remote")
    _git(other.repo, "push", "origin", "HEAD:refs/heads/main")
    remote = vault.remote_oid()
    assert remote and remote not in {base, local}
    state = SyncState(
        session_id=lock.session.session_id,
        machine_id="machine-a",
        base_commit=base,
        lock_oid=lock.oid,
        local_tag=lock.local_tag,
        local_commit=local,
        phase="committed",
    )
    save_state(state_path, state)
    before = state_path.read_bytes()

    with pytest.raises(SyncError) as caught:
        recover_session(vault, state, "machine-a", state_path=state_path)

    assert caught.value.code == "recovery_remote_diverged"
    assert vault.remote_oid() == remote
    assert _git(vault.repo, "rev-parse", "HEAD") == local
    assert inspect_remote_lock(vault).oid == lock.oid  # type: ignore[union-attr]
    assert state_path.read_bytes() == before


def test_acquire_confirmation_failure_retains_intent_local_tag_and_remote_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _ = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    state_path = tmp_path / "state.json"
    original = vault.runner.run
    lock_queries = 0

    def fail_confirmation(*args, **kwargs):
        nonlocal lock_queries
        if args and args[0] == "ls-remote" and args[-1] == "refs/tags/grim-dawn-sync-active":
            lock_queries += 1
            if lock_queries == 2:
                raise SyncError("git_command_failed", "injected confirmation failure", 4)
        return original(*args, **kwargs)

    monkeypatch.setattr(vault.runner, "run", fail_confirmation)
    with pytest.raises(SyncError) as caught:
        acquire_lock(vault, "machine-a", base, state_path=state_path)

    assert caught.value.code == "lock_push_unknown"
    intent = load_state(state_path)
    assert intent.phase == "lock_held"
    assert intent.lock_oid
    assert _git(vault.repo, "rev-parse", intent.local_tag) == intent.lock_oid
    monkeypatch.setattr(vault.runner, "run", original)
    assert inspect_remote_lock(vault).oid == intent.lock_oid  # type: ignore[union-attr]


def test_release_local_tag_cleanup_failure_is_exit6_and_keeps_pending_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, _ = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    state_path = tmp_path / "state.json"
    lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    original = vault.runner.run

    def fail_local_delete(*args, **kwargs):
        if args[:2] == ("tag", "-d"):
            return GitResult(tuple(args), "", "injected local cleanup failure", 1)
        return original(*args, **kwargs)

    monkeypatch.setattr(vault.runner, "run", fail_local_delete)
    with pytest.raises(SyncError) as caught:
        release_lock(vault, lock, base, state_path=state_path)

    assert caught.value.code == "release_cleanup_incomplete"
    assert caught.value.exit_code == 6
    assert load_state(state_path).phase == "release_pending"
    monkeypatch.setattr(vault.runner, "run", original)
    assert inspect_remote_lock(vault) is None
    assert _git(vault.repo, "rev-parse", "--verify", f"refs/tags/{lock.local_tag}") == lock.oid


def test_release_final_state_failure_retains_root_hash_and_recovers_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import grim_dawn_sync.session_lock as module

    vault, _ = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    root_hash = "f" * 64
    state_path = tmp_path / "state.json"
    lock = acquire_lock(vault, "machine-a", base, state_path=state_path)
    actual_save_state = module.save_state
    writes = 0

    def fail_final_write(path: Path, state: SyncState) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise SyncError("state_write_failed", "injected final write failure", 6)
        actual_save_state(path, state)

    monkeypatch.setattr(module, "save_state", fail_final_write)
    with pytest.raises(SyncError) as caught:
        release_lock(
            vault,
            lock,
            base,
            state_path=state_path,
            confirmed_root_hash=root_hash,
        )

    pending = load_state(state_path)
    assert caught.value.code == "state_write_failed"
    assert pending.phase == "release_pending"
    assert pending.pushed_commit == base
    assert pending.last_applied_manifest_root_hash == root_hash
    assert inspect_remote_lock(vault) is None

    monkeypatch.setattr(module, "save_state", actual_save_state)
    assert recover_session(vault, pending, "machine-a", state_path=state_path) == "complete"
    complete = load_state(state_path)
    assert complete.phase is None
    assert complete.last_applied_remote_commit == base
    assert complete.last_applied_manifest_root_hash == root_hash


def test_second_stale_base_check_cleans_intent_and_local_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, other = _vaults(tmp_path)
    base = vault.remote_oid()
    assert base
    state_path = tmp_path / "state.json"
    original_remote_oid = vault.remote_oid
    calls = 0

    def advance_before_second_check():
        nonlocal calls
        calls += 1
        if calls == 2:
            (other.repo / "advance").write_text("advance", encoding="utf-8")
            _git(other.repo, "add", "advance")
            _git(other.repo, "commit", "-m", "advance")
            _git(other.repo, "push", "origin", "HEAD:refs/heads/main")
        return original_remote_oid()

    monkeypatch.setattr(vault, "remote_oid", advance_before_second_check)
    with pytest.raises(SyncError) as caught:
        acquire_lock(vault, "machine-a", base, state_path=state_path)

    assert caught.value.code == "stale_lock_base"
    assert calls == 2
    assert load_state(state_path) == SyncState()
    assert inspect_remote_lock(vault) is None
    assert not _git(vault.repo, "tag", "--list", "grim-dawn-sync-*")
