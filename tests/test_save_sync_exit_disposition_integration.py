"""Real-Git acceptance tests for T-E: exit disposition (plan section 7).

Covers the plan section 9 (Integration) requirements: for both ``local-only``
and ``restore-startup``, remote main must never change, and each is
recoverable from a post-lock fault.  Every remote is a temporary bare
repository; no real save or remote is ever touched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from grim_dawn_sync.errors import EXIT_RECOVERY_REQUIRED, SyncError
from grim_dawn_sync.selection import ReconcileCase, classify_reconciliation
from grim_dawn_sync.session_lock import inspect_remote_lock_readonly, recover_session
from grim_dawn_sync.session_start import read_session_start_metadata
from grim_dawn_sync.state import load_state
from grim_dawn_sync.version_catalog import VersionCatalogBuilder
from grim_dawn_sync.workflow import LaunchWorkflow
from test_save_sync_git_vault import save
from test_save_sync_session_start_integration import SessionStartIntegrationAdapters, _environment, _plan


def test_local_only_disposition_leaves_remote_and_state_untouched_and_next_launch_is_live_ahead(tmp_path: Path) -> None:
    publisher, adapters, config, local, base = _environment(tmp_path)
    save(config.save_root, b"session-play")
    registry, plan, _ = _plan(adapters, config, local / "state.json", choose_kind="live",
                              mode="launch", archives_root=local / "archives")
    plan = registry.build_plan(catalog_token=plan.catalog_token, candidate_id=plan.candidate_id, mode="launch",
                               case=plan.reconcile_case, exit_disposition="local-only")

    before_remote = adapters.vault.remote_oid()
    before_state = load_state(local / "state.json")

    result = LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)

    assert result == {"state": "COMPLETE", "exit_disposition": "local-only"}
    # Remote main and terminal-local state are byte-for-byte unchanged.
    assert adapters.vault.remote_oid() == before_remote
    assert load_state(local / "state.json") == before_state
    assert inspect_remote_lock_readonly(adapters.vault) is None
    assert not any((local / "archives").glob("save-before-restore-*"))
    # This session's own post-game save was still archived locally.
    session_archives = [item for item in (local / "archives").iterdir() if item.name.startswith("save-")]
    assert any((item / "main/a/player.gdc").exists() for item in session_archives)

    # The next launch observes LIVE_AHEAD: live has data remote never saw.
    state = load_state(local / "state.json")
    catalog = VersionCatalogBuilder(
        adapters.vault, config.save_root, machine_id=config.machine_id,
        baseline_root_hash=state.last_applied_manifest_root_hash, archives_root=local / "archives",
    ).build()
    remote_root = adapters.vault.validate_commit_snapshot(catalog.remote_head)["root_hash"]
    case = classify_reconciliation(live_root_hash=catalog.live_root_hash, remote_root_hash=remote_root,
                                   baseline_root_hash=state.last_applied_manifest_root_hash)
    assert case is ReconcileCase.LIVE_AHEAD


def test_restore_startup_disposition_restores_live_and_keeps_post_game_archive_without_publishing(tmp_path: Path) -> None:
    publisher, adapters, config, local, base = _environment(tmp_path)
    registry, plan, _ = _plan(adapters, config, local / "state.json", choose_kind="live",
                              mode="launch", archives_root=local / "archives")
    plan = registry.build_plan(catalog_token=plan.catalog_token, candidate_id=plan.candidate_id, mode="launch",
                               case=plan.reconcile_case, exit_disposition="restore-startup")

    before_remote = adapters.vault.remote_oid()
    before_state = load_state(local / "state.json")

    class PlayingAdapters(SessionStartIntegrationAdapters):
        def launch(self, hook):
            for state in ("START_DPYES", "WAIT_GAME_START", "WAIT_GAME_EXIT"):
                hook(state)
            save(self.config.save_root, b"played-this-session")

    playing = PlayingAdapters(config, local)
    result = LaunchWorkflow(config, local, adapters=playing).execute_selection_plan(plan, registry)

    assert result == {"state": "COMPLETE", "exit_disposition": "restore-startup"}
    # Remote and terminal-local state are unchanged; no push ever happened.
    assert adapters.vault.remote_oid() == before_remote
    assert load_state(local / "state.json") == before_state
    assert inspect_remote_lock_readonly(adapters.vault) is None
    # Live is back to exactly what it was before this launch started.
    assert (config.save_root / "main/a/player.gdc").read_bytes() == b"base"
    # The post-game data this session actually produced is still archived,
    # both by archive_after_game and by the restore's own before-restore copy.
    archives = local / "archives"
    contents = {(item / "main/a/player.gdc").read_bytes() for item in archives.iterdir()
                if (item / "main/a/player.gdc").exists()}
    assert b"played-this-session" in contents
    assert b"base" in contents  # this launch's own session-start snapshot


def test_restore_startup_announces_each_stage_so_an_interrupted_run_is_diagnosable(tmp_path: Path) -> None:
    """Found on a real machine: this sequence copies and rehashes the save three
    times and can run well over a minute with nothing on screen.  Collapsing it
    into a single state made a killed run look identical to one that never
    began, so each stage must appear in the audit log like the launch-time
    restore already does."""
    publisher, adapters, config, local, base = _environment(tmp_path)
    registry, plan, _ = _plan(adapters, config, local / "state.json", choose_kind="live",
                              mode="launch", archives_root=local / "archives")
    plan = registry.build_plan(catalog_token=plan.catalog_token, candidate_id=plan.candidate_id, mode="launch",
                               case=plan.reconcile_case, exit_disposition="restore-startup")

    class PlayingAdapters(SessionStartIntegrationAdapters):
        def launch(self, hook):
            for state in ("START_DPYES", "WAIT_GAME_START", "WAIT_GAME_EXIT"):
                hook(state)
            save(self.config.save_root, b"played-this-session")

    LaunchWorkflow(config, local, adapters=PlayingAdapters(config, local)).execute_selection_plan(plan, registry)

    rows = [json.loads(line) for line in (local / "logs" / "launch.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    states = [row["state"] for row in rows if row["event"] == "entered"]
    for stage in ("RESTORE_SESSION_START", "ARCHIVE_BEFORE_RESTORE", "APPLY_REMOTE_SAVE"):
        assert stage in states, stage
    # They must appear in that order, after the exit-disposition decision.
    tail = states[states.index("EXIT_DISPOSITION_CONFIRM"):] if "EXIT_DISPOSITION_CONFIRM" in states else states
    ordered = [s for s in tail if s in ("RESTORE_SESSION_START", "ARCHIVE_BEFORE_RESTORE", "APPLY_REMOTE_SAVE")]
    assert ordered == ["RESTORE_SESSION_START", "ARCHIVE_BEFORE_RESTORE", "APPLY_REMOTE_SAVE"]


def test_local_only_recovers_from_a_post_archive_fault_before_release(tmp_path: Path, monkeypatch) -> None:
    """A crash between archive_after_game and the unpublished release still recovers cleanly."""
    publisher, adapters, config, local, base = _environment(tmp_path)
    save(config.save_root, b"session-play")
    registry, plan, _ = _plan(adapters, config, local / "state.json", choose_kind="live",
                              mode="launch", archives_root=local / "archives")
    plan = registry.build_plan(catalog_token=plan.catalog_token, candidate_id=plan.candidate_id, mode="launch",
                               case=plan.reconcile_case, exit_disposition="local-only")

    def broken_release(_lock):
        raise SyncError("injected_release_failure", "simulated crash", EXIT_RECOVERY_REQUIRED)
    monkeypatch.setattr(adapters, "release_without_publish", broken_release)

    before_remote = adapters.vault.remote_oid()
    with pytest.raises(SyncError) as caught:
        LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)
    assert caught.value.exit_code == EXIT_RECOVERY_REQUIRED

    state = load_state(local / "state.json")
    assert state.phase == "lock_held"
    assert inspect_remote_lock_readonly(adapters.vault) is not None

    monkeypatch.undo()
    assert recover_session(adapters.vault, state, "terminal", state_path=local / "state.json") == "abandoned_lock_released"
    assert adapters.vault.remote_oid() == before_remote
    assert inspect_remote_lock_readonly(adapters.vault) is None
    final_state = load_state(local / "state.json")
    assert final_state.last_applied_remote_commit == base
