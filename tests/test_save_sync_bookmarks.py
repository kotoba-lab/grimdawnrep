from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import grim_dawn_sync.bookmarks as bookmarks_module
import grim_dawn_sync.session_lock as session_lock_module

from grim_dawn_sync.bookmarks import (
    _create_bookmark_test_only,
    create_commit_bookmark_locked,
    create_displaced_head_bookmark_locked,
    create_live_bookmark_locked,
    make_bookmark_annotation,
    parse_bookmark_annotation,
)
from grim_dawn_sync.errors import SyncError
from grim_dawn_sync.git_vault import GitResult, GitVault
from grim_dawn_sync.manifest import stable_manifest
from grim_dawn_sync.session_lock import LOCK_REF, acquire_lock, recover_session, release_bookmark_lock
from grim_dawn_sync.state import SyncState, load_state, save_state
from grim_dawn_sync.version_catalog import VersionCatalogBuilder
from test_save_sync_git_vault import checkout_remote_main, clone_pair, git, save, valid


def _ordinary_context(tmp_path):
    _, one, _ = clone_pair(tmp_path)
    live = save(tmp_path / "live", b"baseline")
    vault = GitVault(one, branch="master")
    manifest = stable_manifest(live, machine_id="a", retries=1)
    base = vault.snapshot(live, machine_id="a", session_id="baseline", validator=valid)
    vault.push(base)
    original = SyncState(
        last_applied_remote_commit=base,
        last_applied_manifest_root_hash=manifest["root_hash"],
        machine_id="a",
    )
    state_path = tmp_path / "state.local.json"
    save_state(state_path, original)
    return one, vault, manifest, base, original, state_path


def test_annotation_rejects_duplicate_control_and_bounds() -> None:
    with pytest.raises(SyncError):
        parse_bookmark_annotation('{"schema_version":"1.0.0","schema_version":"1.0.0"}')
    with pytest.raises(SyncError):
        make_bookmark_annotation("bad\nname", None, created_by="machine")
    with pytest.raises(SyncError):
        make_bookmark_annotation("x" * 81, None, created_by="machine")
    with pytest.raises(SyncError):
        make_bookmark_annotation("ok", "x" * 501, created_by="machine")


def test_lockless_bookmark_seed_is_private_and_unreachable_from_production_adapters() -> None:
    from grim_dawn_sync.workflow import DomainAdapters, WorkflowAdapters

    assert not hasattr(bookmarks_module, "create_bookmark")
    assert "create_bookmark" not in WorkflowAdapters.__dict__
    assert "create_bookmark" not in DomainAdapters.__dict__
    production_root = Path(bookmarks_module.__file__).parent
    callers = [
        path.name for path in production_root.glob("*.py")
        if path.name != "bookmarks.py" and "_create_bookmark_test_only" in path.read_text(encoding="utf-8")
    ]
    assert callers == []


def test_create_bookmark_is_annotated_remote_verified_and_catalogued(tmp_path) -> None:
    _, one, two = clone_pair(tmp_path)
    source = save(tmp_path / "source", b"one")
    vault = GitVault(one)
    commit = vault.snapshot(source, machine_id="a", session_id="first", validator=valid)
    vault.push(commit)
    made = _create_bookmark_test_only(vault, commit, display_name="material storage", note="keep", created_by="a")
    assert made.commit == commit and made.ref.startswith("grim-dawn-save-")
    other = GitVault(two)
    rows = other.managed_bookmarks()
    assert len(rows) == 1 and rows[0][0:2] == (made.ref, commit)
    assert parse_bookmark_annotation(rows[0][2])["display_name"] == "material storage"
    catalog = VersionCatalogBuilder(other, source, machine_id="a").build()
    # The duplicate root is intentionally coalesced with the live candidate.
    assert catalog.candidates[0].kind == "live"


def test_commit_candidate_bookmark_uses_lock_and_restores_inactive_baseline(tmp_path) -> None:
    _, one, _ = clone_pair(tmp_path)
    live = save(tmp_path / "live", b"baseline")
    vault = GitVault(one)
    manifest = stable_manifest(live, machine_id="a", retries=1)
    base = vault.snapshot(live, machine_id="a", session_id="baseline", validator=valid)
    vault.push(base)
    original = SyncState(
        last_applied_remote_commit=base,
        last_applied_manifest_root_hash=manifest["root_hash"],
        machine_id="a",
    )
    state_path = tmp_path / "state.local.json"
    save_state(state_path, original)
    before_head = git(one, "rev-parse", "HEAD").strip()
    before_status = git(one, "status", "--porcelain=v1", "--untracked-files=all")
    before_live = (live / "main" / "a" / "player.gdc").read_bytes()

    made = create_commit_bookmark_locked(
        vault,
        base,
        manifest["root_hash"],
        state_path=state_path,
        expected_remote_head=base,
        display_name="locked ordinary",
        note=None,
        created_by="a",
    )

    assert load_state(state_path) == original
    assert not git(one, "ls-remote", "--refs", "origin", LOCK_REF)
    assert (made.ref, git(one, "rev-parse", f"refs/tags/{made.ref}").strip(), base) in vault._managed_bookmark_rows(remote=True)
    assert git(one, "ls-remote", "--refs", "origin", "refs/heads/main").split()[0] == base
    assert git(one, "rev-parse", "HEAD").strip() == before_head
    assert git(one, "status", "--porcelain=v1", "--untracked-files=all") == before_status
    assert (live / "main" / "a" / "player.gdc").read_bytes() == before_live


@pytest.mark.parametrize("relationship", ["remote_ahead", "diverged"])
def test_abandoned_bookmark_lock_restores_exact_nonremote_baseline(tmp_path, relationship: str) -> None:
    one, vault, _manifest, base, _original, state_path = _ordinary_context(tmp_path)
    prior = base
    if relationship == "diverged":
        git(one, "checkout", "-b", "diverged-baseline", base)
        git(one, "commit", "--allow-empty", "-m", "diverged baseline")
        prior = git(one, "rev-parse", "HEAD").strip()
        git(one, "checkout", "master")
    live = tmp_path / "live"
    (live / "main" / "a" / "player.gdc").write_bytes(b"new remote base")
    base = vault.snapshot(live, machine_id="a", session_id="remote-new", validator=valid)
    vault.push(base)
    original = SyncState(
        last_applied_remote_commit=prior,
        last_applied_manifest_root_hash="d" * 64,
        machine_id="a",
    )
    save_state(state_path, original)
    before_live = (tmp_path / "live" / "main" / "a" / "player.gdc").read_bytes()

    lock = acquire_lock(vault, "a", base, state_path=state_path, expected_pre_state=original)
    locked = load_state(state_path)
    assert locked.last_applied_remote_commit == prior
    assert locked.last_applied_manifest_root_hash == "d" * 64

    assert recover_session(vault, locked, "a", state_path=state_path) == "abandoned_lock_released"
    assert load_state(state_path) == original
    assert vault.remote_oid() == base
    assert not git(one, "ls-remote", "--refs", "origin", LOCK_REF)
    assert not vault.runner.run(
        "rev-parse", "--verify", "--quiet", f"refs/tags/{lock.local_tag}", check=False,
    ).stdout.strip()
    assert (tmp_path / "live" / "main" / "a" / "player.gdc").read_bytes() == before_live


def test_abandoned_bookmark_lock_restores_empty_baseline(tmp_path) -> None:
    one, vault, _manifest, base, _original, state_path = _ordinary_context(tmp_path)
    original = SyncState()
    save_state(state_path, original)

    acquire_lock(vault, "a", base, state_path=state_path, expected_pre_state=original)
    locked = load_state(state_path)
    assert locked.last_applied_remote_commit is None
    assert locked.last_applied_manifest_root_hash is None

    assert recover_session(vault, locked, "a", state_path=state_path) == "abandoned_lock_released"
    assert load_state(state_path) == original
    assert vault.remote_oid() == base
    assert not git(one, "ls-remote", "--refs", "origin", LOCK_REF)


def test_automatic_backup_then_crash_preserves_nonremote_baseline_for_recovery(tmp_path) -> None:
    one, vault, _manifest, base, _original, state_path = _ordinary_context(tmp_path)
    prior = base
    live = tmp_path / "live"
    (live / "main" / "a" / "player.gdc").write_bytes(b"new remote base")
    base = vault.snapshot(live, machine_id="a", session_id="remote-new", validator=valid)
    vault.push(base)
    original = SyncState(
        last_applied_remote_commit=prior,
        last_applied_manifest_root_hash="e" * 64,
        machine_id="a",
    )
    save_state(state_path, original)
    before_live = (tmp_path / "live" / "main" / "a" / "player.gdc").read_bytes()
    lock = acquire_lock(vault, "a", base, state_path=state_path, expected_pre_state=original)

    made = create_displaced_head_bookmark_locked(
        vault, lock, original, base, state_path=state_path, created_by="a",
    )
    continued = load_state(state_path)
    assert continued.phase == "lock_held"
    assert continued.last_applied_remote_commit == original.last_applied_remote_commit
    assert continued.last_applied_manifest_root_hash == original.last_applied_manifest_root_hash

    assert recover_session(vault, continued, "a", state_path=state_path) == "abandoned_lock_released"
    assert load_state(state_path) == original
    assert vault.remote_oid() == base
    assert not git(one, "ls-remote", "--refs", "origin", LOCK_REF)
    assert (made.ref, git(one, "rev-parse", f"refs/tags/{made.ref}").strip(), base) in vault._managed_bookmark_rows(remote=True)
    assert (tmp_path / "live" / "main" / "a" / "player.gdc").read_bytes() == before_live


def test_bookmark_release_cas_conflict_preserves_foreign_state_and_lock(tmp_path, monkeypatch) -> None:
    one, vault, _manifest, base, original, state_path = _ordinary_context(tmp_path)
    lock = acquire_lock(vault, "a", base, state_path=state_path, expected_pre_state=original)
    current = load_state(state_path)
    foreign = replace(current, last_applied_manifest_root_hash="f" * 64)
    real_cas = session_lock_module.save_state_if_unchanged

    def race_release(path, expected, desired):
        if desired.phase == "bookmark_release_pending":
            save_state(path, foreign)
        return real_cas(path, expected, desired)

    monkeypatch.setattr(session_lock_module, "save_state_if_unchanged", race_release)
    with pytest.raises(SyncError) as caught:
        release_bookmark_lock(vault, lock, original, state_path=state_path)

    assert (caught.value.code, caught.value.exit_code) == ("bookmark_release_state_changed", 6)
    assert load_state(state_path) == foreign
    assert git(one, "ls-remote", "--refs", "origin", LOCK_REF).startswith(lock.oid)
    assert git(one, "rev-parse", "--verify", f"refs/tags/{lock.local_tag}").strip() == lock.oid


def test_commit_bookmark_remote_applied_timeout_keeps_same_intent_until_retry_succeeds(tmp_path, monkeypatch) -> None:
    _one, vault, manifest, base, original, state_path = _ordinary_context(tmp_path)
    real_run = vault.runner.run
    interrupted = False

    def apply_then_timeout(*args, **kwargs):
        nonlocal interrupted
        if args and args[0] == "push" and "--atomic" in args and not interrupted:
            interrupted = True
            real_run(*args, **kwargs)
            raise SyncError("git_timeout", "injected timeout", 4)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(vault.runner, "run", apply_then_timeout)
    with pytest.raises(SyncError) as caught:
        create_commit_bookmark_locked(
            vault, base, manifest["root_hash"], state_path=state_path,
            expected_remote_head=base, display_name="ambiguous", note=None, created_by="a",
        )
    assert caught.value.exit_code == 6
    pending = load_state(state_path)
    assert pending.phase == "bookmark_publish_pending" and pending.bookmark_ref and pending.bookmark_tag_oid

    monkeypatch.setattr(vault.runner, "run", real_run)
    real_rows = vault._managed_bookmark_rows
    first_verify = True
    def fail_first_verify(*, remote: bool):
        nonlocal first_verify
        if remote and first_verify:
            first_verify = False
            raise SyncError("git_timeout", "injected first recovery verify failure", 4)
        return real_rows(remote=remote)
    monkeypatch.setattr(vault, "_managed_bookmark_rows", fail_first_verify)
    with pytest.raises(SyncError) as retry:
        recover_session(vault, pending, "a", state_path=state_path)
    assert retry.value.exit_code == 6 and load_state(state_path) == pending

    monkeypatch.setattr(vault, "_managed_bookmark_rows", real_rows)
    assert recover_session(vault, pending, "a", state_path=state_path) == "bookmark_released"
    assert load_state(state_path) == original
    assert (pending.bookmark_ref, pending.bookmark_tag_oid, pending.local_commit) in vault._managed_bookmark_rows(remote=True)


def test_commit_bookmark_unapplied_unknown_result_replays_exact_persisted_tag(tmp_path, monkeypatch) -> None:
    _one, vault, manifest, base, original, state_path = _ordinary_context(tmp_path)
    real_run = vault.runner.run
    suppressed = False

    def do_not_apply_once(*args, **kwargs):
        nonlocal suppressed
        if args and args[0] == "push" and "--atomic" in args and not suppressed:
            suppressed = True
            return GitResult(args, "", "injected unknown result", 1)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(vault.runner, "run", do_not_apply_once)
    with pytest.raises(SyncError) as caught:
        create_commit_bookmark_locked(
            vault, base, manifest["root_hash"], state_path=state_path,
            expected_remote_head=base, display_name="replay", note=None, created_by="a",
        )
    assert caught.value.exit_code == 6
    pending = load_state(state_path)
    assert pending.phase == "bookmark_publish_pending"
    assert not vault._managed_bookmark_rows(remote=True)

    monkeypatch.setattr(vault.runner, "run", real_run)
    assert recover_session(vault, pending, "a", state_path=state_path) == "bookmark_released"
    assert load_state(state_path) == original
    assert (pending.bookmark_ref, pending.bookmark_tag_oid, pending.local_commit) in vault._managed_bookmark_rows(remote=True)


def test_recovery_publish_confirmation_state_cas_race_keeps_foreign_state_and_artifacts(tmp_path, monkeypatch) -> None:
    one, vault, manifest, base, _original, state_path = _ordinary_context(tmp_path)
    real_run = vault.runner.run
    suppressed = False

    def do_not_apply_once(*args, **kwargs):
        nonlocal suppressed
        if args and args[0] == "push" and "--atomic" in args and not suppressed:
            suppressed = True
            return GitResult(args, "", "injected unknown result", 1)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(vault.runner, "run", do_not_apply_once)
    with pytest.raises(SyncError):
        create_commit_bookmark_locked(
            vault, base, manifest["root_hash"], state_path=state_path,
            expected_remote_head=base, display_name="state race", note=None, created_by="a",
        )
    pending = load_state(state_path)
    assert pending.phase == "bookmark_publish_pending"
    monkeypatch.setattr(vault.runner, "run", real_run)

    real_cas = session_lock_module.save_state_if_unchanged
    foreign = replace(pending, bookmark_root_hash="f" * 64)

    def race_after_publish(path, expected, desired):
        if expected == pending and desired.phase == "bookmark_release_pending":
            save_state(path, foreign)
        return real_cas(path, expected, desired)

    monkeypatch.setattr(session_lock_module, "save_state_if_unchanged", race_after_publish)
    with pytest.raises(SyncError) as caught:
        recover_session(vault, pending, "a", state_path=state_path)

    assert (caught.value.code, caught.value.exit_code) == ("bookmark_recovery_state_changed", 6)
    assert load_state(state_path) == foreign
    assert git(one, "ls-remote", "--refs", "origin", LOCK_REF).startswith(pending.lock_oid)
    assert (pending.bookmark_ref, pending.bookmark_tag_oid, pending.local_commit) in vault._managed_bookmark_rows(remote=True)
    assert git(one, "rev-parse", f"refs/tags/{pending.bookmark_ref}").strip() == pending.bookmark_tag_oid
    assert git(one, "rev-parse", f"refs/tags/{pending.local_tag}").strip() == pending.lock_oid


@pytest.mark.parametrize("mismatch", ["tag-object", "peeled-target"])
def test_commit_bookmark_recovery_rejects_existing_remote_ref_mismatch(tmp_path, monkeypatch, mismatch: str) -> None:
    one, vault, manifest, base, _original, state_path = _ordinary_context(tmp_path)
    real_run = vault.runner.run
    suppressed = False
    def do_not_apply_once(*args, **kwargs):
        nonlocal suppressed
        if args and args[0] == "push" and "--atomic" in args and not suppressed:
            suppressed = True
            return GitResult(args, "", "injected", 1)
        return real_run(*args, **kwargs)
    monkeypatch.setattr(vault.runner, "run", do_not_apply_once)
    with pytest.raises(SyncError):
        create_commit_bookmark_locked(
            vault, base, manifest["root_hash"], state_path=state_path,
            expected_remote_head=base, display_name="conflict", note=None, created_by="a",
        )
    pending = load_state(state_path)
    monkeypatch.setattr(vault.runner, "run", real_run)

    target = base
    if mismatch == "peeled-target":
        git(one, "commit", "--allow-empty", "-m", "foreign target")
        target = git(one, "rev-parse", "HEAD").strip()
    git(one, "tag", "-a", "foreign-bookmark", target, "-m", "different annotation")
    git(one, "push", "origin", f"refs/tags/foreign-bookmark:refs/tags/{pending.bookmark_ref}")

    with pytest.raises(SyncError) as caught:
        recover_session(vault, pending, "a", state_path=state_path)
    assert (caught.value.code, caught.value.exit_code) == ("bookmark_push_incomplete", 6)
    assert load_state(state_path) == pending


@pytest.mark.parametrize("fault", ["identity", "list", "annotation"])
def test_commit_bookmark_post_intent_failures_remain_recoverable(tmp_path, monkeypatch, fault: str) -> None:
    _one, vault, manifest, base, original, state_path = _ordinary_context(tmp_path)
    real_identity = vault.assert_remote_identity
    real_rows = vault._managed_bookmark_rows
    real_run = vault.runner.run

    if fault == "identity":
        def fail_identity_after_intent():
            if load_state(state_path).phase == "bookmark_publish_pending":
                raise SyncError("remote_identity_changed", "injected", 4)
            return real_identity()
        monkeypatch.setattr(vault, "assert_remote_identity", fail_identity_after_intent)
    elif fault == "list":
        def fail_list_after_intent(*, remote: bool):
            if remote and load_state(state_path).phase == "bookmark_publish_pending":
                raise SyncError("bookmark_list_failed", "injected", 4)
            return real_rows(remote=remote)
        monkeypatch.setattr(vault, "_managed_bookmark_rows", fail_list_after_intent)
    else:
        def fail_annotation(*args, **kwargs):
            if args[:2] == ("cat-file", "tag") and str(args[2]).startswith("refs/tags/grim-dawn-save-"):
                return GitResult(args, "", "injected", 1)
            return real_run(*args, **kwargs)
        monkeypatch.setattr(vault.runner, "run", fail_annotation)

    with pytest.raises(SyncError) as caught:
        create_commit_bookmark_locked(
            vault, base, manifest["root_hash"], state_path=state_path,
            expected_remote_head=base, display_name="recover fault", note=None, created_by="a",
        )
    assert caught.value.exit_code == 6
    pending = load_state(state_path)
    assert pending.phase == "bookmark_publish_pending" and pending.bookmark_ref and pending.bookmark_tag_oid

    monkeypatch.setattr(vault, "assert_remote_identity", real_identity)
    monkeypatch.setattr(vault, "_managed_bookmark_rows", real_rows)
    monkeypatch.setattr(vault.runner, "run", real_run)
    assert recover_session(vault, pending, "a", state_path=state_path) == "bookmark_released"
    assert load_state(state_path) == original


def test_commit_bookmark_lock_disappearance_does_not_publish_and_recovers_baseline(tmp_path) -> None:
    one, vault, manifest, base, original, state_path = _ordinary_context(tmp_path)
    deleted = False
    def delete_owned_lock_after_intent() -> None:
        nonlocal deleted
        if not deleted and load_state(state_path).phase == "bookmark_publish_pending":
            deleted = True
            git(one, "push", "origin", f":{LOCK_REF}")

    with pytest.raises(SyncError) as caught:
        create_commit_bookmark_locked(
            vault, base, manifest["root_hash"], state_path=state_path,
            expected_remote_head=base, display_name="lost lock", note=None, created_by="a",
            after_lock=delete_owned_lock_after_intent,
        )
    assert caught.value.exit_code == 6
    pending = load_state(state_path)
    assert pending.phase == "bookmark_publish_pending"
    assert not vault._managed_bookmark_rows(remote=True)
    assert recover_session(vault, pending, "a", state_path=state_path) == "bookmark_not_published"
    assert load_state(state_path) == original


def test_missing_lock_unpublished_recovery_refuses_replaced_local_bookmark_ref(tmp_path, monkeypatch) -> None:
    one, vault, manifest, base, _original, state_path = _ordinary_context(tmp_path)
    real_run = vault.runner.run
    suppressed = False

    def do_not_publish(*args, **kwargs):
        nonlocal suppressed
        if args and args[0] == "push" and "--atomic" in args and not suppressed:
            suppressed = True
            return GitResult(args, "", "injected", 1)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(vault.runner, "run", do_not_publish)
    with pytest.raises(SyncError):
        create_commit_bookmark_locked(
            vault, base, manifest["root_hash"], state_path=state_path,
            expected_remote_head=base, display_name="replace local", note=None, created_by="a",
        )
    pending = load_state(state_path)
    assert pending.phase == "bookmark_publish_pending" and pending.bookmark_ref
    monkeypatch.setattr(vault.runner, "run", real_run)
    git(one, "push", "origin", f":{LOCK_REF}")
    git(one, "tag", "-d", pending.bookmark_ref)
    git(one, "tag", "-a", pending.bookmark_ref, base, "-m", "foreign annotation")
    foreign_oid = git(one, "rev-parse", f"refs/tags/{pending.bookmark_ref}").strip()
    assert foreign_oid != pending.bookmark_tag_oid

    with pytest.raises(SyncError) as caught:
        recover_session(vault, pending, "a", state_path=state_path)

    assert (caught.value.code, caught.value.exit_code) == ("bookmark_local_intent_mismatch", 6)
    assert load_state(state_path) == pending
    assert git(one, "rev-parse", f"refs/tags/{pending.bookmark_ref}").strip() == foreign_oid
    assert vault.remote_oid() == base


def test_missing_lock_unpublished_cleanup_delete_cas_refuses_post_validation_swap(tmp_path, monkeypatch) -> None:
    one, vault, manifest, base, _original, state_path = _ordinary_context(tmp_path)
    real_run = vault.runner.run
    suppressed = False

    def do_not_publish(*args, **kwargs):
        nonlocal suppressed
        if args and args[0] == "push" and "--atomic" in args and not suppressed:
            suppressed = True
            return GitResult(args, "", "injected", 1)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(vault.runner, "run", do_not_publish)
    with pytest.raises(SyncError):
        create_commit_bookmark_locked(
            vault, base, manifest["root_hash"], state_path=state_path,
            expected_remote_head=base, display_name="TOCTOU", note=None, created_by="a",
        )
    pending = load_state(state_path)
    assert pending.phase == "bookmark_publish_pending" and pending.bookmark_ref
    monkeypatch.setattr(vault.runner, "run", real_run)
    git(one, "push", "origin", f":{LOCK_REF}")
    expected_ref = f"refs/tags/{pending.bookmark_ref}"
    swapped = False
    foreign_oid = ""

    def swap_at_delete(*args, **kwargs):
        nonlocal swapped, foreign_oid
        if args[:3] == ("update-ref", "-d", expected_ref) and not swapped:
            swapped = True
            git(one, "tag", "-d", pending.bookmark_ref)
            git(one, "tag", "-a", pending.bookmark_ref, base, "-m", "post-validation foreign")
            foreign_oid = git(one, "rev-parse", expected_ref).strip()
        return real_run(*args, **kwargs)

    monkeypatch.setattr(vault.runner, "run", swap_at_delete)
    with pytest.raises(SyncError) as caught:
        recover_session(vault, pending, "a", state_path=state_path)

    assert swapped and foreign_oid and foreign_oid != pending.bookmark_tag_oid
    assert (caught.value.code, caught.value.exit_code) == ("bookmark_local_intent_mismatch", 6)
    assert load_state(state_path) == pending
    assert git(one, "rev-parse", expected_ref).strip() == foreign_oid
    assert vault.remote_oid() == base


def test_commit_bookmark_intent_write_failure_cleans_tag_and_leaves_releasable_lock(tmp_path, monkeypatch) -> None:
    _one, vault, manifest, base, original, state_path = _ordinary_context(tmp_path)
    real_cas = bookmarks_module.save_state_if_unchanged
    def fail_intent(path, expected, state):
        if state.phase == "bookmark_publish_pending":
            raise OSError("injected intent write failure")
        return real_cas(path, expected, state)
    monkeypatch.setattr(bookmarks_module, "save_state_if_unchanged", fail_intent)

    with pytest.raises(SyncError) as caught:
        create_commit_bookmark_locked(
            vault, base, manifest["root_hash"], state_path=state_path,
            expected_remote_head=base, display_name="intent failure", note=None, created_by="a",
        )
    assert caught.value.exit_code == 6
    lock_state = load_state(state_path)
    assert lock_state.phase == "lock_held"
    assert not vault._managed_bookmark_rows(remote=True)
    assert not vault.runner.run("for-each-ref", "--format=%(refname)", "refs/tags/grim-dawn-save-").stdout.strip()

    monkeypatch.setattr(bookmarks_module, "save_state_if_unchanged", real_cas)
    assert recover_session(vault, lock_state, "a", state_path=state_path) == "abandoned_lock_released"
    assert load_state(state_path) == original


def test_hard_crash_before_bookmark_intent_leaves_no_managed_ref(tmp_path) -> None:
    _one, vault, _manifest, base, _original, _state_path = _ordinary_context(tmp_path)

    class HardCrash(BaseException):
        pass

    def crash_before_intent(_name: str, _tag_oid: str, _commit: str) -> None:
        raise HardCrash()

    with pytest.raises(HardCrash):
        vault.create_managed_bookmark(
            base,
            make_bookmark_annotation("hard crash", None, created_by="a"),
            publication_intent=crash_before_intent,
        )

    assert not vault.runner.run(
        "for-each-ref", "--format=%(refname)", "refs/tags/grim-dawn-save-",
    ).stdout.strip()
    assert not vault._managed_bookmark_rows(remote=True)


def test_mktag_failure_leaves_no_intent_or_managed_ref(tmp_path, monkeypatch) -> None:
    _one, vault, _manifest, base, _original, _state_path = _ordinary_context(tmp_path)
    intents: list[tuple[str, str, str]] = []

    def fail_mktag(*args, **kwargs):
        return GitResult(args, "", "injected mktag failure", 1)

    monkeypatch.setattr(vault.runner, "run_bytes_input_result", fail_mktag)
    with pytest.raises(SyncError) as caught:
        vault.create_managed_bookmark(
            base,
            make_bookmark_annotation("mktag failure", None, created_by="a"),
            publication_intent=lambda *row: intents.append(row),
        )

    assert (caught.value.code, caught.value.exit_code) == ("bookmark_create_failed", 6)
    assert not intents
    assert not vault.runner.run(
        "for-each-ref", "--format=%(refname)", "refs/tags/grim-dawn-save-",
    ).stdout.strip()


@pytest.mark.parametrize("collision", [False, True])
def test_recover_persisted_intent_recreates_absent_ref_or_rejects_collision(tmp_path, monkeypatch, collision: bool) -> None:
    one, vault, manifest, base, original, state_path = _ordinary_context(tmp_path)
    real_run = vault.runner.run
    failed = False

    def fail_first_managed_update(*args, **kwargs):
        nonlocal failed
        if (
            args and args[0] == "update-ref" and len(args) > 1
            and str(args[1]).startswith("refs/tags/grim-dawn-save-") and not failed
        ):
            failed = True
            if collision:
                name = str(args[1]).removeprefix("refs/tags/")
                git(one, "tag", "-a", name, base, "-m", "foreign collision")
            return GitResult(args, "", "injected update-ref failure", 1)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(vault.runner, "run", fail_first_managed_update)
    with pytest.raises(SyncError) as caught:
        create_commit_bookmark_locked(
            vault, base, manifest["root_hash"], state_path=state_path,
            expected_remote_head=base, display_name="ref recovery", note=None, created_by="a",
        )
    assert (caught.value.code, caught.value.exit_code) == ("bookmark_create_failed", 6)
    pending = load_state(state_path)
    assert pending.phase == "bookmark_publish_pending" and pending.bookmark_ref and pending.bookmark_tag_oid
    monkeypatch.setattr(vault.runner, "run", real_run)

    if collision:
        foreign_oid = git(one, "rev-parse", f"refs/tags/{pending.bookmark_ref}").strip()
        assert foreign_oid != pending.bookmark_tag_oid
        with pytest.raises(SyncError) as recovery:
            recover_session(vault, pending, "a", state_path=state_path)
        assert (recovery.value.code, recovery.value.exit_code) == ("invalid_bookmark_intent", 6)
        assert load_state(state_path) == pending
        assert git(one, "rev-parse", f"refs/tags/{pending.bookmark_ref}").strip() == foreign_oid
        assert not vault._managed_bookmark_rows(remote=True)
    else:
        assert not vault.runner.run(
            "rev-parse", "--verify", "--quiet", f"refs/tags/{pending.bookmark_ref}", check=False,
        ).stdout.strip()
        assert recover_session(vault, pending, "a", state_path=state_path) == "bookmark_released"
        assert load_state(state_path) == original
        assert git(one, "rev-parse", f"refs/tags/{pending.bookmark_ref}").strip() == pending.bookmark_tag_oid
        assert (pending.bookmark_ref, pending.bookmark_tag_oid, base) in vault._managed_bookmark_rows(remote=True)


def test_missing_remote_lock_and_absent_local_ref_abandons_exact_persisted_intent(tmp_path, monkeypatch) -> None:
    one, vault, manifest, base, original, state_path = _ordinary_context(tmp_path)
    real_run = vault.runner.run
    failed = False

    def fail_local_ref_creation(*args, **kwargs):
        nonlocal failed
        if (args and args[0] == "update-ref" and len(args) > 1
                and str(args[1]).startswith("refs/tags/grim-dawn-save-") and not failed):
            failed = True
            return GitResult(args, "", "injected pre-ref crash", 1)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(vault.runner, "run", fail_local_ref_creation)
    with pytest.raises(SyncError):
        create_commit_bookmark_locked(
            vault, base, manifest["root_hash"], state_path=state_path,
            expected_remote_head=base, display_name="missing ref", note=None, created_by="a",
        )
    pending = load_state(state_path)
    assert pending.phase == "bookmark_publish_pending" and pending.bookmark_ref
    assert not real_run(
        "rev-parse", "--verify", "--quiet", f"refs/tags/{pending.bookmark_ref}", check=False,
    ).stdout.strip()
    monkeypatch.setattr(vault.runner, "run", real_run)
    git(one, "push", "origin", f":{LOCK_REF}")

    assert recover_session(vault, pending, "a", state_path=state_path) == "bookmark_not_published"
    assert load_state(state_path) == original
    assert vault.remote_oid() == base
    assert not git(one, "ls-remote", "--refs", "origin", LOCK_REF)
    assert not real_run(
        "rev-parse", "--verify", "--quiet", f"refs/tags/{pending.local_tag}", check=False,
    ).stdout.strip()


def test_automatic_displaced_backup_failure_recovers_same_workflow_session_before_main_change(tmp_path, monkeypatch) -> None:
    _one, vault, manifest, base, original, state_path = _ordinary_context(tmp_path)
    lock = acquire_lock(vault, "a", base, state_path=state_path, expected_pre_state=original)
    real_run = vault.runner.run
    suppressed = False
    def do_not_apply_once(*args, **kwargs):
        nonlocal suppressed
        if args and args[0] == "push" and "--atomic" in args and not suppressed:
            suppressed = True
            return GitResult(args, "", "injected automatic backup failure", 1)
        return real_run(*args, **kwargs)
    monkeypatch.setattr(vault.runner, "run", do_not_apply_once)

    with pytest.raises(SyncError) as caught:
        create_displaced_head_bookmark_locked(
            vault, lock, original, base, state_path=state_path, created_by="a",
        )
    assert caught.value.exit_code == 6
    pending = load_state(state_path)
    assert pending.phase == "bookmark_publish_pending" and pending.session_id == lock.session.session_id
    assert vault.remote_oid() == base

    monkeypatch.setattr(vault.runner, "run", real_run)
    assert recover_session(vault, pending, "a", state_path=state_path) == "bookmark_released"
    assert load_state(state_path) == original


def test_automatic_displaced_backup_success_returns_to_same_lock_held_state(tmp_path) -> None:
    _one, vault, _manifest, base, original, state_path = _ordinary_context(tmp_path)
    lock = acquire_lock(vault, "a", base, state_path=state_path, expected_pre_state=original)
    expected_lock_state = load_state(state_path)

    made = create_displaced_head_bookmark_locked(
        vault, lock, original, base, state_path=state_path, created_by="a",
    )

    assert load_state(state_path) == expected_lock_state
    assert git(vault.repo, "ls-remote", "--refs", "origin", LOCK_REF).startswith(lock.oid)
    assert vault.remote_oid() == base
    assert made.commit == base
    assert (made.ref, git(vault.repo, "rev-parse", f"refs/tags/{made.ref}").strip(), base) in vault._managed_bookmark_rows(remote=True)


def test_commit_bookmark_prelock_state_cas_race_publishes_no_tag_or_lock(tmp_path) -> None:
    _one, vault, manifest, base, original, state_path = _ordinary_context(tmp_path)
    changed = SyncState(
        last_applied_remote_commit=base,
        last_applied_manifest_root_hash="f" * 64,
        machine_id="a",
    )
    def replace_state_before_lock() -> None:
        save_state(state_path, changed)

    with pytest.raises(SyncError) as caught:
        create_commit_bookmark_locked(
            vault, base, manifest["root_hash"], state_path=state_path,
            expected_remote_head=base, display_name="CAS race", note=None, created_by="a",
            before_lock=replace_state_before_lock,
        )

    assert caught.value.code == "selection_stale"
    assert load_state(state_path) == changed and load_state(state_path) != original
    assert not git(vault.repo, "ls-remote", "--refs", "origin", LOCK_REF)
    assert not vault._managed_bookmark_rows(remote=True)


def test_remote_only_legacy_tag_is_fetched_read_only_for_catalog(tmp_path) -> None:
    bare, one, two = clone_pair(tmp_path)
    source = save(tmp_path / "source", b"old")
    vault = GitVault(one)
    commit = vault.snapshot(source, machine_id="a", session_id="old", validator=valid)
    vault.push(commit)
    git(one, "tag", "-a", "archive/remote-only", commit, "-m", "legacy")
    git(one, "push", "origin", "refs/tags/archive/remote-only:refs/tags/archive/remote-only")
    other = GitVault(two)
    catalog = VersionCatalogBuilder(other, source, machine_id="a").build(history_limit=1)
    assert ("legacy", commit) not in [(item.kind, item.commit) for item in catalog.candidates]  # same root deduplicates
    assert other.runner.run("rev-parse", "--verify", "refs/tags/archive/remote-only").returncode == 0
    commands = [command[1] for command in other.runner.commands]
    assert not {"checkout", "restore", "commit", "push", "tag"}.intersection(commands)


def test_bookmark_rejects_remote_tag_object_with_same_peeled_commit(tmp_path, monkeypatch) -> None:
    _, one, _ = clone_pair(tmp_path)
    source = save(tmp_path / "source", b"one"); vault = GitVault(one)
    commit = vault.snapshot(source, machine_id="a", session_id="first", validator=valid); vault.push(commit)
    real_rows = vault._managed_bookmark_rows
    def substituted_rows(*, remote: bool):
        rows = real_rows(remote=remote)
        if remote:
            return tuple((name, "f" * 40, target) for name, _tag, target in rows)
        return rows
    monkeypatch.setattr(vault, "_managed_bookmark_rows", substituted_rows)
    with pytest.raises(SyncError) as caught:
        _create_bookmark_test_only(vault, commit, display_name="material storage", note="keep", created_by="a")
    assert caught.value.code == "bookmark_push_incomplete"


def test_live_ahead_bookmark_alias_and_restore_leave_main_state_and_live_unchanged(tmp_path) -> None:
    _, one, two = clone_pair(tmp_path)
    live = save(tmp_path / "live", b"baseline")
    vault = GitVault(one)
    base_manifest = stable_manifest(live, machine_id="a", retries=1)
    base = vault.snapshot(live, machine_id="a", session_id="baseline", validator=valid)
    vault.push(base)
    checkout_remote_main(two)

    state_path = tmp_path / "state.local.json"
    original = SyncState(
        last_applied_remote_commit=base,
        last_applied_manifest_root_hash=base_manifest["root_hash"],
        machine_id="a",
    )
    save_state(state_path, original)
    (live / "main" / "a" / "player.gdc").write_bytes(b"live-ahead")
    live_manifest = stable_manifest(live, machine_id="a", retries=1)
    before_head = git(one, "rev-parse", "HEAD").strip()
    before_status = git(one, "status", "--porcelain=v1", "--untracked-files=all")
    before_live = (live / "main" / "a" / "player.gdc").read_bytes()

    made = create_live_bookmark_locked(
        vault,
        live,
        live_manifest,
        state_path=state_path,
        expected_remote_head=base,
        display_name="before experiment",
        note="detached live snapshot",
        created_by="a",
        validator=valid,
    )

    assert made.commit != base
    assert git(one, "ls-remote", "--refs", "origin", "refs/heads/main").split()[0] == base
    assert git(one, "rev-parse", "HEAD").strip() == before_head
    assert git(one, "status", "--porcelain=v1", "--untracked-files=all") == before_status
    assert (live / "main" / "a" / "player.gdc").read_bytes() == before_live
    assert load_state(state_path) == original

    other = GitVault(two)
    catalog = VersionCatalogBuilder(other, live, machine_id="a").build()
    live_candidate = next(item for item in catalog.candidates if item.kind == "live")
    alias = next(item for item in live_candidate.aliases if item.kind == "bookmark")
    assert alias.commit == made.commit and alias.display_name == "before experiment"

    restored = tmp_path / "restored"
    other.extract_save(made.commit, restored, machine_id="a", validator=valid)
    assert (restored / "main" / "a" / "player.gdc").read_bytes() == b"live-ahead"


@pytest.mark.parametrize("remote_lock_present", [True, False])
def test_bookmark_release_pending_recovery_restores_prior_baseline(tmp_path, remote_lock_present: bool) -> None:
    _, one, _ = clone_pair(tmp_path)
    live = save(tmp_path / "live", b"baseline")
    vault = GitVault(one)
    manifest = stable_manifest(live, machine_id="a", retries=1)
    base = vault.snapshot(live, machine_id="a", session_id="baseline", validator=valid)
    vault.push(base)
    original = SyncState(
        last_applied_remote_commit=base,
        last_applied_manifest_root_hash=manifest["root_hash"],
        machine_id="a",
    )
    state_path = tmp_path / "state.local.json"
    save_state(state_path, original)
    lock = acquire_lock(
        vault, "a", base, state_path=state_path, expected_pre_state=original,
    )
    pending = SyncState(
        last_applied_remote_commit=base,
        last_applied_manifest_root_hash=manifest["root_hash"],
        session_id=lock.session.session_id,
        machine_id="a",
        base_commit=base,
        lock_oid=lock.oid,
        local_tag=lock.local_tag,
        phase="bookmark_release_pending",
        pushed_commit=base,
    )
    save_state(state_path, pending)
    if not remote_lock_present:
        git(one, "push", "origin", f":{LOCK_REF}")

    outcome = recover_session(vault, load_state(state_path), "a", state_path=state_path)
    assert outcome == ("bookmark_released" if remote_lock_present else "bookmark_complete")
    assert load_state(state_path) == original
    assert not git(one, "ls-remote", "--refs", "origin", LOCK_REF)


@pytest.mark.parametrize("race", ["lock_deleted", "lock_replaced", "state_tampered"])
def test_live_bookmark_rechecks_exact_lock_and_state_before_local_tag_creation(tmp_path, race: str) -> None:
    _, one, _ = clone_pair(tmp_path)
    live = save(tmp_path / "live", b"baseline")
    vault = GitVault(one)
    base_manifest = stable_manifest(live, machine_id="a", retries=1)
    base = vault.snapshot(live, machine_id="a", session_id="baseline", validator=valid)
    vault.push(base)
    original = SyncState(
        last_applied_remote_commit=base,
        last_applied_manifest_root_hash=base_manifest["root_hash"],
        machine_id="a",
    )
    state_path = tmp_path / "state.local.json"
    save_state(state_path, original)
    (live / "main" / "a" / "player.gdc").write_bytes(b"live-ahead")
    manifest = stable_manifest(live, machine_id="a", retries=1)
    calls = 0

    def inject_race() -> None:
        nonlocal calls
        calls += 1
        if calls != 3:
            return
        if race == "state_tampered":
            save_state(state_path, original)
            return
        git(one, "push", "origin", f":{LOCK_REF}")
        if race == "lock_replaced":
            git(one, "tag", "-a", "intruder-lock", base, "-m", "intruder")
            git(one, "push", "origin", f"refs/tags/intruder-lock:{LOCK_REF}")

    with pytest.raises(SyncError) as caught:
        create_live_bookmark_locked(
            vault, live, manifest, state_path=state_path, expected_remote_head=base,
            display_name="must not publish", note=None, created_by="a", validator=valid,
            after_lock=inject_race,
        )
    assert caught.value.exit_code == 6
    assert not git(one, "ls-remote", "--refs", "origin", "refs/tags/grim-dawn-save-*")


@pytest.mark.parametrize("fault_call", [5, 6])
def test_recover_finishes_exact_persisted_bookmark_publication_intent(tmp_path, fault_call: int) -> None:
    _, one, _ = clone_pair(tmp_path)
    live = save(tmp_path / "live", b"baseline")
    vault = GitVault(one)
    base_manifest = stable_manifest(live, machine_id="a", retries=1)
    base = vault.snapshot(live, machine_id="a", session_id="baseline", validator=valid)
    vault.push(base)
    original = SyncState(last_applied_remote_commit=base,
                         last_applied_manifest_root_hash=base_manifest["root_hash"], machine_id="a")
    state_path = tmp_path / "state.local.json"; save_state(state_path, original)
    (live / "main" / "a" / "player.gdc").write_bytes(b"intent")
    manifest = stable_manifest(live, machine_id="a", retries=1)
    calls = 0

    def interrupt_once() -> None:
        nonlocal calls
        calls += 1
        if calls == fault_call:
            raise SyncError("injected_bookmark_fault", "fault", 6)

    with pytest.raises(SyncError):
        create_live_bookmark_locked(
            vault, live, manifest, state_path=state_path, expected_remote_head=base,
            display_name="recover me", note=None, created_by="a", validator=valid,
            after_lock=interrupt_once,
        )
    pending = load_state(state_path)
    assert pending.phase == "bookmark_publish_pending"
    assert pending.bookmark_ref and pending.bookmark_tag_oid and pending.bookmark_root_hash == manifest["root_hash"]

    assert recover_session(vault, pending, "a", state_path=state_path) == "bookmark_released"
    assert load_state(state_path) == original
    rows = vault._managed_bookmark_rows(remote=True)
    assert (pending.bookmark_ref, pending.bookmark_tag_oid, pending.local_commit) in rows


def test_publication_intent_save_failure_removes_exact_unpublished_local_tag(tmp_path, monkeypatch) -> None:
    _, one, _ = clone_pair(tmp_path)
    live = save(tmp_path / "live", b"baseline")
    vault = GitVault(one)
    base_manifest = stable_manifest(live, machine_id="a", retries=1)
    base = vault.snapshot(live, machine_id="a", session_id="baseline", validator=valid)
    vault.push(base)
    original = SyncState(last_applied_remote_commit=base,
                         last_applied_manifest_root_hash=base_manifest["root_hash"], machine_id="a")
    state_path = tmp_path / "state.local.json"; save_state(state_path, original)
    (live / "main" / "a" / "player.gdc").write_bytes(b"intent-save-failure")
    manifest = stable_manifest(live, machine_id="a", retries=1)
    before_head = git(one, "rev-parse", "HEAD").strip()
    before_status = git(one, "status", "--porcelain=v1", "--untracked-files=all")
    before_live = (live / "main" / "a" / "player.gdc").read_bytes()
    lock_state: list[SyncState] = []
    real_save_state = bookmarks_module.save_state_if_unchanged

    def fail_intent(path, expected, state):
        if state.phase == "bookmark_publish_pending":
            lock_state.append(load_state(path))
            raise OSError("injected intent persistence failure")
        return real_save_state(path, expected, state)

    monkeypatch.setattr(bookmarks_module, "save_state_if_unchanged", fail_intent)
    with pytest.raises(SyncError) as caught:
        create_live_bookmark_locked(
            vault, live, manifest, state_path=state_path, expected_remote_head=base,
            display_name="must clean local ref", note=None, created_by="a", validator=valid,
        )
    assert caught.value.exit_code == 6
    assert len(lock_state) == 1 and lock_state[0].phase == "lock_held"
    assert load_state(state_path) == lock_state[0]
    assert git(one, "rev-parse", "HEAD").strip() == before_head
    assert git(one, "status", "--porcelain=v1", "--untracked-files=all") == before_status
    assert (live / "main" / "a" / "player.gdc").read_bytes() == before_live
    assert git(one, "ls-remote", "--refs", "origin", "refs/heads/main").split()[0] == base
    assert not git(one, "ls-remote", "--refs", "origin", "refs/tags/grim-dawn-save-*")
    assert not git(one, "for-each-ref", "--format=%(refname)", "refs/tags/grim-dawn-save-")
