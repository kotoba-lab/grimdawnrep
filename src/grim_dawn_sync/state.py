"""Fail-closed, atomically persisted terminal-local sync state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import stat
import re
from typing import Any
import uuid

from grim_dawn_sync.errors import EXIT_RECOVERY_REQUIRED, SyncError


STATE_SCHEMA_VERSION = "1.0.0"
STATE_KEYS = frozenset(
    {
        "schema_version",
        "last_applied_remote_commit",
        "last_applied_manifest_root_hash",
        "session_id",
        "machine_id",
        "base_commit",
        "lock_oid",
        "local_tag",
        "phase",
        "local_commit",
        "pushed_commit",
    }
)
LEGACY_STATE_KEYS = frozenset(
    {
        "schema_version",
        "last_applied_remote_commit",
        "last_applied_manifest_root_hash",
        "session_id",
    }
)
_OPTIONAL = (
    "last_applied_remote_commit",
    "last_applied_manifest_root_hash",
    "session_id",
    "machine_id",
    "base_commit",
    "lock_oid",
    "local_tag",
    "local_commit",
    "pushed_commit",
)
_PHASES = {None, "lock_held", "committed", "pushed", "release_pending"}
_SESSION_FIELDS = ("session_id", "machine_id", "base_commit", "lock_oid", "local_tag")
_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class SyncState:
    schema_version: str = STATE_SCHEMA_VERSION
    last_applied_remote_commit: str | None = None
    last_applied_manifest_root_hash: str | None = None
    session_id: str | None = None
    machine_id: str | None = None
    base_commit: str | None = None
    lock_oid: str | None = None
    local_tag: str | None = None
    phase: str | None = None
    local_commit: str | None = None
    pushed_commit: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_state(payload: dict[str, Any]) -> SyncState:
    if not isinstance(payload, dict) or payload.get("schema_version") != STATE_SCHEMA_VERSION:
        raise SyncError("invalid_state", f"state schema_version must be {STATE_SCHEMA_VERSION}.")
    # The original empty state format is accepted only for backwards-compatible
    # upgrades; all persisted session state uses the complete exact shape.
    keys = frozenset(payload)
    if keys not in (STATE_KEYS, LEGACY_STATE_KEYS):
        raise SyncError("invalid_state", "State must contain exactly the required schema fields.")
    values = {key: payload.get(key) for key in _OPTIONAL}
    if any(value is not None and (not isinstance(value, str) or not value) for value in values.values()):
        raise SyncError("invalid_state", "Optional state identifiers must be non-empty strings or null.")
    phase = payload.get("phase")
    if phase not in _PHASES:
        raise SyncError("invalid_state", "State phase is invalid.")
    if keys == STATE_KEYS:
        _validate_current_transition(phase, values)
    elif values["session_id"] is not None:
        raise SyncError("invalid_state", "Legacy state may not contain an active session.")
    if values["last_applied_remote_commit"] is not None and not _OID.fullmatch(values["last_applied_remote_commit"]):
        raise SyncError("invalid_state", "Last applied commit is invalid.")
    if values["last_applied_manifest_root_hash"] is not None and not _HASH.fullmatch(values["last_applied_manifest_root_hash"]):
        raise SyncError("invalid_state", "Last applied manifest hash is invalid.")
    return SyncState(phase=phase, **values)


def _validate_current_transition(phase: str | None, values: dict[str, str | None]) -> None:
    active = [values[key] for key in _SESSION_FIELDS]
    if phase is None:
        if any(active) or values["local_commit"] is not None or values["pushed_commit"] is not None:
            raise SyncError("invalid_state", "Inactive state must not contain recovery session fields.")
        return
    if not all(active):
        raise SyncError("invalid_state", "Active recovery state is missing session identifiers.")
    try:
        session_uuid = uuid.UUID(values["session_id"] or "")
        if str(session_uuid) != values["session_id"]:
            raise ValueError
    except ValueError as error:
        raise SyncError("invalid_state", "Active session UUID is invalid.") from error
    if not _TOKEN.fullmatch(values["machine_id"] or ""):
        raise SyncError("invalid_state", "Active machine identifier is invalid.")
    if not _OID.fullmatch(values["base_commit"] or "") or not _OID.fullmatch(values["lock_oid"] or ""):
        raise SyncError("invalid_state", "Active object identifiers are invalid.")
    if values["local_tag"] != f"grim-dawn-sync-{values['session_id']}":
        raise SyncError("invalid_state", "Local tag does not match the active session.")
    local_commit = values["local_commit"]
    pushed_commit = values["pushed_commit"]
    for commit in (local_commit, pushed_commit):
        if commit is not None and not _OID.fullmatch(commit):
            raise SyncError("invalid_state", "Recovery commit identifier is invalid.")
    if phase == "lock_held" and (local_commit is not None or pushed_commit is not None):
        raise SyncError("invalid_state", "Lock-held state must not contain commit results.")
    if phase == "committed" and (local_commit is None or pushed_commit is not None):
        raise SyncError("invalid_state", "Committed state must contain only its local commit.")
    if phase == "pushed" and (local_commit is None or pushed_commit is None or local_commit != pushed_commit):
        raise SyncError("invalid_state", "Pushed state must contain one matching local and remote commit.")
    if phase == "release_pending" and (
        pushed_commit is None or (local_commit is not None and local_commit != pushed_commit)
    ):
        raise SyncError("invalid_state", "Release-pending state must identify the pushed commit.")


def _is_reparse(result: os.stat_result) -> bool:
    attributes = getattr(result, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _unsafe_state_path(message: str) -> SyncError:
    return SyncError("unsafe_state_path", message, EXIT_RECOVERY_REQUIRED)


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _check_existing_ancestors(path: Path) -> None:
    """Reject symlink/reparse ancestors before mkdir or file access."""
    pending: list[Path] = []
    current = path
    while True:
        pending.append(current)
        if current.parent == current:
            break
        current = current.parent
    for candidate in reversed(pending):
        result = _lstat(candidate)
        if result is None:
            continue
        if candidate.is_symlink() or _is_reparse(result):
            raise _unsafe_state_path("Recovery state path traverses a reparse point.")
        if candidate != path and not stat.S_ISDIR(result.st_mode):
            raise _unsafe_state_path("Recovery state path has a non-directory ancestor.")


def _check_parent(path: Path) -> None:
    result = _lstat(path.parent)
    if result is None or path.parent.is_symlink() or _is_reparse(result) or not stat.S_ISDIR(result.st_mode):
        raise _unsafe_state_path("Recovery state parent is not a safe directory.")


def _check_destination(path: Path, *, missing_ok: bool) -> os.stat_result | None:
    result = _lstat(path)
    if result is None:
        if missing_ok:
            return None
        raise FileNotFoundError(path)
    if path.is_symlink() or _is_reparse(result) or not stat.S_ISREG(result.st_mode):
        raise _unsafe_state_path("Recovery state is not a regular non-reparse file.")
    return result


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def load_state(path: Path) -> SyncState:
    try:
        _check_existing_ancestors(path.parent)
        if _lstat(path.parent) is None:
            raise FileNotFoundError(path)
        _check_parent(path)
        before = _check_destination(path, missing_ok=False)
        with path.open("r", encoding="utf-8", newline="") as handle:
            opened = os.fstat(handle.fileno())
            if _is_reparse(opened) or not stat.S_ISREG(opened.st_mode):
                raise _unsafe_state_path("Recovery state changed before it was opened.")
            raw = handle.read()
        after = _check_destination(path, missing_ok=False)
        identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if before is None or after is None or any(
            getattr(before, field, None) != getattr(opened, field, None)
            or getattr(opened, field, None) != getattr(after, field, None)
            for field in identity
        ):
            raise _unsafe_state_path("Recovery state changed while it was read.")
        payload = json.loads(raw, object_pairs_hook=_object_without_duplicate_keys)
        return parse_state(payload)
    except FileNotFoundError as error:
        raise SyncError("state_missing", "Recovery state is missing.", EXIT_RECOVERY_REQUIRED) from error
    except SyncError as error:
        raise SyncError(
            "state_corrupt",
            "Recovery state is unreadable, unsafe, or corrupt.",
            EXIT_RECOVERY_REQUIRED,
        ) from error
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        raise SyncError(
            "state_corrupt",
            "Recovery state is unreadable, unsafe, or corrupt.",
            EXIT_RECOVERY_REQUIRED,
        ) from error


def save_state(path: Path, state: SyncState) -> None:
    """Write a complete state file using a same-directory atomic replacement."""
    parse_state(state.as_dict())
    temporary: Path | None = None
    try:
        _check_existing_ancestors(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        _check_parent(path)
        _check_destination(path, missing_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(state.as_dict(), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush(); os.fsync(handle.fileno())
        _check_parent(path)
        _check_destination(temporary, missing_ok=False)
        _check_destination(path, missing_ok=True)
        os.replace(temporary, path)
        temporary = None
    except SyncError:
        raise
    except OSError as error:
        raise SyncError(
            "state_write_failed",
            "Recovery state could not be persisted.",
            EXIT_RECOVERY_REQUIRED,
        ) from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
