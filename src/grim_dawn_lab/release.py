"""Fail-closed distribution audit for local game-derived inputs."""

from __future__ import annotations

from pathlib import Path
import subprocess


FORBIDDEN_SUFFIXES = {".arz", ".arc", ".gdc", ".gst", ".gsh"}
FORBIDDEN_PREFIXES = ("data/raw/", "data/generated/", "captures/")
SYNTHETIC_GAME_FIXTURE_PREFIX = "tests/fixtures/game_install/"


def audit_distribution_paths(paths: list[str]) -> dict:
    normalized = [path.replace("\\", "/") for path in paths]
    violations = []
    for path in normalized:
        suffix = Path(path).suffix.lower()
        if path.lower().startswith(FORBIDDEN_PREFIXES):
            violations.append({"path": path, "reason": "generated_or_raw_game_data"})
        elif suffix in FORBIDDEN_SUFFIXES and not path.startswith(SYNTHETIC_GAME_FIXTURE_PREFIX):
            violations.append({"path": path, "reason": "game_or_save_binary"})
    return {"safe": not violations, "checked_paths": len(normalized), "violations": violations}


def audit_git_distribution(root: Path) -> dict:
    root = root.resolve()
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        return {
            "safe": False,
            "root": str(root),
            "violations": [{"path": None, "reason": "git_ls_files_failed"}],
        }
    paths = [line for line in completed.stdout.splitlines() if line]
    result = audit_distribution_paths(paths)
    for path in paths:
        normalized = path.replace("\\", "/")
        if normalized.startswith(SYNTHETIC_GAME_FIXTURE_PREFIX) and Path(normalized).suffix.lower() in FORBIDDEN_SUFFIXES:
            if (root / Path(normalized)).stat().st_size > 4096:
                result["violations"].append(
                    {"path": normalized, "reason": "synthetic_fixture_exceeds_4k_limit"}
                )
    result["safe"] = not result["violations"]
    result["root"] = str(root)
    result["source"] = "git_tracked_and_untracked_distribution_files"
    return result
