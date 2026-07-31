"""Managed, immutable save bookmarks backed by annotated Git tags."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import unicodedata
from typing import Any, Callable
import uuid
from pathlib import Path

from grim_dawn_sync.errors import EXIT_RECOVERY_REQUIRED, EXIT_VALIDATION, SyncError
from grim_dawn_sync.git_vault import GitVault
from grim_dawn_sync.manifest import stable_manifest
from grim_dawn_sync.session_lock import acquire_lock, inspect_remote_lock_readonly, release_bookmark_lock
from grim_dawn_sync.state import SyncState, load_state, save_state


BOOKMARK_SCHEMA_VERSION = "1.0.0"
BOOKMARK_KIND = "grim_dawn_save_bookmark"
_CREATOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class ManagedBookmark:
    ref: str
    commit: str
    display_name: str
    note: str | None
    created_at: str
    created_by: str


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _text(value: object, *, label: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise SyncError("invalid_bookmark_annotation", f"Bookmark {label} is invalid.", EXIT_VALIDATION)
    if any(ord(char) == 0 or unicodedata.category(char).startswith("C") for char in value):
        raise SyncError("invalid_bookmark_annotation", f"Bookmark {label} is invalid.", EXIT_VALIDATION)
    return value


def parse_bookmark_annotation(raw: str) -> dict[str, str | None]:
    """Parse an exact, display-safe annotation payload without coercion."""
    try:
        payload = json.loads(raw, object_pairs_hook=_no_duplicates)
    except (TypeError, ValueError, json.JSONDecodeError, UnicodeError) as error:
        raise SyncError("invalid_bookmark_annotation", "Bookmark annotation is invalid.", EXIT_VALIDATION) from error
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "kind", "display_name", "note", "created_at", "created_by"}:
        raise SyncError("invalid_bookmark_annotation", "Bookmark annotation is invalid.", EXIT_VALIDATION)
    if payload["schema_version"] != BOOKMARK_SCHEMA_VERSION or payload["kind"] != BOOKMARK_KIND:
        raise SyncError("invalid_bookmark_annotation", "Bookmark annotation is invalid.", EXIT_VALIDATION)
    display_name = _text(payload["display_name"], label="display name", minimum=1, maximum=80)
    note_value = payload["note"]
    note = None if note_value is None else _text(note_value, label="note", minimum=0, maximum=500)
    created_at = _text(payload["created_at"], label="timestamp", minimum=20, maximum=40)
    created_by = payload["created_by"]
    if not isinstance(created_by, str) or not _CREATOR.fullmatch(created_by):
        raise SyncError("invalid_bookmark_annotation", "Bookmark annotation is invalid.", EXIT_VALIDATION)
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise SyncError("invalid_bookmark_annotation", "Bookmark annotation is invalid.", EXIT_VALIDATION) from error
    if parsed.tzinfo is None:
        raise SyncError("invalid_bookmark_annotation", "Bookmark annotation is invalid.", EXIT_VALIDATION)
    return {"display_name": display_name, "note": note, "created_at": created_at, "created_by": created_by}


def make_bookmark_annotation(display_name: str, note: str | None, *, created_by: str) -> str:
    validated = parse_bookmark_annotation(json.dumps({
        "schema_version": BOOKMARK_SCHEMA_VERSION, "kind": BOOKMARK_KIND,
        "display_name": display_name, "note": note,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "created_by": created_by,
    }, ensure_ascii=False))
    return json.dumps({
        "schema_version": BOOKMARK_SCHEMA_VERSION, "kind": BOOKMARK_KIND,
        **validated,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def create_bookmark(vault: GitVault, commit: str, *, display_name: str, note: str | None, created_by: str) -> ManagedBookmark:
    annotation = make_bookmark_annotation(display_name, note, created_by=created_by)
    ref, target, remote_annotation = vault.create_managed_bookmark(commit, annotation)
    metadata = parse_bookmark_annotation(remote_annotation)
    if metadata != parse_bookmark_annotation(annotation):
        raise SyncError("bookmark_remote_mismatch", "Remote bookmark annotation could not be verified.", EXIT_VALIDATION)
    return ManagedBookmark(ref, target, str(metadata["display_name"]), metadata["note"], str(metadata["created_at"]), str(metadata["created_by"]))


def create_live_bookmark(vault: GitVault, live_root: Path, manifest: dict, *, expected_remote_head: str,
                         display_name: str, note: str | None, created_by: str,
                         retries: int = 1, window_seconds: float = 0, validator,
                         before_publish: Callable[[], None] | None = None,
                         expected_lock_oid: str | None = None,
                         publication_intent: Callable[[str, str, str], None] | None = None,
                         publication_confirmed: Callable[[str, str, str], None] | None = None) -> ManagedBookmark:
    """Create and publish an immutable detached snapshot of current live data."""
    annotation = make_bookmark_annotation(display_name, note, created_by=created_by)
    commit = vault.create_detached_snapshot(
        live_root, machine_id=created_by, session_id=f"bookmark-{uuid.uuid4().hex}",
        expected_manifest=manifest, expected_remote_head=expected_remote_head,
        retries=retries, window_seconds=window_seconds, validator=validator,
    )
    if before_publish is not None:
        before_publish()
    ref, target, remote_annotation = vault.create_managed_bookmark(
        commit, annotation, detached_root_hash=str(manifest["root_hash"]), expected_remote_head=expected_remote_head,
        expected_lock_oid=expected_lock_oid, publication_guard=before_publish,
        publication_intent=publication_intent, publication_confirmed=publication_confirmed,
    )
    metadata = parse_bookmark_annotation(remote_annotation)
    if metadata != parse_bookmark_annotation(annotation):
        raise SyncError("bookmark_remote_mismatch", "Remote bookmark annotation could not be verified.", EXIT_VALIDATION)
    return ManagedBookmark(ref, target, str(metadata["display_name"]), metadata["note"], str(metadata["created_at"]), str(metadata["created_by"]))


def create_live_bookmark_locked(
    vault: GitVault,
    live_root: Path,
    manifest: dict,
    *,
    state_path: Path,
    expected_remote_head: str,
    display_name: str,
    note: str | None,
    created_by: str,
    retries: int = 1,
    window_seconds: float = 0,
    validator,
    before_lock: Callable[[], None] | None = None,
    after_lock: Callable[[], None] | None = None,
) -> ManagedBookmark:
    """Publish a detached live snapshot under the normal cross-process lock."""
    original = load_state(state_path)
    if (
        original.phase is not None
        or original.machine_id != created_by
        or original.last_applied_remote_commit is None
        or original.last_applied_manifest_root_hash is None
    ):
        raise SyncError("bookmark_state_invalid", "Live bookmark requires an inactive enrolled baseline.", EXIT_VALIDATION)

    expected_root = str(manifest.get("root_hash", ""))

    def assert_current() -> None:
        vault.assert_remote_identity()
        if vault.remote_oid() != expected_remote_head:
            raise SyncError("selection_stale", "Remote main changed during live bookmark creation.", EXIT_VALIDATION)
        observed = stable_manifest(
            live_root, machine_id=created_by, retries=retries, window_seconds=window_seconds,
        )
        if observed.get("root_hash") != expected_root or not validator(live_root, observed).get("ok"):
            raise SyncError("source_changed", "Live save changed during bookmark creation.", EXIT_VALIDATION)

    if before_lock is not None:
        before_lock()
    assert_current()
    lock = acquire_lock(vault, created_by, expected_remote_head, state_path=state_path)
    try:
        expected_lock_state = SyncState(
            session_id=lock.session.session_id,
            machine_id=lock.session.machine_id,
            base_commit=lock.session.base_commit,
            lock_oid=lock.oid,
            local_tag=lock.local_tag,
            phase="lock_held",
        )
        expected_publication_state = [expected_lock_state]

        def before_publish() -> None:
            if after_lock is not None:
                after_lock()
            assert_current()
            remote_lock = inspect_remote_lock_readonly(vault)
            if remote_lock is None or remote_lock.oid != lock.oid or remote_lock.session != lock.session:
                raise SyncError("bookmark_lock_changed", "Bookmark session lock ownership changed before publication.", EXIT_RECOVERY_REQUIRED)
            if load_state(state_path) != expected_publication_state[0]:
                raise SyncError("bookmark_state_changed", "Bookmark recovery state changed before publication.", EXIT_RECOVERY_REQUIRED)

        def publication_intent(ref: str, tag_oid: str, commit: str) -> None:
            before_publish()
            pending = SyncState(
                last_applied_remote_commit=original.last_applied_remote_commit,
                last_applied_manifest_root_hash=original.last_applied_manifest_root_hash,
                session_id=lock.session.session_id, machine_id=lock.session.machine_id,
                base_commit=lock.session.base_commit, lock_oid=lock.oid, local_tag=lock.local_tag,
                phase="bookmark_publish_pending", local_commit=commit,
                bookmark_ref=ref, bookmark_tag_oid=tag_oid, bookmark_root_hash=expected_root,
            )
            save_state(state_path, pending)
            expected_publication_state[0] = pending

        def publication_confirmed(ref: str, tag_oid: str, commit: str) -> None:
            before_publish()
            current = expected_publication_state[0]
            if current.bookmark_ref != ref or current.bookmark_tag_oid != tag_oid or current.local_commit != commit:
                raise SyncError("bookmark_state_changed", "Bookmark publication result did not match its intent.", EXIT_RECOVERY_REQUIRED)
            confirmed = SyncState(
                last_applied_remote_commit=original.last_applied_remote_commit,
                last_applied_manifest_root_hash=original.last_applied_manifest_root_hash,
                session_id=lock.session.session_id, machine_id=lock.session.machine_id,
                base_commit=lock.session.base_commit, lock_oid=lock.oid, local_tag=lock.local_tag,
                phase="bookmark_release_pending", pushed_commit=lock.session.base_commit,
                bookmark_ref=ref, bookmark_tag_oid=tag_oid, bookmark_root_hash=expected_root,
            )
            save_state(state_path, confirmed)
            expected_publication_state[0] = confirmed

        if after_lock is not None:
            after_lock()
        assert_current()
        result = create_live_bookmark(
            vault,
            live_root,
            manifest,
            expected_remote_head=expected_remote_head,
            display_name=display_name,
            note=note,
            created_by=created_by,
            retries=retries,
            window_seconds=window_seconds,
            validator=validator,
            before_publish=before_publish,
            expected_lock_oid=lock.oid,
            publication_intent=publication_intent,
            publication_confirmed=publication_confirmed,
        )
        if after_lock is not None:
            after_lock()
        assert_current()
        release_bookmark_lock(vault, lock, original, state_path=state_path)
        return result
    except SyncError as error:
        if error.exit_code == EXIT_RECOVERY_REQUIRED:
            raise
        raise SyncError(
            error.code,
            f"{error.message} The bookmark lock was retained; run recover.",
            EXIT_RECOVERY_REQUIRED,
            {**error.details, "session_id": lock.session.session_id},
        ) from error
    except Exception as error:
        raise SyncError(
            "bookmark_incomplete",
            "Live bookmark creation was interrupted; the bookmark lock was retained. Run recover.",
            EXIT_RECOVERY_REQUIRED,
            {"session_id": lock.session.session_id},
        ) from error
