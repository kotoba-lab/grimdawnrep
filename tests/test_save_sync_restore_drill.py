from __future__ import annotations

import json
from pathlib import Path
import subprocess

from grim_dawn_sync import cli
from grim_dawn_sync.git_vault import GitVault
from grim_dawn_sync.manifest import stable_manifest


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, encoding="utf-8",
        capture_output=True,
    ).stdout.strip()


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


def _config(path: Path, *, live: Path, vault: Path) -> None:
    path.write_text(json.dumps({
        "schema_version": "1.0.0", "machine_id": "drill-terminal",
        "save_root": str(live), "vault_repo": str(vault),
        "remote": "origin", "branch": "main",
        "game_install": str(path.parent / "game"), "launcher_mode": "dpyes",
        "launcher_path": str(path.parent / "DPYes.exe"),
        "game_process_names": ["Grim Dawn.exe"], "launch_timeout_seconds": 1,
        "stable_window_seconds": 1, "stable_scan_retries": 1,
        "offline_policy": "deny",
    }), encoding="utf-8")


def test_restore_drill_real_vault_preserves_live_state_and_all_git_refs(tmp_path: Path) -> None:
    """A drill publishes only its verified copy, even with a remote lock present."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    vault_root = tmp_path / "vault"
    _git(tmp_path, "clone", str(remote), str(vault_root))
    _git(vault_root, "config", "user.name", "restore drill test")
    _git(vault_root, "config", "user.email", "restore-drill@example.invalid")
    (vault_root / ".sync" / "empty-hooks").mkdir(parents=True)
    _git(vault_root, "config", "core.hooksPath", ".sync/empty-hooks")

    old_source = tmp_path / "old-source"
    (old_source / "main" / "hero").mkdir(parents=True)
    (old_source / "main" / "hero" / "data.bin").write_bytes(b"historical-save")
    vault = GitVault(vault_root)
    old = vault.snapshot(old_source, machine_id="drill-terminal", session_id="old")
    _git(vault_root, "branch", "-M", "main")
    vault.push(old)
    new_source = tmp_path / "new-source"
    (new_source / "main" / "hero").mkdir(parents=True)
    (new_source / "main" / "hero" / "data.bin").write_bytes(b"current-vault-save")
    newest = vault.snapshot(new_source, machine_id="drill-terminal", session_id="new")
    vault.push(newest)

    # An actual remote lock ref makes the no-Git-mutation guarantee observable.
    _git(vault_root, "tag", "-a", "grim-dawn-sync-active", old, "-m", "test lock")
    _git(vault_root, "push", "origin", "refs/tags/grim-dawn-sync-active")

    root = tmp_path / "terminal-state"; root.mkdir()
    live = root / "live"; (live / "main" / "hero").mkdir(parents=True)
    (live / "main" / "hero" / "data.bin").write_bytes(b"must-not-change")
    config_path = root / "config.local.json"; _config(config_path, live=live, vault=vault_root)
    state_path = root / "state.json"; state_path.write_bytes(b'{"sentinel":"state-is-untouched"}\n')

    expected = vault.validate_commit_snapshot(old)
    before_config = config_path.read_bytes(); before_live = _files(live)
    before_live_manifest = stable_manifest(live, machine_id="drill-terminal", retries=1, window_seconds=0)
    before_state = state_path.read_bytes(); before_state_mtime = state_path.stat().st_mtime_ns
    before_head = _git(vault_root, "rev-parse", "HEAD")
    before_status = _git(vault_root, "status", "--porcelain=v1", "--untracked-files=all")
    before_main = _git(tmp_path, "--git-dir", str(remote), "rev-parse", "refs/heads/main")
    before_lock = _git(tmp_path, "--git-dir", str(remote), "rev-parse", "refs/tags/grim-dawn-sync-active")

    result = cli.restore(config_path, old, apply=True, drill=True)

    assert result["materialized"] is True
    assert (result["root_hash"], result["file_count"], result["total_bytes"]) == (
        expected["root_hash"], expected["file_count"], expected["total_bytes"],
    )
    published = root / "restore-drills" / result["drill_id"]
    assert published.is_dir()
    published_manifest = stable_manifest(published, machine_id="drill-terminal", retries=1, window_seconds=0)
    assert {key: published_manifest[key] for key in ("root_hash", "file_count", "total_bytes")} == {
        key: expected[key] for key in ("root_hash", "file_count", "total_bytes")
    }

    assert config_path.read_bytes() == before_config
    assert _files(live) == before_live
    after_live_manifest = stable_manifest(live, machine_id="drill-terminal", retries=1, window_seconds=0)
    assert {key: after_live_manifest[key] for key in ("root_hash", "file_count", "total_bytes", "files")} == {
        key: before_live_manifest[key] for key in ("root_hash", "file_count", "total_bytes", "files")
    }
    assert state_path.read_bytes() == before_state
    assert state_path.stat().st_mtime_ns == before_state_mtime
    assert _git(vault_root, "rev-parse", "HEAD") == before_head
    assert _git(vault_root, "status", "--porcelain=v1", "--untracked-files=all") == before_status
    assert _git(tmp_path, "--git-dir", str(remote), "rev-parse", "refs/heads/main") == before_main
    assert _git(tmp_path, "--git-dir", str(remote), "rev-parse", "refs/tags/grim-dawn-sync-active") == before_lock
