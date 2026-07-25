"""Read-only discovery and diagnostics for a Grim Dawn installation."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
from typing import Iterable

from grim_dawn_lab import __version__


REQUIRED_INPUTS = (
    ("base_database", "base", Path("database/database.arz")),
)

# DLC layers are optional: their absence is retained in the manifest instead of
# being treated as an invalid base-game installation.
OPTIONAL_INPUTS = (
    ("localization_en", "localization", Path("resources/Text_EN.arc")),
    ("localization_ja", "localization", Path("resources/Text_JA.arc")),
    ("gdx1_database", "gdx1", Path("gdx1/database/GDX1.arz")),
    ("gdx2_database", "gdx2", Path("gdx2/database/GDX2.arz")),
    ("gdx3_database", "gdx3", Path("gdx3/database/GDX3.arz")),
    ("gdx3_localization_en", "gdx3_localization", Path("gdx3/resources/Text_EN.arc")),
    ("gdx3_localization_ja", "gdx3_localization", Path("gdx3/resources/Text_JA.arc")),
)


def steam_install_candidates(environ: dict[str, str] | None = None) -> list[Path]:
    """Return deduplicated Steam default locations without touching the filesystem."""
    env = os.environ if environ is None else environ
    roots = [env.get("ProgramFiles(x86)"), env.get("ProgramFiles")]
    candidates: list[Path] = []
    for root in roots:
        if not root:
            continue
        candidate = Path(root) / "Steam" / "steamapps" / "common" / "Grim Dawn"
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def resolve_install_path(
    explicit_path: Path | None,
    candidates: Iterable[Path] | None = None,
) -> tuple[Path | None, str, list[Path]]:
    """Resolve an explicit path first, otherwise the first plausible Steam install."""
    if explicit_path is not None:
        return explicit_path.expanduser().resolve(), "explicit", []

    checked = list(steam_install_candidates() if candidates is None else candidates)
    for candidate in checked:
        if (candidate / "database" / "database.arz").is_file():
            return candidate.resolve(), "steam_default", checked
    return None, "not_found", checked


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_file(install_path: Path, key: str, group: str, relative_path: Path, requirement: str) -> dict:
    path = install_path / relative_path
    item = {
        "key": key,
        "group": group,
        "relative_path": relative_path.as_posix(),
        "exists": path.is_file(),
        "requirement": requirement,
    }
    if item["exists"]:
        stat = path.stat()
        item.update(
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            sha256=sha256_file(path),
        )
    return item


def create_manifest(
    explicit_path: Path | None = None,
    *,
    channel: str = "unknown",
    candidates: Iterable[Path] | None = None,
) -> dict:
    install_path, detection, checked = resolve_install_path(explicit_path, candidates)
    warnings: list[dict[str, str]] = []
    files: list[dict] = []

    if install_path is None:
        warnings.append(
            {
                "code": "install_not_found",
                "message": "Grim Dawn was not found; pass --install-path explicitly.",
            }
        )
    else:
        if not install_path.is_dir():
            warnings.append(
                {
                    "code": "install_path_missing",
                    "message": f"Install path does not exist: {install_path}",
                }
            )
        files = [inspect_file(install_path, *definition, "required") for definition in REQUIRED_INPUTS]
        files.extend(inspect_file(install_path, *definition, "optional") for definition in OPTIONAL_INPUTS)
        for item in files:
            if item["requirement"] != "required":
                continue
            if not item["exists"]:
                warnings.append(
                    {
                        "code": "required_file_missing",
                        "message": f"Required input is missing: {item['relative_path']}",
                    }
                )

    return {
        "schema_version": "1.1.0",
        "tool_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "channel_evidence": "user_supplied" if channel != "unknown" else "not_established",
        "install": {
            "path": str(install_path) if install_path is not None else None,
            "detection": detection,
            "checked_candidates": [str(path) for path in checked],
        },
        "content": {
            "base": any(item["key"] == "base_database" and item["exists"] for item in files),
            "gdx1": any(item["key"] == "gdx1_database" and item["exists"] for item in files),
            "gdx2": any(item["key"] == "gdx2_database" and item["exists"] for item in files),
            "gdx3": any(item["key"] == "gdx3_database" and item["exists"] for item in files),
            "localization_en": any(item["key"] == "localization_en" and item["exists"] for item in files),
            "localization_ja": any(item["key"] == "localization_ja" and item["exists"] for item in files),
        },
        "files": files,
        "warnings": warnings,
    }
