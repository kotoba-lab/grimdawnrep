"""CLI boundary for save synchronization; T0 performs no save or network mutation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import stat
import sys
import uuid
from typing import Any

from grim_dawn_sync import __version__
from grim_dawn_sync.config import default_config_path, load_config
from grim_dawn_sync.errors import EXIT_OK, SyncError
from grim_dawn_sync.manifest import stable_manifest
from grim_dawn_sync.discovery import cloud_candidates, game_candidates, inspect_path
from grim_dawn_sync.workflow import LaunchWorkflow
from grim_dawn_sync.shortcut import install_shortcut
from grim_dawn_sync.git_vault import GitVault
from grim_dawn_sync.session_lock import (
    acquire_lock,
    inspect_remote_lock_readonly,
    mark_bootstrap_live_applied,
    prepare_bootstrap,
    push_bootstrap,
    recover_session,
    release_lock,
)
from grim_dawn_sync.snapshot import _copy_verified, restore_from_directory
from grim_dawn_sync.state import SyncState, load_state, save_state
from grim_dawn_sync.process_monitor import ProcessMonitor, WindowsProcessMonitor
from grim_dawn_sync.validation import validate_players


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grim-dawn-sync", allow_abbrev=False)
    parser.add_argument("--config", type=Path, default=default_config_path(), help="terminal-local config.local.json")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="read configuration and report readiness")
    subparsers.add_parser("status", help="report terminal-local readiness")
    subparsers.add_parser("recover", help="recover an interrupted session")
    restore = subparsers.add_parser("restore", help="restore a vault commit")
    restore.add_argument("--commit", required=True); restore.add_argument("--apply", action="store_true")
    subparsers.add_parser("snapshot", help="snapshot the current save")
    bootstrap = subparsers.add_parser("bootstrap", help="initialize from an explicit cloud source")
    bootstrap.add_argument("--source-cloud", type=Path, required=True); bootstrap.add_argument("--apply", action="store_true")
    enroll = subparsers.add_parser("enroll", aliases=["join"], help="adopt the current remote snapshot on a new terminal")
    enroll.add_argument("--apply", action="store_true")
    subparsers.add_parser("launch", help="run the synchronized DPYes launch workflow")
    shortcut = subparsers.add_parser("install-shortcut", help="create the Save Sync desktop shortcut")
    shortcut.add_argument("--apply", action="store_true")
    return parser


def _process_preflight(config: Any, monitor: ProcessMonitor | None = None) -> dict[str, Any]:
    """Do not mutate when process enumeration is incomplete or a target runs."""
    scan = (monitor or WindowsProcessMonitor()).scan()
    names = {name.casefold() for name in getattr(config, "game_process_names", ("Grim Dawn.exe",))} | {"dpyes.exe"}
    if not scan.complete:
        raise SyncError("process_scan_incomplete", "Game process status could not be verified; no change was made.", 5)
    matches = [item for item in scan.processes if item.name.casefold() in names]
    if matches:
        raise SyncError("game_already_running", "Grim Dawn or DPYes is running; no change was made.", 5)
    return {"status": "stopped", "complete": True}


def _process_status(config: Any, monitor: ProcessMonitor | None = None) -> dict[str, Any]:
    scan = (monitor or WindowsProcessMonitor()).scan()
    names = {name.casefold() for name in getattr(config, "game_process_names", ("Grim Dawn.exe",))} | {"dpyes.exe"}
    if not scan.complete:
        return {"status": "unknown", "complete": False}
    matches = [item for item in scan.processes if item.name.casefold() in names]
    return {"status": "running" if matches else "clear", "complete": True}


def doctor(config_path: Path, *, monitor: ProcessMonitor | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    save = {"configured": True, "exists": config.save_root.exists(), "manifest": None}
    warnings: list[dict[str, str]] = []
    if config.save_root.is_dir():
        try:
            manifest = stable_manifest(config.save_root, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
            save["manifest"] = {key: manifest[key] for key in ("root_hash", "file_count", "total_bytes", "character_count")}
        except SyncError as error:
            save["validation"] = error.code
    else:
        warnings.append({"code": "save_root_missing", "message": "Configured save root was not found; nothing was created."})
    process = _process_status(config, monitor)
    vault: dict[str, Any] = inspect_path(config.vault_repo)
    try:
        checked = _vault(config); checked.preflight(); remote = checked.remote_oid()
        # Doctor must never fetch: its local tracking ref may be stale, so a
        # Vault supplied remote relation is the only meaningful comparison.
        relation = _live_remote_relation(checked, remote)
        vault.update({"git": "available", "remote_commit": remote, "relation": relation,
                      "active_lock": inspect_remote_lock_readonly(checked) is not None})
    except SyncError as error:
        vault.update({"git": "unavailable", "validation": error.code})
    return {
        "schema_version": "1.0.0",
        "tool_version": __version__,
        "command": "doctor",
        "read_only": True,
        "machine_id": config.machine_id,
        "checks": {
            "config": {"ok": True},
            "save_root": save,
            "cloud": cloud_candidates(config.game_install),
            "vault": vault,
            "launcher": game_candidates(config.game_install, config.launcher_path),
            "processes": process,
        },
        "warnings": warnings,
    }


def _render(payload: dict[str, Any], *, as_json: bool) -> str:
    if as_json:
        # Command results can contain paths supplied by adapters.  The CLI is
        # the serialization boundary, so JSON mode must remain machine-readable
        # rather than failing after a successful operation.
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    if "error" in payload:
        return f"{payload['error']['code']}: {payload['error']['message']}\n"
    if payload.get("command") == "doctor":
        return "doctor: configuration valid; read-only diagnostics complete.\n"
    return f"{payload.get('command', 'grim-dawn-sync')}: complete\n"


def _vault(config) -> GitVault:
    return GitVault(config.vault_repo, remote=config.remote, branch=config.branch)


def _state_path(config_path: Path) -> Path:
    return Path(config_path).parent / "state.json"


def _tree_usage(path: Path) -> dict[str, int]:
    """Best-effort, read-only usage summary; never follows links."""
    if not path.is_dir() or path.is_symlink():
        return {"files": 0, "bytes": 0}
    files = total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file() and not item.is_symlink():
                files += 1; total += item.stat().st_size
    except OSError:
        return {"files": files, "bytes": total}
    return {"files": files, "bytes": total}


def _safe_archive_parent(path: Path) -> None:
    """Create a dedicated archive parent without traversing a link/reparse."""
    chain: list[Path] = []
    current = path
    while True:
        chain.append(current)
        if current.parent == current:
            break
        current = current.parent
    missing: list[Path] = []
    for candidate in reversed(chain):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            missing.append(candidate)
            continue
        if candidate.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400) or not stat.S_ISDIR(info.st_mode):
            raise SyncError("unsafe_archive_path", "Archive parent is not a safe directory.", 6)
    try:
        for candidate in missing:
            candidate.mkdir()
    except OSError as error:
        raise SyncError("unsafe_archive_path", "Archive parent could not be created safely.", 6) from error


def _live_remote_relation(vault: GitVault, remote: str | None) -> str:
    """Read-only relation; do not infer state from a stale tracking ref."""
    result = vault.runner.run("rev-parse", "--verify", "--quiet", "HEAD", check=False)
    local = result.stdout.strip() if result.returncode == 0 else None
    if remote is None:
        return "unborn" if local is None else "remote_missing"
    return "equal" if local == remote else "remote_changed_or_unknown"


def _local_head_oid(vault: GitVault) -> str | None:
    result = vault.runner.run("rev-parse", "--verify", "--quiet", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def status(config_path: Path, *, monitor: ProcessMonitor | None = None) -> dict[str, Any]:
    config = load_config(config_path); vault = _vault(config); state_path = _state_path(config_path)
    try:
        state = load_state(state_path)
    except SyncError as error:
        if error.code != "state_missing": raise
        state = SyncState()
    vault.preflight()
    remote = vault.remote_oid(); lock = inspect_remote_lock_readonly(vault)
    relation = _live_remote_relation(vault, remote)
    recovery = state.phase is not None
    readiness = "recovery_required" if recovery else ("ready" if relation in {"equal", "behind"} and remote else "blocked")
    return {"schema_version":"1.0.0", "command":"status", "readiness":readiness,
            "remote_commit":remote, "last_pushed_commit":state.pushed_commit or state.last_applied_remote_commit,
            "vault_relation":relation, "active_lock": None if lock is None else {"machine_id":lock.session.machine_id, "session_id":lock.session.session_id, "started_at":lock.session.started_at},
            "recovery_phase":state.phase,
            "processes": _process_status(config, monitor),
            "archive_usage": _tree_usage(Path(config_path).parent / "archives"),
            "vault_usage": _tree_usage(Path(config.vault_repo) / "save")}


def recover(config_path: Path) -> dict[str, Any]:
    config = load_config(config_path); vault = _vault(config); vault.preflight()
    state = load_state(_state_path(config_path))
    result = recover_session(vault, state, config.machine_id, state_path=_state_path(config_path))
    return {"schema_version":"1.0.0", "command":"recover", "result":result}


def restore(config_path: Path, commit: str, *, apply: bool) -> dict[str, Any]:
    config = load_config(config_path); vault = _vault(config); vault.preflight()
    root = Path(config_path).parent
    if not apply:
        # Do not use extract_save here: extraction necessarily creates a
        # staging directory.  A default restore is an inspection only.
        return {"schema_version": "1.0.0", "command": "restore", "commit": commit,
                **_inspect_restore(vault, commit)}
    _validate_restore_ancestry(vault, commit)
    _process_preflight(config)
    # Extract validates the historical manifest before any live-save operation.
    # ``extract_save`` publishes through a create-only staging sibling.  The
    # CLI owns its staging parent, so prepare it safely before extraction.
    _safe_archive_parent(root / "staging")
    # Keep every attempt create-only.  A prior successful restore may retain
    # its extracted tree for recovery inspection, so its destination must not
    # prevent the same historical commit from being restored again.
    destination = root / "staging" / f"restore-{commit}-{uuid.uuid4().hex}"
    vault.extract_save(commit, destination, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    result = restore_from_directory(destination, config.save_root, root / "archives", root / "recovery", machine_id=config.machine_id, apply=apply, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    return {"schema_version":"1.0.0", "command":"restore", "commit":commit, **result}


def snapshot(config_path: Path) -> dict[str, Any]:
    config = load_config(config_path); vault = _vault(config); state_path = _state_path(config_path)
    _process_preflight(config)
    # Preserve the exact live tree before Git/lock operations.  A later push or
    # release failure therefore always leaves a local, verified recovery copy.
    manifest = stable_manifest(config.save_root, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    validation = validate_players(config.save_root, manifest)
    if not validation.get("ok"):
        raise SyncError(validation.get("classification", "save_invalid"), "Save validation did not permit snapshot.", 3)
    archive_parent = Path(config_path).parent / "archives"
    _safe_archive_parent(archive_parent)
    archive = archive_parent / f"save-before-snapshot-{manifest['root_hash'][:16]}-{uuid.uuid4().hex}"
    _copy_verified(config.save_root, archive, manifest, config.machine_id, config.stable_scan_retries, 0, validate_players)
    status_value = vault.update_fast_forward()
    if status_value.relation in {"ahead", "diverged", "unborn"}: raise SyncError("vault_not_reconciled", "Vault must be synchronized before snapshot.", 4)
    base = vault.remote_oid()
    if not base: raise SyncError("remote_main_missing", "Remote main is missing; run bootstrap first.", 4)
    lock = acquire_lock(vault, config.machine_id, base, state_path=state_path)
    oid = vault.snapshot(config.save_root, machine_id=config.machine_id, session_id=lock.session.session_id, expected_manifest=manifest, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    save_state(state_path, SyncState(last_applied_manifest_root_hash=manifest["root_hash"], session_id=lock.session.session_id, machine_id=lock.session.machine_id, base_commit=lock.session.base_commit, lock_oid=lock.oid, local_tag=lock.local_tag, phase="committed", local_commit=oid))
    pushed = vault.push(oid); release_lock(vault, lock, pushed, state_path=state_path, confirmed_root_hash=manifest["root_hash"])
    return {"schema_version":"1.0.0", "command":"snapshot", "commit":pushed, "root_hash": manifest["root_hash"]}


def bootstrap(config_path: Path, source: Path, *, apply: bool) -> dict[str, Any]:
    config = load_config(config_path); vault = _vault(config); root = Path(config_path).parent
    if not source.is_dir(): raise SyncError("cloud_source_missing", "Cloud save source does not exist.", 3)
    # Plan first; dry-run has no live or Git mutation.
    planned = restore_from_directory(source, config.save_root, root / "archives", root / "recovery", machine_id=config.machine_id, apply=False, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    if not apply: return {"schema_version":"1.0.0", "command":"bootstrap", **planned}
    _process_preflight(config)
    vault.preflight()
    try:
        prior_state = load_state(_state_path(config_path))
    except SyncError as error:
        if error.code != "state_missing":
            raise
        prior_state = SyncState()
    resuming = prior_state.phase == "bootstrap_pending"
    if prior_state.phase is not None and not resuming:
        raise SyncError("recovery_required", "Another recovery session must be completed first.", 6)
    manifest = stable_manifest(source, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    validation = validate_players(source, manifest)
    if not validation.get("ok"):
        raise SyncError(validation.get("classification", "save_invalid"), "Cloud save validation did not permit bootstrap.", 3)
    if resuming:
        return _resume_bootstrap_cli(config_path, config, vault, prior_state, manifest)
    if config.save_root.exists() or config.save_root.is_symlink():
        raise SyncError("bootstrap_live_not_empty", "Bootstrap requires a missing live save root.", 3)
    initial_remote = vault.remote_oid()
    if initial_remote is not None:
        raise SyncError("bootstrap_remote_not_empty", "Bootstrap requires an empty remote main.", 4)
    # Preserve the cloud input before creating a live save.  The archive name
    # is unique and _copy_verified is create-only/symlink-safe.
    archive_parent = root / "archives"
    _safe_archive_parent(archive_parent)
    archive = archive_parent / f"cloud-before-bootstrap-{manifest['root_hash'][:16]}-{uuid.uuid4().hex}"
    _copy_verified(source, archive, manifest, config.machine_id, config.stable_scan_retries, config.stable_window_seconds, validate_players)
    oid = vault.snapshot(archive, machine_id=config.machine_id, session_id="bootstrap", expected_manifest=manifest, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    # Recheck after local commit creation so a remote bootstrap race cannot
    # proceed into live-save creation.
    remote = vault.remote_oid()
    if remote is not None:
        raise SyncError("bootstrap_remote_not_empty", "Bootstrap requires an empty or exactly matching pending remote.", 4)
    pending = prepare_bootstrap(vault, config.machine_id, oid, manifest["root_hash"], state_path=_state_path(config_path))
    return _resume_bootstrap_cli(config_path, config, vault, pending, manifest)


def _enroll_existing_state(state_path: Path, config: Any, commit: str, root_hash: str) -> bool:
    """Return true only for this terminal's already-complete exact baseline."""
    try:
        state = load_state(state_path)
    except SyncError as error:
        if error.code == "state_missing":
            return False
        raise
    if state.phase is not None:
        raise SyncError("recovery_required", "An interrupted session must be recovered before enrollment.", 6)
    if (
        state.machine_id == config.machine_id
        and state.last_applied_remote_commit == commit
        and state.last_applied_manifest_root_hash == root_hash
    ):
        return True
    raise SyncError("enroll_state_exists", "Existing terminal state does not match this remote snapshot.", 6)


def enroll(config_path: Path, *, apply: bool) -> dict[str, Any]:
    """Join a non-empty vault without pushing or overwriting an existing save.

    Dry-run deliberately does not fetch: it reports the advertised remote OID
    but cannot attest its snapshot until ``--apply`` has fetched it locally.
    """
    config = load_config(config_path); vault = _vault(config); state_path = _state_path(config_path)
    vault.preflight()
    remote = vault.remote_oid()
    lock = inspect_remote_lock_readonly(vault)
    if lock is not None:
        raise SyncError("lock_held", "Another terminal holds the remote sync lock.", 6)
    if remote is None:
        raise SyncError("remote_main_missing", "Remote main is missing; enrollment cannot continue.", 4)
    if not apply:
        if _local_head_oid(vault) != remote:
            raise SyncError("vault_not_reconciled", "Dry-run requires local HEAD to equal advertised remote main.", 4)
        return {"schema_version": "1.0.0", "command": "enroll", "dry_run": True,
                "remote_commit": remote, "remote_snapshot_verified": False,
                "note": "Dry-run does not fetch or create staging; use --apply to verify and enroll."}
    _process_preflight(config)
    # Refuse an interrupted local session before fetch can advance this clone.
    # A missing state is the normal first-enrollment condition.
    try:
        preliminary_state = load_state(state_path)
    except SyncError as error:
        if error.code != "state_missing":
            raise
    else:
        if preliminary_state.phase is not None:
            raise SyncError("recovery_required", "An interrupted session must be recovered before enrollment.", 6)
    status_value = vault.update_fast_forward()
    # ``update_fast_forward`` reports the pre-merge relation, so ``behind``
    # is acceptable only when the subsequent HEAD equality check proves its
    # fast-forward completed.  All other non-equal relations are unsafe.
    if status_value.relation in {"ahead", "diverged", "unborn"}:
        raise SyncError("vault_not_reconciled", "Vault must fast-forward to remote main before enrollment.", 4)
    remote = vault.remote_oid()
    if remote is None:
        raise SyncError("remote_main_missing", "Remote main disappeared during enrollment.", 4)
    if _local_head_oid(vault) != remote:
        raise SyncError("vault_not_reconciled", "Vault HEAD did not match fetched remote main.", 4)
    if inspect_remote_lock_readonly(vault) is not None:
        raise SyncError("lock_held", "Another terminal holds the remote sync lock.", 6)
    manifest = vault.validate_commit_snapshot(remote)
    root_hash = manifest["root_hash"]
    idempotent = _enroll_existing_state(state_path, config, remote, root_hash)
    live_manifest: dict[str, Any] | None = None
    if config.save_root.exists() or config.save_root.is_symlink():
        if config.save_root.is_symlink() or not config.save_root.is_dir():
            raise SyncError("enroll_live_unsafe", "Existing live save path is not a safe directory.", 6)
        live_manifest = stable_manifest(config.save_root, machine_id=config.machine_id,
                                        retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
        if live_manifest.get("file_count", 0) == 0:
            raise SyncError("enroll_live_conflict", "Existing live save is empty; no overwrite was made.", 4)
        validation = validate_players(config.save_root, live_manifest)
        if not validation.get("ok"):
            raise SyncError(validation.get("classification", "save_invalid"), "Existing live save validation failed.", 3)
        if live_manifest["root_hash"] != root_hash:
            raise SyncError("enroll_live_conflict", "Existing live save differs from remote; no overwrite was made.", 4)
    if idempotent:
        if live_manifest is None:
            raise SyncError("enroll_live_missing", "Enrolled live save is missing; recovery is required.", 6)
        return {"schema_version": "1.0.0", "command": "enroll", "dry_run": False,
                "commit": remote, "root_hash": root_hash, "idempotent": True}
    if live_manifest is None:
        root = Path(config_path).parent
        _safe_archive_parent(root / "staging")
        destination = root / "staging" / f"enroll-{remote}-{uuid.uuid4().hex}"
        vault.extract_save(remote, destination, machine_id=config.machine_id,
                           retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
        if vault.remote_oid() != remote or inspect_remote_lock_readonly(vault) is not None:
            raise SyncError("enroll_remote_changed", "Remote changed or became locked before restore; no live save was changed.", 4)
        _process_preflight(config)
        result = restore_from_directory(destination, config.save_root, root / "archives", root / "recovery",
                                        machine_id=config.machine_id, apply=True,
                                        retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
        if result.get("root_hash") != root_hash:
            raise SyncError("enroll_live_mismatch", "Restored live save did not match remote snapshot.", 6)
    observed = stable_manifest(config.save_root, machine_id=config.machine_id,
                               retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
    if observed["root_hash"] != root_hash:
        raise SyncError("enroll_live_mismatch", "Live save did not match remote snapshot.", 6)
    if vault.remote_oid() != remote or inspect_remote_lock_readonly(vault) is not None:
        raise SyncError("enroll_remote_changed", "Remote changed or became locked before state was saved.", 4)
    save_state(state_path, SyncState(last_applied_remote_commit=remote,
                                     last_applied_manifest_root_hash=root_hash,
                                     machine_id=config.machine_id))
    return {"schema_version": "1.0.0", "command": "enroll", "dry_run": False,
            "commit": remote, "root_hash": root_hash, "idempotent": False}


def _resume_bootstrap_cli(
    config_path: Path,
    config: Any,
    vault: GitVault,
    pending: SyncState,
    source_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Resume only the exact prepared commit; never create a replacement."""
    oid = pending.local_commit
    root_hash = pending.last_applied_manifest_root_hash
    if (
        pending.phase != "bootstrap_pending"
        or pending.machine_id != config.machine_id
        or not isinstance(oid, str)
        or not isinstance(root_hash, str)
        or source_manifest.get("root_hash") != root_hash
    ):
        raise SyncError("bootstrap_recovery_mismatch", "Bootstrap retry does not match pending recovery.", 4)
    committed = vault.validate_commit_snapshot(oid)
    if committed.get("root_hash") != root_hash:
        raise SyncError("bootstrap_recovery_mismatch", "Pending bootstrap commit does not match recovery state.", 4)
    head = vault.runner.run("rev-parse", "--verify", "--quiet", "HEAD", check=False)
    if head.returncode or head.stdout.strip() != oid:
        raise SyncError("bootstrap_recovery_mismatch", "Pending bootstrap commit is not the current local HEAD.", 4)
    remote = vault.remote_oid()
    if remote not in {None, oid}:
        raise SyncError("bootstrap_remote_not_empty", "Bootstrap remote does not match pending recovery.", 4)

    live_ready = False
    if config.save_root.is_dir() and not config.save_root.is_symlink():
        try:
            live_ready = next(config.save_root.iterdir(), None) is not None
        except OSError as error:
            raise SyncError("bootstrap_live_unreadable", "Bootstrap live save could not be inspected.", 6) from error
    elif config.save_root.exists() or config.save_root.is_symlink():
        raise SyncError("bootstrap_live_mismatch", "Bootstrap live path is not a safe save directory.", 6)

    if live_ready:
        observed = stable_manifest(config.save_root, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
        if observed["root_hash"] != root_hash:
            raise SyncError("bootstrap_live_mismatch", "Existing bootstrap live save does not match pending recovery.", 6)
    elif pending.bootstrap_live_applied:
        raise SyncError("bootstrap_live_mismatch", "Applied bootstrap live save is missing.", 6)
    else:
        root = Path(config_path).parent
        # The CLI owns its state-local staging parent.  GitVault owns only
        # the create-only extraction destination below it, so prepare this
        # ordinary directory without following links/reparse points first.
        staging_root = root / "staging"
        _safe_archive_parent(staging_root)
        extracted = staging_root / f"bootstrap-{oid}-{uuid.uuid4().hex}"
        vault.extract_save(oid, extracted, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
        applied = restore_from_directory(extracted, config.save_root, root / "archives", root / "recovery", machine_id=config.machine_id, apply=True, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
        observed = stable_manifest(config.save_root, machine_id=config.machine_id, retries=config.stable_scan_retries, window_seconds=config.stable_window_seconds)
        if observed["root_hash"] != root_hash or applied.get("root_hash") != root_hash:
            raise SyncError("bootstrap_live_mismatch", "Applied bootstrap live save failed exact verification.", 6)

    if not pending.bootstrap_live_applied:
        mark_bootstrap_live_applied(config.machine_id, oid, root_hash, root_hash, state_path=_state_path(config_path))
    pushed = push_bootstrap(vault, config.machine_id, oid, root_hash, state_path=_state_path(config_path))
    return {"schema_version": "1.0.0", "command": "bootstrap", "commit": pushed, "root_hash": root_hash, "dry_run": False}


_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z", re.IGNORECASE)


def _inspect_restore(vault: GitVault, commit: str) -> dict[str, Any]:
    """Validate the requested historical save without materializing it.

    GitVault deliberately keeps extraction and validation together.  The CLI
    needs this small read-only counterpart so its default action cannot create
    a staging, archive, journal, or state artifact.
    """
    _validate_restore_ancestry(vault, commit)
    try:
        # This is deliberately a single strict Vault operation.  It validates
        # the canonical manifest and every committed save-tree entry without
        # materialising a staging directory.
        manifest = vault.validate_commit_snapshot(commit)
        root_hash = manifest["root_hash"]
        file_count = manifest["file_count"]
        total_bytes = manifest["total_bytes"]
    except (KeyError, TypeError, ValueError, SyncError) as error:
        raise SyncError("restore_manifest_invalid", "Restore commit has an invalid save manifest.", 3) from error
    if not isinstance(root_hash, str) or not _OID.fullmatch(root_hash) or not isinstance(file_count, int) or not isinstance(total_bytes, int):
        raise SyncError("restore_manifest_invalid", "Restore commit has an invalid save manifest.", 3)
    return {"dry_run": True, "root_hash": root_hash, "file_count": file_count, "total_bytes": total_bytes}


def _validate_restore_ancestry(vault: GitVault, commit: str) -> None:
    branch_ref = f"refs/heads/{vault.branch}"
    result = vault.runner.run("merge-base", "--is-ancestor", commit, branch_ref, check=False)
    if result.returncode == 1:
        raise SyncError("restore_commit_not_in_history", "Restore commit is not in configured branch history.", 3)
    if result.returncode != 0:
        raise SyncError("git_command_failed", "Restore commit ancestry could not be verified.", 4)


_SAFE_DETAIL_KEYS = {
    "safe_oid", "safe_root_hash", "archive_root", "quarantine_root",
    "archive_id", "quarantine_id", "machine_id", "session_id",
    "last_state", "next_command", "local_commit", "root_hash",
    "archive", "quarantine",
}


def _safe_error_payload(error: SyncError) -> dict[str, Any]:
    """Keep CLI failures useful without reflecting paths, remotes, or stderr."""
    details: dict[str, Any] = {}
    for key, value in error.details.items():
        if key not in _SAFE_DETAIL_KEYS:
            continue
        if key in {"safe_oid", "local_commit"} and isinstance(value, str) and _OID.fullmatch(value):
            details[key] = value
        elif key in {"safe_root_hash", "root_hash", "archive_root", "quarantine_root"} and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            details[key] = value
        elif key in {"archive_id", "quarantine_id", "archive", "quarantine", "machine_id", "session_id", "last_state"} and isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
            details[key] = value
        elif key == "next_command" and value in {"grim-dawn-sync recover", "grim-dawn-sync status"}:
            details[key] = value
    payload = {"error": {"code": error.code, "message": error.message, "details": details}}
    payload["error"]["next_command"] = details.get("next_command") or ("grim-dawn-sync recover" if error.exit_code == 6 or error.code == "lock_held" else "grim-dawn-sync status")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor": payload = doctor(args.config)
        elif args.command == "status": payload = status(args.config)
        elif args.command == "recover": payload = recover(args.config)
        elif args.command == "restore": payload = restore(args.config, args.commit, apply=args.apply)
        elif args.command == "snapshot": payload = snapshot(args.config)
        elif args.command == "bootstrap": payload = bootstrap(args.config, args.source_cloud, apply=args.apply)
        elif args.command in {"enroll", "join"}: payload = enroll(args.config, apply=args.apply)
        elif args.command == "launch":
            config = load_config(args.config)
            result = LaunchWorkflow(config, args.config.parent).run()
            payload = {"schema_version": "1.0.0", "command": "launch", "result": result}
        elif args.command == "install-shortcut":
            if not args.apply: payload = {"schema_version": "1.0.0", "command": "install-shortcut", "dry_run": True}
            else:
                desktop = Path.home() / "Desktop"
                install_shortcut(desktop)
                payload = {"schema_version": "1.0.0", "command": "install-shortcut", "created": True}
        exit_code = EXIT_OK
    except SyncError as error:
        payload = _safe_error_payload(error)
        exit_code = error.exit_code
    except Exception:
        # The command boundary must not leak a Python exception, Git stderr,
        # or local save path when an adapter violates its error contract.
        payload = _safe_error_payload(SyncError(
            "operation_failed", "The operation stopped safely; run recovery before trying again.", 6,
        ))
        exit_code = 6
    sys.stdout.write(_render(payload, as_json=args.json))
    return exit_code
