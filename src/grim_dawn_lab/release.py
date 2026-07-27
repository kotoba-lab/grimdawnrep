"""Fail-closed distribution audit for local game-derived inputs."""

from __future__ import annotations

from pathlib import Path
import subprocess


_FORBIDDEN_SUFFIXES = {".gdc", ".gst", ".gsh"}
_FORBIDDEN_PREFIXES = ("data/raw/", "data/generated/", "captures/")


def audit_git_distribution(root: Path) -> dict:
    root = root.resolve()
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return {"safe": False, "root": str(root), "violations": [{"path": None, "reason": "git_ls_files_failed"}]}
    violations = []
    for raw_path in completed.stdout.splitlines():
        path = raw_path.replace("\\", "/")
        lowered = path.lower()
        if lowered.startswith(_FORBIDDEN_PREFIXES):
            violations.append({"path": path, "reason": "game_derived_or_generated_path"})
        elif Path(lowered).suffix in _FORBIDDEN_SUFFIXES:
            violations.append({"path": path, "reason": "save_file"})
    return {"safe": not violations, "root": str(root), "violations": violations}
