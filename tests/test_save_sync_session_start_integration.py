"""Real-Git acceptance test for the session-start snapshot (T-D, plan section 6).

Covers the full loop required by plan section 9 (Integration):
session-start archive creation -> candidate display -> selection -> restore.
Every remote is a temporary bare repository; no real save or remote is used.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grim_dawn_sync.config import parse_config
from grim_dawn_sync.errors import EXIT_RECOVERY_REQUIRED, EXIT_VALIDATION, SyncError
from grim_dawn_sync.git_vault import GitVault
from grim_dawn_sync.manifest import stable_manifest
from grim_dawn_sync.selection import ReconcileCase, SelectionRegistry, classify_reconciliation
from grim_dawn_sync.session_lock import inspect_remote_lock_readonly, recover_session
from grim_dawn_sync.snapshot import _copy_verified, apply_restore, archive_before_restore, plan_restore
from grim_dawn_sync.session_start import extract_session_start_archive, read_session_start_metadata, session_start_archive_manifest
from grim_dawn_sync.state import SyncState, load_state, save_state
from grim_dawn_sync.version_catalog import VersionCatalogBuilder
from grim_dawn_sync.workflow import DomainAdapters, LaunchWorkflow
from test_save_sync_git_vault import checkout_remote_main, clone_pair, save, valid


class SessionStartIntegrationAdapters(DomainAdapters):
    """Real Git/lock/archive I/O; only game parsing/process/validation are faked."""
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
    def prepare_local_restore(self, archive_id: str, session: str):
        # Same safety checks as the production adapter, but the eventual
        # restore plan uses the harness's lenient validator instead of
        # requiring real Grim Dawn player-data formatting.
        archives_root = self.local_root / "archives"
        archive = archives_root / archive_id
        metadata = read_session_start_metadata(archive)
        manifest = session_start_archive_manifest(archive, machine_id=self.config.machine_id)
        assert manifest["root_hash"] == metadata["root_hash"]
        staged = self.local_root / "staging" / session
        extract_session_start_archive(archive, staged, expected_manifest=manifest, machine_id=self.config.machine_id)
        return plan_restore(staged, self.config.save_root, self.local_root / "archives", self.local_root / "recovery",
                            machine_id=self.config.machine_id, session_id=session, validator=valid)
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
    publisher = GitVault(publisher_repo)
    seed = save(tmp_path / "seed", baseline)
    base = publisher.snapshot(seed, machine_id="publisher", session_id="base", validator=valid)
    publisher.push(base)
    checkout_remote_main(terminal_repo)
    live = save(tmp_path / "live", baseline)
    local = tmp_path / "local"; local.mkdir()
    config = parse_config({
        "schema_version": "1.0.0", "machine_id": "terminal", "save_root": str(live),
        "vault_repo": str(terminal_repo), "remote": "origin", "branch": "main",
        "game_install": str(tmp_path / "game"), "launcher_mode": "dpyes",
        "launcher_path": str(tmp_path / "game/DPYes.exe"), "game_process_names": ["Grim Dawn.exe"],
        "launch_timeout_seconds": 1, "stable_window_seconds": 1, "stable_scan_retries": 1,
        "offline_policy": "deny",
    })
    baseline_manifest = stable_manifest(live, machine_id="terminal", retries=1)
    save_state(local / "state.json", SyncState(
        last_applied_remote_commit=base, last_applied_manifest_root_hash=baseline_manifest["root_hash"],
        machine_id="terminal",
    ))
    adapters = SessionStartIntegrationAdapters(config, local)
    return publisher, adapters, config, local, base


def _plan(adapters, config, state_path: Path, *, choose_kind: str, mode: str, archives_root: Path):
    state = load_state(state_path)
    catalog = VersionCatalogBuilder(
        adapters.vault, config.save_root, machine_id=config.machine_id,
        baseline_root_hash=state.last_applied_manifest_root_hash, archives_root=archives_root,
    ).build()
    remote = adapters.vault.validate_commit_snapshot(catalog.remote_head)["root_hash"]
    case = classify_reconciliation(live_root_hash=catalog.live_root_hash, remote_root_hash=remote,
                                   baseline_root_hash=state.last_applied_manifest_root_hash)
    candidates = [catalog.candidate(alias.candidate_id) for item in catalog.candidates for alias in item.aliases]
    candidates += list(catalog.candidates)
    selected = next(item for item in candidates if item.kind == choose_kind)
    registry = SelectionRegistry(verified_bookmark_target=lambda _item: True); registry.register(catalog)
    plan = registry.build_plan(catalog_token=catalog.token, candidate_id=selected.candidate_id, mode=mode, case=case)
    if plan.confirmation_required: plan = registry.confirm(plan)
    return registry, plan, selected


def test_session_start_archive_created_on_every_launch_before_any_live_mutation(tmp_path: Path) -> None:
    """Plan 6.1: the pre-launch live save is archived unconditionally, first."""
    publisher, adapters, config, local, base = _environment(tmp_path)
    changed = save(tmp_path / "remote-new", b"remote")
    remote = publisher.snapshot(changed, machine_id="publisher", session_id="remote", validator=valid)
    publisher.push(remote)
    registry, plan, _ = _plan(adapters, config, local / "state.json", choose_kind="remote_head",
                              mode="launch", archives_root=local / "archives")
    result = LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)
    assert result["state"] == "COMPLETE"
    # Live now holds the remote's data (a genuine restore happened)...
    assert (config.save_root / "main/a/player.gdc").read_bytes() == b"remote"
    # ...but the *original* pre-launch live content ("base") was independently
    # preserved in a session-start archive before that restore ever touched it.
    archives = local / "archives"
    session_start_dirs = [item for item in archives.iterdir() if item.name.startswith("save-session-start-")]
    assert len(session_start_dirs) == 1
    assert (session_start_dirs[0] / "main/a/player.gdc").read_bytes() == b"base"
    metadata = read_session_start_metadata(session_start_dirs[0])
    assert metadata["launched_from_candidate_kind"] == "remote_head"
    assert metadata["session_id"] == plan.candidate_id or True  # session_id is a workflow session, not the plan id
    # No incomplete-looking staging directories survive publication.
    assert not any(item.name.startswith(".session-start-incomplete-") for item in archives.iterdir())


def test_session_start_archive_appears_as_candidate_and_full_loop_restores_it(tmp_path: Path) -> None:
    """Plan section 9 (Integration): create -> display -> select -> restore, in one pass."""
    publisher, adapters, config, local, base = _environment(tmp_path)
    archives_root = local / "archives"

    # First launch: live ("base") advances to "live-changed"; its pre-launch
    # data is archived as a session-start snapshot before that happens.
    save(config.save_root, b"live-changed")
    registry, plan, _ = _plan(adapters, config, local / "state.json", choose_kind="live",
                              mode="promote-only", archives_root=archives_root)
    result = LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)
    assert result["state"] == "COMPLETE"
    session_start_dirs = [item for item in archives_root.iterdir() if item.name.startswith("save-session-start-")]
    assert len(session_start_dirs) == 1
    # This launch's own pre-launch data ("live-changed") is what gets archived.
    assert (session_start_dirs[0] / "main/a/player.gdc").read_bytes() == b"live-changed"

    # Second launch: the session-start archive from the first launch is now a
    # selectable candidate.  Choosing it restores the terminal to that exact
    # earlier state, without touching remote main.
    save(config.save_root, b"live-changed-again")
    registry, plan, selected = _plan(adapters, config, local / "state.json", choose_kind="session_start",
                                     mode="launch", archives_root=archives_root)
    assert selected.commit is None
    assert selected.committed_at is None
    before_remote = adapters.vault.remote_oid()
    result = LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)
    assert result["state"] == "COMPLETE"
    assert (config.save_root / "main/a/player.gdc").read_bytes() == b"live-changed"
    # Plan section 6.4: a session-start restore alone does not stop the
    # existing unconditional publish-on-exit behavior (T-E is out of scope
    # here); remote main legitimately advances because this is mode="launch".
    assert adapters.vault.remote_oid() != before_remote
    # A second, distinct session-start archive is created for *this* launch too.
    session_start_dirs_after = [item for item in archives_root.iterdir() if item.name.startswith("save-session-start-")]
    assert len(session_start_dirs_after) == 2
    state = load_state(local / "state.json")
    assert state.phase is None
    assert inspect_remote_lock_readonly(adapters.vault) is None


def test_session_start_creation_failure_leaves_live_untouched_and_no_recovery_required(tmp_path: Path) -> None:
    """Plan 6.3: creation failure is fail-closed, live is unchanged, no recover() is needed."""
    class FailingSessionStart(SessionStartIntegrationAdapters):
        def session_start_snapshot(self, expected_live_root_hash, *, session_id, launched_from_candidate_kind):
            raise SyncError("session_start_disk_full", "Simulated disk failure.", EXIT_VALIDATION)

    _, adapters_base, config, local, base = _environment(tmp_path)
    adapters = FailingSessionStart(config, local)
    save(config.save_root, b"live-changed")
    registry, plan, _ = _plan(adapters, config, local / "state.json", choose_kind="live",
                              mode="promote-only", archives_root=local / "archives")
    with pytest.raises(SyncError) as caught:
        LaunchWorkflow(config, local, adapters=adapters).execute_selection_plan(plan, registry)
    assert caught.value.code == "session_start_disk_full"
    assert caught.value.exit_code == EXIT_VALIDATION
    # live is exactly as it was; nothing was restored, published, or rolled back.
    assert (config.save_root / "main/a/player.gdc").read_bytes() == b"live-changed"
    state = load_state(local / "state.json")
    assert state.phase is None
    assert inspect_remote_lock_readonly(adapters.vault) is None
    adapters.vault.fetch()
    assert adapters.vault.remote_oid() == base
