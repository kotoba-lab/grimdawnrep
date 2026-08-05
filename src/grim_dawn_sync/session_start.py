"""Session-start local snapshot: verified, tool-owned pre-launch archives.

A session-start archive preserves the exact live save as it existed the
moment a launch acquired its lock, before any restore, launch, or promotion
touches it.  It is created through the same two-stage (incomplete-name
staging -> atomic rename) publish pattern as ``preserve``/``archive_after_game``
so a crash never leaves a half-written directory visible as a candidate.

This module intentionally has no Git, lock, or workflow knowledge.  It only
knows how to publish, read back, and safely enumerate these archives.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid
from typing import Callable

from grim_dawn_sync.errors import EXIT_VALIDATION, SyncError
from grim_dawn_sync.manifest import (
    _reject_unsafe,
    build_manifest,
    is_character_player_path,
    validate_manifest_path,
)
from grim_dawn_sync.snapshot import _copy_verified, _reject_existing_ancestors, _safe_target, _safe_tree, Validator


SESSION_START_SCHEMA_VERSION = "1.0.0"
SESSION_START_KIND = "grim_dawn_session_start_snapshot"
SESSION_START_METADATA_NAME = ".session-start.json"

# Distinct prefix from replace-before archives (``save-before-*``) so the two
# kinds of local archive are never confused by prefix alone.
SESSION_START_ID_PATTERN = re.compile(r"^save-session-start-[0-9a-f]{16}-[0-9a-f]{32}$")
_INCOMPLETE_PATTERN = re.compile(r"^\.session-start-incomplete-[0-9a-f]{32}$")

_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_KNOWN_CANDIDATE_KINDS = {"live", "remote_head", "history", "bookmark", "legacy", "session_start"}
_REQUIRED_METADATA_KEYS = {
    "schema_version", "kind", "created_at", "root_hash", "machine_id", "session_id",
    "launched_from_candidate_kind",
}


def session_start_archive_id(root_hash: str) -> str:
    if not isinstance(root_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", root_hash):
        raise SyncError("invalid_manifest_root", "Session-start archive requires a valid root hash.", EXIT_VALIDATION)
    return f"save-session-start-{root_hash[:16]}-{uuid.uuid4().hex}"


def _validate_session_start_payload(payload: object) -> dict:
    if not isinstance(payload, dict) or set(payload) != _REQUIRED_METADATA_KEYS:
        raise SyncError("invalid_session_start_metadata", "Session-start metadata is not canonical.", EXIT_VALIDATION)
    if payload.get("schema_version") != SESSION_START_SCHEMA_VERSION or payload.get("kind") != SESSION_START_KIND:
        raise SyncError("invalid_session_start_metadata", "Session-start metadata has an unexpected schema or kind.", EXIT_VALIDATION)
    root_hash, machine_id, session_id, candidate_kind, created_at = (
        payload.get("root_hash"), payload.get("machine_id"), payload.get("session_id"),
        payload.get("launched_from_candidate_kind"), payload.get("created_at"),
    )
    if not isinstance(root_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", root_hash):
        raise SyncError("invalid_session_start_metadata", "Session-start metadata root hash is invalid.", EXIT_VALIDATION)
    if not isinstance(machine_id, str) or not _MACHINE_ID_RE.fullmatch(machine_id):
        raise SyncError("invalid_session_start_metadata", "Session-start metadata machine ID is invalid.", EXIT_VALIDATION)
    if not isinstance(session_id, str):
        raise SyncError("invalid_session_start_metadata", "Session-start metadata session ID is invalid.", EXIT_VALIDATION)
    try:
        parsed_uuid = uuid.UUID(session_id)
        if str(parsed_uuid) != session_id:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as error:
        raise SyncError("invalid_session_start_metadata", "Session-start metadata session ID is invalid.", EXIT_VALIDATION) from error
    if not isinstance(candidate_kind, str) or candidate_kind not in _KNOWN_CANDIDATE_KINDS:
        raise SyncError("invalid_session_start_metadata", "Session-start metadata candidate kind is invalid.", EXIT_VALIDATION)
    if not isinstance(created_at, str):
        raise SyncError("invalid_session_start_metadata", "Session-start metadata timestamp is invalid.", EXIT_VALIDATION)
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None or not created_at.endswith("Z"):
            raise ValueError
    except ValueError as error:
        raise SyncError("invalid_session_start_metadata", "Session-start metadata timestamp is invalid.", EXIT_VALIDATION) from error
    return payload


def write_session_start_metadata(stage: Path, *, root_hash: str, machine_id: str, session_id: str,
                                  launched_from_candidate_kind: str, now: datetime | None = None) -> None:
    """Write the sidecar metadata into a not-yet-published staging directory."""
    payload = {
        "schema_version": SESSION_START_SCHEMA_VERSION,
        "kind": SESSION_START_KIND,
        "created_at": (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "root_hash": root_hash,
        "machine_id": machine_id,
        "session_id": session_id,
        "launched_from_candidate_kind": launched_from_candidate_kind,
    }
    # A bug here must never publish a sidecar that would fail its own read-back check.
    _validate_session_start_payload(payload)
    _reject_unsafe(stage)
    target = stage / SESSION_START_METADATA_NAME
    if target.exists() or target.is_symlink():
        raise SyncError("snapshot_name_collision", "Session-start metadata already exists.", EXIT_VALIDATION)
    temporary = stage / f".session-start-{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, target)
    except OSError as error:
        raise SyncError("archive_publish_failed", "Session-start metadata could not be written.", EXIT_VALIDATION) from error


def read_session_start_metadata(archive: Path) -> dict:
    target = archive / SESSION_START_METADATA_NAME
    try:
        _reject_unsafe(target)
    except SyncError as error:
        raise SyncError("invalid_session_start_metadata", "Session-start metadata could not be read.", EXIT_VALIDATION) from error
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as error:
        raise SyncError("invalid_session_start_metadata", "Session-start metadata could not be read.", EXIT_VALIDATION) from error
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise SyncError("invalid_session_start_metadata", "Session-start metadata is not valid JSON.", EXIT_VALIDATION) from error
    return _validate_session_start_payload(payload)


def session_start_archive_manifest(archive: Path, *, machine_id: str) -> dict:
    """Rescan an archive's save tree, excluding the metadata sidecar itself."""
    raw = build_manifest(archive, machine_id=machine_id)
    files = [item for item in raw["files"] if item["path"] != SESSION_START_METADATA_NAME]
    canonical = "\n".join(f"{item['path']}\0{item['size']}\0{item['sha256']}" for item in files).encode()
    root_hash = hashlib.sha256(canonical).hexdigest()
    character_count = sum(1 for item in files if is_character_player_path(item["path"]))
    return {
        **raw, "files": files, "root_hash": root_hash, "file_count": len(files),
        "total_bytes": sum(item["size"] for item in files), "character_count": character_count,
    }


def _integrity_only(_root: Path, _manifest: dict) -> dict:
    """No player/gameplay validation: session-start only guarantees byte-for-byte integrity.

    Player-format validation belongs to the normal validate/quarantine path
    later in the workflow.  Refusing to preserve the exact pre-launch live
    save just because it fails that stricter check would defeat the point of
    a safety-net snapshot, so this stage only ever checks hash integrity
    (already enforced by ``_copy_verified`` itself).
    """
    return {"ok": True}


def create_session_start_archive(archives_root: Path, live_root: Path, *, manifest: dict, machine_id: str,
                                  session_id: str, launched_from_candidate_kind: str,
                                  retries: int = 1, window_seconds: float = 0,
                                  validator: Validator = _integrity_only,
                                  now: datetime | None = None) -> str:
    """Publish a verified session-start archive; fail closed, never partially visible."""
    _reject_existing_ancestors(archives_root)
    try:
        archives_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SyncError("snapshot_root_unavailable", "Local archive root could not be created.", EXIT_VALIDATION) from error
    _reject_unsafe(archives_root)
    archive_id = session_start_archive_id(manifest["root_hash"])
    destination = archives_root / archive_id
    stage = archives_root / f".session-start-incomplete-{uuid.uuid4().hex}"
    if destination.exists() or destination.is_symlink() or stage.exists() or stage.is_symlink():
        raise SyncError("snapshot_name_collision", "Session-start archive destination already exists.", EXIT_VALIDATION)
    _copy_verified(live_root, stage, manifest, machine_id, retries, window_seconds, validator)
    write_session_start_metadata(
        stage, root_hash=manifest["root_hash"], machine_id=machine_id, session_id=session_id,
        launched_from_candidate_kind=launched_from_candidate_kind, now=now,
    )
    try:
        stage_info = stage.lstat()
    except OSError as error:
        raise SyncError("archive_publish_failed", "Session-start archive could not be inspected.", EXIT_VALIDATION) from error
    if (
        stage.is_symlink()
        or bool(getattr(stage_info, "st_file_attributes", 0) & 0x400)
        or not stat.S_ISDIR(stage_info.st_mode)
    ):
        raise SyncError("unsafe_archive_path", "Session-start staging is not a safe directory.", EXIT_VALIDATION)
    if destination.parent != archives_root or destination.exists() or destination.is_symlink():
        raise SyncError("snapshot_name_collision", "Session-start archive destination already exists.", EXIT_VALIDATION)
    try:
        os.replace(stage, destination)
    except OSError as error:
        raise SyncError("archive_publish_failed", "Session-start archive could not be published.", EXIT_VALIDATION) from error
    return archive_id


def extract_session_start_archive(archive: Path, destination: Path, *, expected_manifest: dict, machine_id: str) -> None:
    """Copy an archive's save tree (never its metadata sidecar) into a fresh staging dir."""
    if destination.exists() or destination.is_symlink():
        raise SyncError("snapshot_name_collision", "Session-start extraction destination already exists.", EXIT_VALIDATION)
    try:
        _reject_existing_ancestors(destination.parent)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _reject_unsafe(destination.parent)
        source_dirs, entries = _safe_tree(archive)
        destination.mkdir()
        for relative in source_dirs:
            target = _safe_target(destination, relative)
            target.mkdir()
        for entry in entries:
            relative = validate_manifest_path(entry.relative_to(archive).as_posix())
            if relative == SESSION_START_METADATA_NAME:
                continue
            target = _safe_target(destination, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target, follow_symlinks=False)
    except OSError as error:
        raise SyncError("snapshot_copy_failed", "Session-start archive could not be extracted safely.", EXIT_VALIDATION) from error
    actual = build_manifest(destination, machine_id=machine_id)
    if actual["root_hash"] != expected_manifest["root_hash"]:
        raise SyncError("snapshot_hash_mismatch", "Extracted session-start archive did not match its verified manifest.", EXIT_VALIDATION)


def scan_session_start_archives(archives_root: Path, *, machine_id: str) -> list[tuple[str, dict, dict]]:
    """Enumerate verified session-start archives; silently skip anything unsafe or invalid.

    Returns ``(archive_id, manifest, metadata)`` tuples.  A single corrupt or
    tampered archive must never prevent every other candidate from loading, so
    validation failures here are skip-worthy, not fatal.
    """
    results: list[tuple[str, dict, dict]] = []
    try:
        if not archives_root.is_dir() or archives_root.is_symlink():
            return []
        _reject_unsafe(archives_root)
        entries = sorted(archives_root.iterdir(), key=lambda item: item.name)
    except (OSError, SyncError):
        return []
    for entry in entries:
        if not SESSION_START_ID_PATTERN.fullmatch(entry.name):
            continue
        try:
            _reject_unsafe(entry)
            if not entry.is_dir():
                continue
            metadata = read_session_start_metadata(entry)
            manifest = session_start_archive_manifest(entry, machine_id=machine_id)
            if manifest["root_hash"] != metadata["root_hash"]:
                continue
            results.append((entry.name, manifest, metadata))
        except (SyncError, OSError):
            continue
    return results


def session_start_usage(archives_root: Path) -> dict[str, int]:
    """Best-effort count and total bytes of session-start archives (valid or not)."""
    if not archives_root.is_dir() or archives_root.is_symlink():
        return {"count": 0, "bytes": 0}
    count = 0
    total = 0
    try:
        for entry in archives_root.iterdir():
            if not SESSION_START_ID_PATTERN.fullmatch(entry.name) or entry.is_symlink() or not entry.is_dir():
                continue
            count += 1
            for item in entry.rglob("*"):
                if item.is_file() and not item.is_symlink():
                    try:
                        total += item.stat().st_size
                    except OSError:
                        pass
    except OSError:
        return {"count": count, "bytes": total}
    return {"count": count, "bytes": total}
