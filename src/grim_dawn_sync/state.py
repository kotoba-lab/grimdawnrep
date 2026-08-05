"""Fail-closed, atomically persisted terminal-local sync state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import re
from typing import Any
import uuid

from grim_dawn_sync.errors import EXIT_CONFLICT, EXIT_RECOVERY_REQUIRED, SyncError


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
        "bootstrap_live_applied",
        "bookmark_ref",
        "bookmark_tag_oid",
        "bookmark_root_hash",
    }
)
PRE_BOOKMARK_INTENT_STATE_KEYS = STATE_KEYS - {"bookmark_ref", "bookmark_tag_oid", "bookmark_root_hash"}
PRE_LIVE_MARKER_STATE_KEYS = STATE_KEYS - {"bootstrap_live_applied"}
PRE_BOOTSTRAP_STATE_KEYS = PRE_BOOKMARK_INTENT_STATE_KEYS - {"bootstrap_live_applied"}
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
    "bookmark_ref",
    "bookmark_tag_oid",
    "bookmark_root_hash",
)
_PHASES = {None, "bootstrap_pending", "lock_held", "committed", "pushed", "release_pending", "bookmark_publish_pending", "bookmark_release_pending", "unpublished_release_pending"}
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
    bootstrap_live_applied: bool = False
    bookmark_ref: str | None = None
    bookmark_tag_oid: str | None = None
    bookmark_root_hash: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_state(payload: dict[str, Any]) -> SyncState:
    if not isinstance(payload, dict) or payload.get("schema_version") != STATE_SCHEMA_VERSION:
        raise SyncError("invalid_state", f"state schema_version must be {STATE_SCHEMA_VERSION}.")
    # The original empty state format is accepted only for backwards-compatible
    # upgrades; all persisted session state uses the complete exact shape.
    keys = frozenset(payload)
    if keys not in (STATE_KEYS, PRE_BOOKMARK_INTENT_STATE_KEYS, PRE_LIVE_MARKER_STATE_KEYS, PRE_BOOTSTRAP_STATE_KEYS, LEGACY_STATE_KEYS):
        raise SyncError("invalid_state", "State must contain exactly the required schema fields.")
    values = {key: payload.get(key) for key in _OPTIONAL}
    if any(value is not None and (not isinstance(value, str) or not value) for value in values.values()):
        raise SyncError("invalid_state", "Optional state identifiers must be non-empty strings or null.")
    phase = payload.get("phase")
    bootstrap_live_applied = payload.get("bootstrap_live_applied", False)
    if not isinstance(bootstrap_live_applied, bool):
        raise SyncError("invalid_state", "Bootstrap live-applied marker must be boolean.")
    if phase not in _PHASES:
        raise SyncError("invalid_state", "State phase is invalid.")
    if keys in (STATE_KEYS, PRE_BOOKMARK_INTENT_STATE_KEYS, PRE_LIVE_MARKER_STATE_KEYS, PRE_BOOTSTRAP_STATE_KEYS):
        _validate_current_transition(phase, values, bootstrap_live_applied)
    elif values["session_id"] is not None:
        raise SyncError("invalid_state", "Legacy state may not contain an active session.")
    if values["last_applied_remote_commit"] is not None and not _OID.fullmatch(values["last_applied_remote_commit"]):
        raise SyncError("invalid_state", "Last applied commit is invalid.")
    if values["last_applied_manifest_root_hash"] is not None and not _HASH.fullmatch(values["last_applied_manifest_root_hash"]):
        raise SyncError("invalid_state", "Last applied manifest hash is invalid.")
    return SyncState(
        phase=phase,
        bootstrap_live_applied=bootstrap_live_applied,
        **values,
    )


def _validate_current_transition(
    phase: str | None,
    values: dict[str, str | None],
    bootstrap_live_applied: bool,
) -> None:
    active = [values[key] for key in _SESSION_FIELDS]
    if phase is None:
        if (
            any(values[key] for key in _SESSION_FIELDS if key != "machine_id")
            or values["local_commit"] is not None
            or values["pushed_commit"] is not None
            or bootstrap_live_applied
            or any(values[key] is not None for key in ("bookmark_ref", "bookmark_tag_oid", "bookmark_root_hash"))
        ):
            raise SyncError("invalid_state", "Inactive state must not contain recovery session fields.")
        if values["machine_id"] is not None and not _TOKEN.fullmatch(values["machine_id"]):
            raise SyncError("invalid_state", "Inactive state machine identifier is invalid.")
        if values["machine_id"] is not None and (
            values["last_applied_remote_commit"] is None
            or values["last_applied_manifest_root_hash"] is None
        ):
            raise SyncError("invalid_state", "Inactive machine baseline requires commit and root hash.")
        return
    if phase == "bootstrap_pending":
        if (
            values["machine_id"] is None
            or values["local_commit"] is None
            or values["last_applied_manifest_root_hash"] is None
            or values["last_applied_remote_commit"] is not None
            or values["session_id"] is not None
            or values["base_commit"] is not None
            or values["lock_oid"] is not None
            or values["local_tag"] is not None
            or values["pushed_commit"] is not None
        ):
            raise SyncError(
                "invalid_state",
                "Bootstrap-pending state must contain only its machine, local commit, and root hash.",
            )
        if not _TOKEN.fullmatch(values["machine_id"]) or not _OID.fullmatch(values["local_commit"]):
            raise SyncError("invalid_state", "Bootstrap-pending identifiers are invalid.")
        return
    if bootstrap_live_applied:
        raise SyncError("invalid_state", "Only bootstrap-pending state may contain a live-applied marker.")
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
    if phase == "bookmark_release_pending" and (
        local_commit is not None or pushed_commit != values["base_commit"]
        or ((values["last_applied_remote_commit"] is None) != (values["last_applied_manifest_root_hash"] is None))
    ):
        raise SyncError("invalid_state", "Bookmark release state must preserve the exact prior baseline and unchanged main.")
    if phase == "unpublished_release_pending" and (
        local_commit is not None or pushed_commit != values["base_commit"]
        or ((values["last_applied_remote_commit"] is None) != (values["last_applied_manifest_root_hash"] is None))
    ):
        raise SyncError("invalid_state", "Unpublished release state must preserve the exact prior baseline and unchanged main.")
    bookmark_fields = (values["bookmark_ref"], values["bookmark_tag_oid"], values["bookmark_root_hash"])
    if phase == "bookmark_publish_pending":
        if (
            local_commit is None or pushed_commit is not None
            or values["last_applied_remote_commit"] is None or values["last_applied_manifest_root_hash"] is None
            or not all(bookmark_fields)
            or not re.fullmatch(r"grim-dawn-save-[0-9a-f]{32}", values["bookmark_ref"] or "")
            or not _OID.fullmatch(values["bookmark_tag_oid"] or "")
            or not _HASH.fullmatch(values["bookmark_root_hash"] or "")
        ):
            raise SyncError("invalid_state", "Bookmark publish state must contain one exact managed tag intent.")
    elif phase == "bookmark_release_pending":
        if any(bookmark_fields) and (
            not all(bookmark_fields)
            or not re.fullmatch(r"grim-dawn-save-[0-9a-f]{32}", values["bookmark_ref"] or "")
            or not _OID.fullmatch(values["bookmark_tag_oid"] or "")
            or not _HASH.fullmatch(values["bookmark_root_hash"] or "")
        ):
            raise SyncError("invalid_state", "Bookmark release state has an incomplete managed tag result.")
    elif any(bookmark_fields):
        raise SyncError("invalid_state", "Only bookmark publication states may contain managed tag fields.")


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


@contextmanager
def _state_write_lock(path: Path):
    """Serialize every cooperating state writer with an OS-released lock."""
    _check_existing_ancestors(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _check_parent(path)
    lock_path = path.with_name(f".{path.name}.lock")
    _check_destination(lock_path, missing_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    locked = False
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        opened = os.fstat(descriptor)
        if _is_reparse(opened) or not stat.S_ISREG(opened.st_mode):
            raise _unsafe_state_path("Recovery state lock is not a safe regular file.")
        if opened.st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (OSError, BlockingIOError) as error:
            raise SyncError("state_busy", "Recovery state is being updated by another process.", EXIT_CONFLICT) from error
        yield
    except SyncError:
        raise
    except OSError as error:
        raise SyncError("state_write_failed", "Recovery state could not be locked.", EXIT_RECOVERY_REQUIRED) from error
    finally:
        if descriptor is not None:
            if locked:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(descriptor)


def _save_state_unlocked(path: Path, state: SyncState) -> None:
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


def save_state(path: Path, state: SyncState) -> None:
    """Write a complete state file using a locked same-directory replacement."""
    parse_state(state.as_dict())
    with _state_write_lock(path):
        _save_state_unlocked(path, state)


def save_state_if_unchanged(path: Path, expected: SyncState, state: SyncState) -> None:
    """Atomically compare the canonical pre-state and replace it under one lock.

    A missing file is the canonical empty pre-state.  This permits the first
    session intent to use the same semantic CAS as every later transition.
    """
    parse_state(expected.as_dict())
    parse_state(state.as_dict())
    with _state_write_lock(path):
        current = SyncState() if _lstat(path) is None else load_state(path)
        if current != expected:
            raise SyncError("selection_stale", "Recovery state changed before lock acquisition.", EXIT_CONFLICT)
        _save_state_unlocked(path, state)
