"""Deterministic, fail-closed read-only save manifests."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from pathlib import PurePosixPath
import time

from grim_dawn_sync.errors import EXIT_VALIDATION, SyncError


MANIFEST_SCHEMA_VERSION = "1.0.0"


def _reject_unsafe(path: Path) -> None:
    try:
        stat = path.lstat()
    except OSError as error:
        raise SyncError("save_scan_failed", "Save tree could not be inspected.", EXIT_VALIDATION) from error
    reparse = getattr(stat, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if path.is_symlink() or reparse:
        raise SyncError("unsafe_save_tree", "Save tree contains a link or reparse point.", EXIT_VALIDATION)


def _entries(root: Path) -> list[Path]:
    if not root.is_dir():
        raise SyncError("save_root_missing", "Configured save root does not exist.", EXIT_VALIDATION)
    _reject_unsafe(root)
    entries: list[Path] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        _reject_unsafe(current_path)
        for name in dirs + files:
            entry = current_path / name
            _reject_unsafe(entry)
            if entry.is_file():
                entries.append(entry)
    return sorted(entries, key=lambda path: __import__("unicodedata").normalize("NFC", path.relative_to(root).as_posix()).casefold())


def validate_manifest_path(value: str) -> str:
    """Return canonical safe relative POSIX path; never accept an external escape."""
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value or value.startswith("/") or value.startswith("//") or ":" in value:
        raise SyncError("invalid_manifest_path", "Manifest contains an invalid file path.", EXIT_VALIDATION)
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise SyncError("invalid_manifest_path", "Manifest contains an invalid file path.", EXIT_VALIDATION)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise SyncError("invalid_save_path", "Save tree contains an invalid relative path.", EXIT_VALIDATION)
    return __import__("unicodedata").normalize("NFC", path.as_posix())


def _relative(root: Path, path: Path) -> str:
    return validate_manifest_path(path.relative_to(root).as_posix())


def is_character_player_path(value: str) -> bool:
    try: parts = PurePosixPath(validate_manifest_path(value)).parts
    except SyncError: return False
    return len(parts) == 3 and parts[0].casefold() == "main" and parts[2].casefold() == "player.gdc"


def assert_safe_save_file(root: Path, relative: str) -> Path:
    relative = validate_manifest_path(relative)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    for path in (root, *[root.joinpath(*PurePosixPath(relative).parts[:index]) for index in range(1, len(PurePosixPath(relative).parts) + 1)]):
        _reject_unsafe(path)
    try: candidate.relative_to(root)
    except ValueError as error: raise SyncError("invalid_manifest_path", "Manifest path escapes save root.", EXIT_VALIDATION) from error
    return candidate


def build_manifest(root: Path, *, machine_id: str, now: datetime | None = None) -> dict:
    before_entries = _entries(root)
    files = []
    seen: set[str] = set()
    for path in before_entries:
        relative = _relative(root, path)
        folded = relative.casefold()
        if folded in seen:
            raise SyncError("path_collision", "Save tree contains colliding paths.", EXIT_VALIDATION)
        seen.add(folded)
        _reject_unsafe(path)
        before = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        _reject_unsafe(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise SyncError("save_changed_during_scan", "Save changed during manifest scan.", EXIT_VALIDATION)
        files.append({"path": relative, "size": before.st_size, "mtime_ns": before.st_mtime_ns, "sha256": digest, "_absolute": path})
    if [_relative(root, entry) for entry in _entries(root)] != [_relative(root, entry) for entry in before_entries]:
        raise SyncError("save_changed_during_scan", "Save tree changed during manifest scan.", EXIT_VALIDATION)
    for item in files:
        path = item.pop("_absolute")
        _reject_unsafe(path)
        before_second = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        _reject_unsafe(path)
        after = path.stat()
        if (before_second.st_size, before_second.st_mtime_ns, after.st_size, after.st_mtime_ns, digest) != (item["size"], item["mtime_ns"], item["size"], item.pop("mtime_ns"), item["sha256"]):
            raise SyncError("save_changed_during_scan", "Save changed during manifest scan.", EXIT_VALIDATION)
    canonical = "\n".join(f"{item['path']}\0{item['size']}\0{item['sha256']}" for item in files).encode()
    root_hash = hashlib.sha256(canonical).hexdigest()
    character_count = sum(1 for item in files if is_character_player_path(item["path"]))
    return {"schema_version": MANIFEST_SCHEMA_VERSION, "created_at": (now or datetime.now(timezone.utc)).isoformat(), "machine_id": machine_id, "root_hash": root_hash, "file_count": len(files), "total_bytes": sum(item["size"] for item in files), "character_count": character_count, "files": files}


def stable_manifest(root: Path, *, machine_id: str, retries: int, window_seconds: float = 0) -> dict:
    for _ in range(retries):
        first = build_manifest(root, machine_id=machine_id)
        if window_seconds:
            time.sleep(window_seconds)
        second = build_manifest(root, machine_id=machine_id)
        if first["root_hash"] == second["root_hash"]:
            return second
    raise SyncError("save_not_stable", "Save did not stabilize for validation.", EXIT_VALIDATION)
