"""Fail-closed archive and directory-swap primitives for save restore."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import shutil
import uuid
from typing import Callable

from grim_dawn_sync.errors import EXIT_RECOVERY_REQUIRED, EXIT_VALIDATION, SyncError
from grim_dawn_sync.manifest import _reject_unsafe, assert_safe_save_file, stable_manifest, validate_manifest_path
from grim_dawn_sync.validation import validate_players

Validator = Callable[[Path, dict], dict]


@dataclass(frozen=True)
class RestorePlan:
    session_id: str; source: Path; live: Path; archive: Path | None; stage: Path; rollback: Path
    source_manifest: dict; live_manifest: dict | None
    archive_complete: bool = False


def _manifest(path: Path, machine_id: str, retries: int, window: float, validator: Validator) -> dict:
    result = stable_manifest(path, machine_id=machine_id, retries=retries, window_seconds=window)
    validation = validator(path, result)
    if not validation.get("ok", False):
        raise SyncError(validation["classification"], "Save validation did not permit restore.", EXIT_VALIDATION)
    return result


def _reject_existing(path: Path) -> None:
    if path.exists() or path.is_symlink():
        _reject_unsafe(path)


def _present_safe(path: Path) -> bool:
    """Distinguish a missing path from a dangling link, which is unsafe."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise SyncError("save_scan_failed", "Save path could not be inspected.", EXIT_VALIDATION) from error
    _reject_unsafe(path)
    return True


def _reject_existing_ancestors(path: Path) -> None:
    current = path
    while True:
        _reject_existing(current)
        if current.parent == current:
            return
        current = current.parent


def _safe_tree(root: Path) -> tuple[list[str], list[Path]]:
    if not root.is_dir():
        raise SyncError("save_root_missing", "Configured save root does not exist.", EXIT_VALIDATION)
    _reject_unsafe(root)
    directories: list[str] = []
    files: list[Path] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        current_path = Path(current); _reject_unsafe(current_path)
        for name in dirs:
            entry = current_path / name; _reject_unsafe(entry)
            directories.append(validate_manifest_path(entry.relative_to(root).as_posix()))
        for name in names:
            entry = current_path / name; _reject_unsafe(entry)
            if entry.is_file(): files.append(entry)
    return sorted(directories, key=str.casefold), sorted(files, key=lambda item: validate_manifest_path(item.relative_to(root).as_posix()).casefold())


def _safe_target(root: Path, relative: str) -> Path:
    relative = validate_manifest_path(relative)
    candidate = root.joinpath(*relative.split("/"))
    _reject_existing_ancestors(candidate)
    return candidate


def _copy_verified(source: Path, destination: Path, expected: dict, machine_id: str, retries: int, window: float, validator: Validator) -> None:
    if destination.exists() or destination.is_symlink():
        raise SyncError("snapshot_name_collision", "Snapshot destination already exists.", EXIT_VALIDATION)
    try:
        source_dirs, entries = _safe_tree(source)
        _reject_existing_ancestors(destination.parent)
        destination.mkdir()
        for relative in source_dirs:
            _safe_target(source, relative)
            target = _safe_target(destination, relative)
            target.mkdir()
            _safe_target(source, relative); _safe_target(destination, relative)
        for entry in entries:
            relative = validate_manifest_path(entry.relative_to(source).as_posix())
            _safe_target(source, relative)
            target = _safe_target(destination, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target, follow_symlinks=False)
            _safe_target(source, relative); _safe_target(destination, relative)
    except OSError as error:
        raise SyncError("snapshot_copy_failed", "Snapshot copy failed safely.", EXIT_VALIDATION) from error
    post_dirs, _ = _safe_tree(source)
    if post_dirs != source_dirs or stable_manifest(source, machine_id=machine_id, retries=retries, window_seconds=window)["root_hash"] != expected["root_hash"]:
        raise SyncError("source_changed", "Restore source changed during copy.", EXIT_VALIDATION)
    destination_dirs, _ = _safe_tree(destination)
    if destination_dirs != source_dirs:
        raise SyncError("snapshot_directory_mismatch", "Copied snapshot directory set did not match source.", EXIT_VALIDATION)
    actual = _manifest(destination, machine_id, retries, window, validator)
    if actual["root_hash"] != expected["root_hash"]:
        raise SyncError("snapshot_hash_mismatch", "Copied snapshot did not match source.", EXIT_VALIDATION)


def _write_journal(state_root: Path, payload: dict) -> None:
    try:
        _reject_existing_ancestors(state_root)
        state_root.mkdir(parents=True, exist_ok=True)
        _reject_unsafe(state_root)
        target = state_root / "restore-journal.json"
        temporary = state_root / f".restore-journal-{uuid.uuid4().hex}.tmp"
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, target)
    except OSError as error:
        raise SyncError("journal_write_failed", "Recovery journal could not be written.", EXIT_RECOVERY_REQUIRED, {"phase": payload["phase"], "session_id": payload["session_id"]}) from error


def plan_restore(source: Path, live: Path, archive_root: Path, state_root: Path, *, machine_id: str, retries: int = 1, window_seconds: float = 0, session_id: str | None = None, validator: Validator = validate_players) -> RestorePlan:
    session = session_id or uuid.uuid4().hex
    source_manifest = _manifest(source, machine_id, retries, window_seconds, validator)
    live_manifest = _manifest(live, machine_id, retries, window_seconds, validator) if _present_safe(live) else None
    stage = live.parent / f".save-sync-stage-{session}"; rollback = live.parent / f".save-sync-rollback-{session}"
    archive = archive_root / f"save-before-restore-{session}" if live_manifest else None
    for path in (stage, rollback, archive):
        if path is not None and (path.exists() or path.is_symlink()):
            raise SyncError("snapshot_name_collision", "Snapshot destination already exists.", EXIT_VALIDATION)
    return RestorePlan(session, source, live, archive, stage, rollback, source_manifest, live_manifest)


def _journal_failure(code: str, message: str, session_id: str, phase: str, error: SyncError) -> SyncError:
    return SyncError(code, message, EXIT_RECOVERY_REQUIRED, {"session_id": session_id, "phase": phase, "journal_error": error.code})


def archive_before_restore(plan: RestorePlan, *, machine_id: str, retries: int = 1, window_seconds: float = 0, validator: Validator = validate_players, current_manifest: dict | None = None) -> RestorePlan:
    """Publish the live-save archive before *any* live-save mutation.

    This is intentionally a separately observable operation for the launch
    state machine.  ``apply_restore`` still archives when called directly, so
    the small restore API remains safe for callers outside the workflow.
    """
    if plan.archive_complete or plan.live_manifest is None:
        return replace(plan, archive_complete=True)
    if not _present_safe(plan.live):
        raise SyncError("live_changed", "Live save changed after planning.", EXIT_VALIDATION)
    current = current_manifest or _manifest(plan.live, machine_id, retries, window_seconds, validator)
    if current["root_hash"] != plan.live_manifest["root_hash"]:
        raise SyncError("live_changed", "Live save changed after planning.", EXIT_VALIDATION)
    assert plan.archive is not None
    archive_stage = plan.archive.parent / f".save-sync-archive-stage-{plan.session_id}"
    if archive_stage.exists() or archive_stage.is_symlink():
        raise SyncError("snapshot_name_collision", "Snapshot destination already exists.", EXIT_VALIDATION)
    try:
        _reject_existing_ancestors(plan.archive.parent)
        plan.archive.parent.mkdir(parents=True, exist_ok=True); _reject_unsafe(plan.archive.parent)
        _copy_verified(plan.live, archive_stage, current, machine_id, retries, window_seconds, validator)
        _reject_existing_ancestors(plan.archive.parent); _reject_existing(plan.archive)
        if plan.archive.exists() or plan.archive.is_symlink(): raise OSError("archive collision")
        os.rename(archive_stage, plan.archive); _reject_unsafe(plan.archive)
    except OSError as error:
        raise SyncError("archive_publish_failed", "Archive could not be published; live save was unchanged.", EXIT_VALIDATION) from error
    return replace(plan, archive_complete=True)


def apply_restore(plan: RestorePlan, state_root: Path, *, machine_id: str, retries: int = 1, window_seconds: float = 0, validator: Validator = validate_players) -> dict:
    source_manifest = _manifest(plan.source, machine_id, retries, window_seconds, validator)
    if source_manifest["root_hash"] != plan.source_manifest["root_hash"]:
        raise SyncError("source_changed", "Restore source changed after planning.", EXIT_VALIDATION)
    live_present = _present_safe(plan.live)
    if live_present:
        current = _manifest(plan.live, machine_id, retries, window_seconds, validator)
        if plan.live_manifest is None or current["root_hash"] != plan.live_manifest["root_hash"]:
            raise SyncError("live_changed", "Live save changed after planning.", EXIT_VALIDATION)
        if not plan.archive_complete:
            plan = archive_before_restore(plan, machine_id=machine_id, retries=retries, window_seconds=window_seconds, validator=validator, current_manifest=current)
    try:
        _reject_existing_ancestors(plan.live.parent)
        if not plan.live.parent.is_dir(): raise OSError("live parent missing")
        same = os.stat(plan.live.parent).st_dev == os.stat(plan.stage.parent).st_dev == os.stat(plan.rollback.parent).st_dev
    except OSError as error:
        raise SyncError("live_parent_unavailable", "Live save parent is unavailable.", EXIT_VALIDATION) from error
    if not same: raise SyncError("same_volume_required", "Swap staging must be on the live-save volume.", EXIT_VALIDATION)
    _copy_verified(plan.source, plan.stage, source_manifest, machine_id, retries, window_seconds, validator)
    journal = {
        "session_id": plan.session_id, "root_hash": source_manifest["root_hash"], "phase": "prepared",
        "source": str(plan.source), "live": str(plan.live), "stage": str(plan.stage),
        "rollback": str(plan.rollback), "archive": str(plan.archive) if plan.archive else None,
    }
    try: _write_journal(state_root, journal)
    except SyncError as error: raise SyncError("journal_write_failed", "Restore was not started because its journal failed.", EXIT_VALIDATION, {"session_id": plan.session_id, "phase": "prepared"}) from error
    if live_present:
        try: os.rename(plan.live, plan.rollback)
        except OSError as error: raise SyncError("live_rename_failed", "Live save could not be parked.", EXIT_VALIDATION) from error
        journal["phase"] = "live_parked"
        try: _write_journal(state_root, journal)
        except SyncError as error:
            try: os.rename(plan.rollback, plan.live)
            except OSError as rollback_error: raise _journal_failure("recovery_required", "Journal failed after parking live save.", plan.session_id, "live_parked", error) from rollback_error
            raise SyncError("journal_write_failed_after_park", "Live save was restored after journal failure.", EXIT_VALIDATION, {"session_id": plan.session_id, "phase": "live_parked"}) from error
    try: os.rename(plan.stage, plan.live)
    except OSError as error:
        if plan.rollback.exists():
            try: os.rename(plan.rollback, plan.live)
            except OSError as rollback_error: raise SyncError("recovery_required", "Swap rollback failed; recovery artifacts were retained.", EXIT_RECOVERY_REQUIRED, {"session_id": plan.session_id, "phase": "live_parked"}) from rollback_error
        raise SyncError("swap_failed_rolled_back", "Live save was restored after stage promotion failed.", EXIT_VALIDATION) from error
    journal["phase"] = "promoted"
    try: _write_journal(state_root, journal)
    except SyncError as error: raise _journal_failure("journal_write_failed_after_promote", "Promoted save requires recovery because its journal failed.", plan.session_id, "promoted", error) from error
    applied = _manifest(plan.live, machine_id, retries, window_seconds, validator)
    if applied["root_hash"] != source_manifest["root_hash"]:
        raise SyncError("recovery_required", "Promoted save verification failed; recovery artifacts were retained.", EXIT_RECOVERY_REQUIRED, {"session_id": plan.session_id, "phase": "promoted"})
    journal["phase"] = "complete"
    try: _write_journal(state_root, journal)
    except SyncError as error: raise _journal_failure("journal_write_failed_after_complete", "Completed restore requires recovery because its journal failed.", plan.session_id, "complete", error) from error
    return {"applied": True, "root_hash": applied["root_hash"], "archive_created": plan.archive is not None}


def restore_from_directory(source: Path, live: Path, archive_root: Path, state_root: Path, *, machine_id: str, apply: bool = False, retries: int = 1, window_seconds: float = 0, validator: Validator = validate_players) -> dict:
    plan = plan_restore(source, live, archive_root, state_root, machine_id=machine_id, retries=retries, window_seconds=window_seconds, validator=validator)
    if not apply:
        return {"dry_run": True, "session_id": plan.session_id, "source_root_hash": plan.source_manifest["root_hash"], "live_exists": plan.live_manifest is not None}
    return apply_restore(plan, state_root, machine_id=machine_id, retries=retries, window_seconds=window_seconds, validator=validator)
