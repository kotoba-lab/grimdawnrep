from __future__ import annotations

import pytest
import grim_dawn_sync.bookmarks as bookmarks_module

from grim_dawn_sync.bookmarks import (
    create_bookmark,
    create_live_bookmark_locked,
    make_bookmark_annotation,
    parse_bookmark_annotation,
)
from grim_dawn_sync.errors import SyncError
from grim_dawn_sync.git_vault import GitVault
from grim_dawn_sync.manifest import stable_manifest
from grim_dawn_sync.session_lock import LOCK_REF, acquire_lock, recover_session
from grim_dawn_sync.state import SyncState, load_state, save_state
from grim_dawn_sync.version_catalog import VersionCatalogBuilder
from test_save_sync_git_vault import checkout_remote_main, clone_pair, git, save, valid


def test_annotation_rejects_duplicate_control_and_bounds() -> None:
    with pytest.raises(SyncError):
        parse_bookmark_annotation('{"schema_version":"1.0.0","schema_version":"1.0.0"}')
    with pytest.raises(SyncError):
        make_bookmark_annotation("bad\nname", None, created_by="machine")
    with pytest.raises(SyncError):
        make_bookmark_annotation("x" * 81, None, created_by="machine")
    with pytest.raises(SyncError):
        make_bookmark_annotation("ok", "x" * 501, created_by="machine")


def test_create_bookmark_is_annotated_remote_verified_and_catalogued(tmp_path) -> None:
    _, one, two = clone_pair(tmp_path)
    source = save(tmp_path / "source", b"one")
    vault = GitVault(one)
    commit = vault.snapshot(source, machine_id="a", session_id="first", validator=valid)
    vault.push(commit)
    made = create_bookmark(vault, commit, display_name="material storage", note="keep", created_by="a")
    assert made.commit == commit and made.ref.startswith("grim-dawn-save-")
    other = GitVault(two)
    rows = other.managed_bookmarks()
    assert len(rows) == 1 and rows[0][0:2] == (made.ref, commit)
    assert parse_bookmark_annotation(rows[0][2])["display_name"] == "material storage"
    catalog = VersionCatalogBuilder(other, source, machine_id="a").build()
    # The duplicate root is intentionally coalesced with the live candidate.
    assert catalog.candidates[0].kind == "live"


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
        create_bookmark(vault, commit, display_name="material storage", note="keep", created_by="a")
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
    lock = acquire_lock(vault, "a", base, state_path=state_path)
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
    real_save_state = bookmarks_module.save_state

    def fail_intent(path, state):
        if state.phase == "bookmark_publish_pending":
            lock_state.append(load_state(path))
            raise OSError("injected intent persistence failure")
        return real_save_state(path, state)

    monkeypatch.setattr(bookmarks_module, "save_state", fail_intent)
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
