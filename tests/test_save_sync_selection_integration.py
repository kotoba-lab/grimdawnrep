"""Isolated real-Git acceptance tests for selection workflows.

Every remote is a temporary bare repository and every save is synthetic.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grim_dawn_sync.config import parse_config
from grim_dawn_sync.bookmarks import _create_bookmark_test_only
from grim_dawn_sync.errors import EXIT_RECOVERY_REQUIRED, SyncError
from grim_dawn_sync.git_vault import GitVault
from grim_dawn_sync.manifest import stable_manifest
from grim_dawn_sync.selection import ReconcileCase, SelectionRegistry, classify_reconciliation
from grim_dawn_sync.session_lock import acquire_lock, inspect_remote_lock_readonly, recover_session
from grim_dawn_sync.snapshot import _copy_verified, apply_restore, archive_before_restore, plan_restore
from grim_dawn_sync.state import SyncState, load_state, save_state
from grim_dawn_sync.version_catalog import VersionCatalogBuilder
from grim_dawn_sync.workflow import DomainAdapters, LaunchWorkflow
from test_save_sync_git_vault import checkout_remote_main, clone_pair, git, save, valid


class IntegrationAdapters(DomainAdapters):
    """Use real Git/lock/restore/archive I/O, replacing only game parsing/process launch."""
    def preflight(self) -> None: self.vault.preflight()
    def prepare_remote_restore(self, base: str, session: str):
        source = self.local_root / "staging" / session
        self.vault.extract_save(base, source, machine_id=self.config.machine_id, validator=valid)
        return plan_restore(source, self.config.save_root, self.local_root / "archives", self.local_root / "recovery",
                            machine_id=self.config.machine_id, session_id=session, validator=valid)
    def archive_before_restore(self, plan):
        return archive_before_restore(plan, machine_id=self.config.machine_id, validator=valid)
    def apply_remote_save(self, plan):
        return apply_restore(plan, self.local_root / "recovery", machine_id=self.config.machine_id, validator=valid)
    def validate(self, manifest: dict, baseline: dict | None = None) -> dict: return manifest
    def archive_after_game(self, manifest: dict) -> str:
        dest = self._new_destination("archives", manifest)
        _copy_verified(self.config.save_root, dest, manifest, self.config.machine_id, 1, 0, valid)
        self.last_archive_destination, self.last_archive_root = dest.name, manifest["root_hash"]
        return manifest["root_hash"]
    def rescue_raw(self, manifest: dict | None) -> str: return (manifest or self.live_manifest())["root_hash"]
    def snapshot(self, session: str, manifest: dict, hook):
        return self.vault.snapshot(self.config.save_root, machine_id=self.config.machine_id, session_id=session,
                                   expected_manifest=manifest, validator=valid, state_hook=hook)
    def launch(self, hook):
        for state in ("START_DPYES", "WAIT_GAME_START", "WAIT_GAME_EXIT"): hook(state)


def _environment(tmp_path: Path, baseline: bytes = b"base"):
    _, publisher_repo, terminal_repo = clone_pair(tmp_path)
    publisher = GitVault(publisher_repo); seed = save(tmp_path / "seed", baseline)
    base = publisher.snapshot(seed, machine_id="publisher", session_id="base", validator=valid); publisher.push(base)
    checkout_remote_main(terminal_repo)
    live = save(tmp_path / "live", baseline); local = tmp_path / "local"; local.mkdir()
    config = parse_config({
        "schema_version": "1.0.0", "machine_id": "terminal", "save_root": str(live),
        "vault_repo": str(terminal_repo), "remote": "origin", "branch": "main",
        "game_install": str(tmp_path / "game"), "launcher_mode": "dpyes",
        "launcher_path": str(tmp_path / "game/DPYes.exe"), "game_process_names": ["Grim Dawn.exe"],
        "launch_timeout_seconds": 1, "stable_window_seconds": 1, "stable_scan_retries": 1,
        "offline_policy": "deny",
    })
    baseline_manifest = stable_manifest(live, machine_id="terminal", retries=1)
    save_state(local / "state.json", SyncState(last_applied_remote_commit=base,
                                                last_applied_manifest_root_hash=baseline_manifest["root_hash"],
                                                machine_id="terminal"))
    adapters = IntegrationAdapters(config, local)
    return publisher, adapters, config, local, base


def _plan(adapters: IntegrationAdapters, config, state_path: Path, *, choose: str, mode: str):
    state = load_state(state_path)
    catalog = VersionCatalogBuilder(adapters.vault, config.save_root, machine_id=config.machine_id,
                                    baseline_root_hash=state.last_applied_manifest_root_hash).build()
    remote = adapters.vault.validate_commit_snapshot(catalog.remote_head)["root_hash"]
    case = classify_reconciliation(live_root_hash=catalog.live_root_hash, remote_root_hash=remote,
                                   baseline_root_hash=state.last_applied_manifest_root_hash)
    candidates = [catalog.candidate(alias.candidate_id) for item in catalog.candidates for alias in item.aliases]
    candidates += list(catalog.candidates)
    selected = next(item for item in candidates if item.kind == choose)
    registry = SelectionRegistry(verified_bookmark_target=lambda _item: True); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=selected.candidate_id, mode=mode, case=case)
    if plan.confirmation_required: plan = registry.confirm(plan)
    return registry, plan


def _assert_clean_success(adapters: IntegrationAdapters, local: Path) -> None:
    state = load_state(local / "state.json")
    assert state.phase is None and state.last_applied_remote_commit == adapters.vault.remote_oid()
    assert inspect_remote_lock_readonly(adapters.vault) is None
    adapters.vault.fetch()
    assert adapters.vault.reconcile().relation == "equal"


def _recover_real(adapters: IntegrationAdapters, local: Path) -> str:
    state_path = local / "state.json"; pending = load_state(state_path)
    assert pending.phase is not None and inspect_remote_lock_readonly(adapters.vault) is not None
    result = recover_session(adapters.vault, pending, adapters.config.machine_id, state_path=state_path)
    _assert_clean_success(adapters, local)
    return result


def test_remote_only_apply_reaches_equal_ready_state(tmp_path: Path) -> None:
    publisher, adapters, config, local, _ = _environment(tmp_path)
    changed = save(tmp_path / "remote-new", b"remote")
    oid = publisher.snapshot(changed, machine_id="publisher", session_id="remote", validator=valid); publisher.push(oid)
    registry, plan = _plan(adapters, config, local / "state.json", choose="remote_head", mode="launch")
    assert LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)["state"] == "COMPLETE"
    assert (config.save_root / "main/a/player.gdc").read_bytes() == b"remote"
    _assert_clean_success(adapters, local)


def test_live_only_bookmarks_displaced_remote_then_promotes(tmp_path: Path) -> None:
    _, adapters, config, local, base = _environment(tmp_path)
    save(config.save_root, b"live")
    registry, plan = _plan(adapters, config, local / "state.json", choose="live", mode="promote-only")
    assert LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)["state"] == "COMPLETE"
    assert any(target == base for _, target, _ in adapters.vault.managed_bookmarks())
    _assert_clean_success(adapters, local)


@pytest.mark.parametrize("winner", ["remote_head", "live"])
def test_diverged_selection_preserves_loser_and_publishes_winner(tmp_path: Path, winner: str) -> None:
    publisher, adapters, config, local, old = _environment(tmp_path)
    save(config.save_root, b"live")
    changed = save(tmp_path / "remote-new", b"remote")
    remote = publisher.snapshot(changed, machine_id="publisher", session_id="remote", validator=valid); publisher.push(remote)
    registry, plan = _plan(adapters, config, local / "state.json", choose=winner, mode="promote-only")
    assert LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)["state"] == "COMPLETE"
    expected = b"remote" if winner == "remote_head" else b"live"
    assert (config.save_root / "main/a/player.gdc").read_bytes() == expected
    if winner == "live":
        assert any(target == remote for _, target, _ in adapters.vault.managed_bookmarks())
    else:
        assert any(path.read_bytes() == b"live" for path in (local / "archives").rglob("player.gdc"))
    _assert_clean_success(adapters, local)


def test_stale_live_remote_and_lock_stop_before_workflow_mutation(tmp_path: Path) -> None:
    publisher, adapters, config, local, base = _environment(tmp_path)
    save(config.save_root, b"live")
    registry, plan = _plan(adapters, config, local / "state.json", choose="live", mode="promote-only")
    original_state = (local / "state.json").read_bytes()
    save(config.save_root, b"changed-again")
    with pytest.raises(SyncError) as caught:
        LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)
    assert caught.value.code == "selection_stale" and (local / "state.json").read_bytes() == original_state

    # A real competing remote lock is detected by the atomic acquire path.
    save(config.save_root, b"live")
    registry, plan = _plan(adapters, config, local / "state.json", choose="live", mode="promote-only")
    competitor = GitVault(publisher.repo)
    acquire_lock(competitor, "competitor", competitor.remote_oid(), state_path=tmp_path / "competitor-state.json")
    with pytest.raises(SyncError) as caught:
        LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)
    assert caught.value.code in {"lock_held", "lock_race_lost"}


def test_history_and_managed_bookmark_targets_pass_isolated_restore_inspection(tmp_path: Path) -> None:
    publisher, adapters, config, local, base = _environment(tmp_path)
    _create_bookmark_test_only(publisher, base, display_name="Known old save", note="restore drill", created_by="publisher")
    changed = save(tmp_path / "remote-new", b"remote")
    remote = publisher.snapshot(changed, machine_id="publisher", session_id="remote", validator=valid); publisher.push(remote)
    catalog = VersionCatalogBuilder(adapters.vault, config.save_root, machine_id=config.machine_id).build()
    candidates = [catalog.candidate(alias.candidate_id) for item in catalog.candidates for alias in item.aliases]
    candidates += list(catalog.candidates)
    history = next(item for item in candidates if item.kind == "history" and item.commit == base)
    bookmark = next(item for item in candidates if item.kind == "bookmark" and item.commit == base)
    for index, item in enumerate((history, bookmark)):
        destination = tmp_path / f"inspection-{index}"
        adapters.vault.extract_save(item.commit, destination, machine_id=config.machine_id, validator=valid)
        assert (destination / "main/a/player.gdc").read_bytes() == b"base"
    assert adapters.vault.remote_oid() == remote
    assert inspect_remote_lock_readonly(adapters.vault) is None
    assert load_state(local / "state.json").phase is None


@pytest.mark.parametrize("selected_kind", ["history", "bookmark"])
def test_history_or_managed_bookmark_promote_preserves_losers_and_reaches_clean_equal(
    tmp_path: Path, selected_kind: str,
) -> None:
    publisher, adapters, config, local, base = _environment(tmp_path)
    _create_bookmark_test_only(publisher, base, display_name="Selected retained base", note=None, created_by="publisher")
    remote_source = save(tmp_path / "remote-new", b"remote-loser")
    displaced_remote = publisher.snapshot(remote_source, machine_id="publisher", session_id="remote", validator=valid)
    publisher.push(displaced_remote)
    save(config.save_root, b"live-loser")

    registry, plan = _plan(adapters, config, local / "state.json", choose=selected_kind, mode="promote-only")
    result = LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)

    assert result["state"] == "COMPLETE"
    assert (config.save_root / "main/a/player.gdc").read_bytes() == b"base"
    assert any(path.read_bytes() == b"live-loser" for path in (local / "archives").rglob("player.gdc"))
    assert any(target == displaced_remote for _, target, _ in adapters.vault.managed_bookmarks())
    published = adapters.vault.remote_oid(); assert published and published != displaced_remote
    assert adapters.vault.validate_commit_snapshot(published)["root_hash"] == adapters.live_manifest()["root_hash"]
    _assert_clean_success(adapters, local)


@pytest.mark.parametrize("tag_name", ["archive/known-base", "milestone/known-base"])
def test_legacy_candidate_catalog_selection_restore_and_promote_reaches_clean_equal(
    tmp_path: Path, tag_name: str,
) -> None:
    publisher, adapters, config, local, base = _environment(tmp_path)
    git(publisher.repo, "tag", "-a", tag_name, base, "-m", "legacy retained base")
    git(publisher.repo, "push", "origin", f"refs/tags/{tag_name}:refs/tags/{tag_name}")
    remote_source = save(tmp_path / "remote-new", b"remote-loser")
    displaced_remote = publisher.snapshot(remote_source, machine_id="publisher", session_id="remote", validator=valid)
    publisher.push(displaced_remote)
    save(config.save_root, b"live-loser")

    state = load_state(local / "state.json")
    catalog = VersionCatalogBuilder(
        adapters.vault, config.save_root, machine_id=config.machine_id,
        baseline_root_hash=state.last_applied_manifest_root_hash,
    ).build()
    remote_root = adapters.vault.validate_commit_snapshot(catalog.remote_head)["root_hash"]
    case = classify_reconciliation(live_root_hash=catalog.live_root_hash, remote_root_hash=remote_root,
                                   baseline_root_hash=state.last_applied_manifest_root_hash)
    candidates = [catalog.candidate(alias.candidate_id) for item in catalog.candidates for alias in item.aliases]
    candidates += list(catalog.candidates)
    selected = next(item for item in candidates if item.kind == "legacy" and item.display_name == tag_name)
    registry = SelectionRegistry(verified_bookmark_target=lambda item: item == selected); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=selected.candidate_id,
                               mode="promote-only", case=case)
    plan = registry.confirm(plan)
    result = LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)

    assert result["state"] == "COMPLETE"
    assert (config.save_root / "main/a/player.gdc").read_bytes() == b"base"
    assert any(path.read_bytes() == b"live-loser" for path in (local / "archives").rglob("player.gdc"))
    assert any(target == displaced_remote for _, target, _ in adapters.vault.managed_bookmarks())
    _assert_clean_success(adapters, local)


def test_remote_change_after_plan_is_rejected_before_local_mutation(tmp_path: Path) -> None:
    publisher, adapters, config, local, _ = _environment(tmp_path)
    save(config.save_root, b"live")
    registry, plan = _plan(adapters, config, local / "state.json", choose="live", mode="promote-only")
    state_before = (local / "state.json").read_bytes()
    changed = save(tmp_path / "remote-new", b"remote")
    publisher.push(publisher.snapshot(changed, machine_id="publisher", session_id="remote", validator=valid))
    with pytest.raises(SyncError) as caught:
        LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)
    assert caught.value.code == "selection_stale"
    assert (local / "state.json").read_bytes() == state_before
    assert inspect_remote_lock_readonly(adapters.vault) is None


def test_real_recover_after_bookmark_push_ambiguity_then_retry(tmp_path: Path) -> None:
    class AmbiguousBookmark(IntegrationAdapters):
        failed = False
        def bookmark_displaced_remote(self, commit, plan):
            super().bookmark_displaced_remote(commit, plan)
            if not self.failed:
                self.failed = True
                raise SyncError("bookmark_push_incomplete", "Bookmark result is ambiguous.", EXIT_RECOVERY_REQUIRED)
    _, _, config, local, base = _environment(tmp_path)
    adapters = AmbiguousBookmark(config, local); save(config.save_root, b"live")
    registry, plan = _plan(adapters, config, local / "state.json", choose="live", mode="promote-only")
    with pytest.raises(SyncError) as caught:
        LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)
    assert caught.value.code == "bookmark_push_incomplete" and caught.value.exit_code == EXIT_RECOVERY_REQUIRED
    assert any(target == base for _, target, _ in adapters.vault.managed_bookmarks())
    assert _recover_real(adapters, local) == "abandoned_lock_released"
    registry, plan = _plan(adapters, config, local / "state.json", choose="live", mode="promote-only")
    assert LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)["state"] == "COMPLETE"
    _assert_clean_success(adapters, local)


def test_real_recover_after_apply_failure_keeps_selected_live_and_clean_state(tmp_path: Path) -> None:
    class FailAfterApply(IntegrationAdapters):
        def apply_remote_save(self, plan):
            result = super().apply_remote_save(plan)
            raise SyncError("restore_apply_failed", "Restore stopped after applying selected data.", EXIT_RECOVERY_REQUIRED)
    publisher, _, config, local, _ = _environment(tmp_path)
    changed = save(tmp_path / "remote-new", b"remote")
    publisher.push(publisher.snapshot(changed, machine_id="publisher", session_id="remote", validator=valid))
    adapters = FailAfterApply(config, local)
    registry, plan = _plan(adapters, config, local / "state.json", choose="remote_head", mode="launch")
    with pytest.raises(SyncError) as caught:
        LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)
    assert caught.value.code == "restore_apply_failed" and caught.value.exit_code == EXIT_RECOVERY_REQUIRED
    assert (config.save_root / "main/a/player.gdc").read_bytes() == b"remote"
    pending = load_state(local / "state.json")
    original = SyncState(
        last_applied_remote_commit=pending.last_applied_remote_commit,
        last_applied_manifest_root_hash=pending.last_applied_manifest_root_hash,
        machine_id=pending.machine_id,
    )
    assert recover_session(
        adapters.vault, pending, adapters.config.machine_id, state_path=local / "state.json",
    ) == "abandoned_lock_released"
    assert load_state(local / "state.json") == original
    assert inspect_remote_lock_readonly(adapters.vault) is None
    adapters.vault.fetch()
    assert adapters.vault.reconcile().relation == "equal"
    assert (config.save_root / "main/a/player.gdc").read_bytes() == b"remote"


def test_real_recover_confirms_ambiguous_snapshot_push_and_clears_lock(tmp_path: Path) -> None:
    class AmbiguousMainPush(IntegrationAdapters):
        def push(self, oid):
            self.vault.push(oid)
            raise SyncError("push_incomplete", "Main push result is ambiguous.", EXIT_RECOVERY_REQUIRED)
    _, _, config, local, _ = _environment(tmp_path)
    adapters = AmbiguousMainPush(config, local); save(config.save_root, b"live")
    registry, plan = _plan(adapters, config, local / "state.json", choose="live", mode="promote-only")
    with pytest.raises(SyncError) as caught:
        LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)
    assert caught.value.code == "push_incomplete" and load_state(local / "state.json").phase == "committed"
    assert _recover_real(adapters, local) == "released"


def test_real_recover_resumes_release_pending_and_reaches_clean_state(tmp_path: Path) -> None:
    class ReleaseDeleteFailure(IntegrationAdapters):
        def release(self, lock, oid, manifest):
            save_state(self.state_path, SyncState(
                last_applied_manifest_root_hash=manifest["root_hash"],
                session_id=lock.session.session_id, machine_id=lock.session.machine_id,
                base_commit=lock.session.base_commit, lock_oid=lock.oid, local_tag=lock.local_tag,
                phase="release_pending", local_commit=oid, pushed_commit=oid,
            ))
            raise SyncError("release_incomplete", "Lock deletion was not confirmed.", EXIT_RECOVERY_REQUIRED)
    _, _, config, local, _ = _environment(tmp_path)
    adapters = ReleaseDeleteFailure(config, local); save(config.save_root, b"live")
    registry, plan = _plan(adapters, config, local / "state.json", choose="live", mode="promote-only")
    with pytest.raises(SyncError) as caught:
        LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)
    assert caught.value.code == "release_incomplete" and load_state(local / "state.json").phase == "release_pending"
    assert _recover_real(adapters, local) == "released"
