"""Read-only path discovery. This module never creates candidate directories."""
from __future__ import annotations

import ctypes
from pathlib import Path


def windows_documents_path() -> Path | None:
    """Best-effort Known Folder lookup, isolated from all sync operations."""
    if __import__("os").name != "nt":
        return None
    try:
        buf = ctypes.create_unicode_buffer(260)
        # CSIDL_PERSONAL; SHGetFolderPathW is available on supported Windows.
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
            return Path(buf.value)
    except (AttributeError, OSError):
        pass
    return None


def default_save_root() -> Path | None:
    documents = windows_documents_path()
    return documents / "My Games" / "Grim Dawn" / "save" if documents else None


def resolve_save_root(explicit: Path | None) -> tuple[Path | None, str]:
    if explicit is not None:
        return explicit, "config"
    candidate = default_save_root()
    return candidate, "windows_documents" if candidate else "not_found"


def inspect_path(path: Path | None) -> dict:
    if path is None: return {"configured": False, "exists": False, "type": "missing"}
    try:
        stat = path.lstat(); reparse = getattr(stat, "st_file_attributes", 0) & 0x400
        if path.is_symlink() or reparse: return {"configured": True, "exists": True, "type": "link_or_reparse"}
        return {"configured": True, "exists": True, "type": "directory" if path.is_dir() else "file"}
    except OSError: return {"configured": True, "exists": False, "type": "missing"}


def game_candidates(game_install: Path, launcher_path: Path) -> dict:
    return {"launcher_path": inspect_path(launcher_path), "game_executables": [inspect_path(game_install / part / "Grim Dawn.exe") for part in ("", "x64", "compat")]}


def cloud_candidates(game_install: Path) -> dict:
    # Steam root is derived from the install layout; no account id is retained or reported.
    try: steam = game_install.parents[2]
    except IndexError: return {"status": "not_found", "count": 0}
    userdata = steam / "userdata"
    try:
        if not userdata.is_dir(): return {"status": "not_found", "count": 0, "candidates": []}
        candidates = [path / "219990" / "remote" / "save" for path in userdata.iterdir()]
        safe = [inspect_path(path) for path in candidates]
    except OSError:
        return {"status": "unreadable", "count": 0, "candidates": []}
    normal = [item for item in safe if item["type"] == "directory"]
    return {"status": "single" if len(normal) == 1 else "multiple" if len(normal) > 1 else "not_found", "count": len(normal), "candidates": safe}
