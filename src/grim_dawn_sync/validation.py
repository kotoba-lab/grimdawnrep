"""Read-only save validation and destructive-change guards."""
from __future__ import annotations

from pathlib import Path

from grim_dawn_lab.gdc import GdcError, UnsupportedGdcVersion, import_player_gdc
from grim_dawn_sync.errors import EXIT_VALIDATION, SyncError
from grim_dawn_sync.manifest import assert_safe_save_file, is_character_player_path, validate_manifest_path


def validate_players(root: Path, manifest: dict) -> dict:
    unsupported = 0
    invalid = 0
    for item in manifest["files"]:
        path = validate_manifest_path(item.get("path"))
        if is_character_player_path(path):
            try:
                candidate = assert_safe_save_file(root, path)
                try:
                    import_player_gdc(candidate)
                finally:
                    # A parser error must not skip the reparse-point post-check.
                    assert_safe_save_file(root, path)
            except UnsupportedGdcVersion:
                unsupported += 1
            except (GdcError, OSError):
                invalid += 1
    if invalid:
        raise SyncError("invalid_save", "One or more player saves failed validation.", EXIT_VALIDATION)
    if unsupported:
        return {"ok": False, "classification": "unsupported_save_version", "push_allowed": False}
    return {"ok": True, "classification": "valid", "push_allowed": True}


def destructive_change(previous: dict, current: dict) -> dict:
    old = {item["path"]: item for item in previous["files"]}
    new = {item["path"]: item for item in current["files"]}
    removed = set(old) - set(new)
    missing_players = [path for path in removed if is_character_player_path(path)]
    reasons = []
    if current["character_count"] < previous["character_count"]: reasons.append("character_count_decreased")
    if missing_players: reasons.append("player_gdc_missing")
    if removed: reasons.append("files_removed")
    if current["total_bytes"] < previous["total_bytes"]: reasons.append("total_bytes_decreased")
    return {"destructive_change": bool(reasons), "reasons": reasons}
