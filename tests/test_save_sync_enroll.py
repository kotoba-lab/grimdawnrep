from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from grim_dawn_sync import cli
from grim_dawn_sync.errors import SyncError
from grim_dawn_sync.state import SyncState
from grim_dawn_sync.git_vault import GitVault


ROOT = "a" * 64
OID = "b" * 40


class Vault:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.runner = SimpleNamespace(run=lambda *_a, **_kw: SimpleNamespace(stdout=OID + "\n", returncode=0))
    def preflight(self) -> None: self.calls.append("preflight")
    def remote_oid(self) -> str: self.calls.append("remote"); return OID
    def update_fast_forward(self): self.calls.append("ff"); return SimpleNamespace(relation="equal")
    def validate_commit_snapshot(self, commit: str): self.calls.append("validate"); assert commit == OID; return {"root_hash": ROOT}
    def extract_save(self, *_a, **_kw): self.calls.append("extract")


def _config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(save_root=tmp_path / "live", machine_id="terminal-b", stable_scan_retries=1, stable_window_seconds=0)


def test_enroll_dry_run_does_not_fetch_state_or_create_staging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None)
    monkeypatch.setattr(cli, "load_state", lambda _: pytest.fail("dry-run must not read state"))
    result = cli.enroll(tmp_path / "config.local.json", apply=False)
    assert result["remote_snapshot_verified"] is False
    assert vault.calls == ["preflight", "remote"] and not config.save_root.exists()


def test_enroll_apply_existing_different_live_never_extract_or_write_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path); config.save_root.mkdir()
    saved: list[object] = []
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "_process_preflight", lambda _: None)
    monkeypatch.setattr(cli, "load_state", lambda _: (_ for _ in ()).throw(SyncError("state_missing", "x", 6)))
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": "c" * 64})
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True})
    monkeypatch.setattr(cli, "save_state_if_unchanged", lambda *_a: saved.append(True))
    with pytest.raises(SyncError) as caught: cli.enroll(tmp_path / "config.local.json", apply=True)
    assert caught.value.code == "enroll_live_conflict" and "extract" not in vault.calls and saved == []


def test_enroll_existing_matching_live_persists_complete_baseline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path); config.save_root.mkdir(); saved: list[tuple[SyncState, SyncState]] = []
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "_process_preflight", lambda _: None)
    monkeypatch.setattr(cli, "load_state", lambda _: (_ for _ in ()).throw(SyncError("state_missing", "x", 6)))
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": ROOT, "file_count": 1})
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True})
    monkeypatch.setattr(
        cli, "save_state_if_unchanged",
        lambda _p, expected, state: saved.append((expected, state)),
    )
    assert cli.enroll(tmp_path / "config.local.json", apply=True)["idempotent"] is False
    assert saved == [(
        SyncState(),
        SyncState(last_applied_remote_commit=OID, last_applied_manifest_root_hash=ROOT, machine_id="terminal-b"),
    )]


def test_enroll_idempotent_requires_matching_live_and_never_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path); config.save_root.mkdir(); writes: list[object] = []
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "_process_preflight", lambda _: None)
    monkeypatch.setattr(cli, "load_state", lambda _: SyncState(last_applied_remote_commit=OID, last_applied_manifest_root_hash=ROOT, machine_id="terminal-b"))
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": ROOT, "file_count": 1})
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True})
    monkeypatch.setattr(cli, "save_state_if_unchanged", lambda *_a: writes.append(True))
    assert cli.enroll(tmp_path / "config.local.json", apply=True)["idempotent"] is True
    assert writes == [] and "extract" not in vault.calls


def test_enroll_recovery_state_is_rejected_before_fast_forward(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "_process_preflight", lambda _: None)
    monkeypatch.setattr(cli, "load_state", lambda _: SyncState(phase="bootstrap_pending", machine_id="terminal-b", local_commit=OID, last_applied_manifest_root_hash=ROOT))
    with pytest.raises(SyncError) as caught: cli.enroll(tmp_path / "config.local.json", apply=True)
    assert caught.value.code == "recovery_required" and "ff" not in vault.calls


def test_enroll_corrupt_state_is_rejected_before_fast_forward(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "_process_preflight", lambda _: None)
    monkeypatch.setattr(cli, "load_state", lambda _: (_ for _ in ()).throw(SyncError("state_corrupt", "x", 6)))
    with pytest.raises(SyncError) as caught: cli.enroll(tmp_path / "config.local.json", apply=True)
    assert caught.value.code == "state_corrupt" and "ff" not in vault.calls


def test_enroll_machine_id_mismatch_is_not_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path); config.save_root.mkdir()
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "_process_preflight", lambda _: None)
    monkeypatch.setattr(cli, "load_state", lambda _: SyncState(last_applied_remote_commit=OID, last_applied_manifest_root_hash=ROOT, machine_id="other-terminal"))
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": ROOT, "file_count": 1})
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True})
    with pytest.raises(SyncError) as caught: cli.enroll(tmp_path / "config.local.json", apply=True)
    assert caught.value.code == "enroll_state_exists"


def test_enroll_remote_race_before_state_save_leaves_state_unwritten(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path); config.save_root.mkdir(); writes: list[object] = []
    remote_calls = iter([OID, OID, "c" * 40])
    vault.remote_oid = lambda: next(remote_calls)  # type: ignore[method-assign]
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "_process_preflight", lambda _: None)
    monkeypatch.setattr(cli, "load_state", lambda _: (_ for _ in ()).throw(SyncError("state_missing", "x", 6)))
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": ROOT, "file_count": 1})
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True}); monkeypatch.setattr(cli, "save_state_if_unchanged", lambda *_a: writes.append(True))
    with pytest.raises(SyncError) as caught: cli.enroll(tmp_path / "config.local.json", apply=True)
    assert caught.value.code == "enroll_remote_changed" and writes == []


def test_enroll_apply_requires_local_head_after_fast_forward(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path)
    vault.runner = SimpleNamespace(run=lambda *_a, **_kw: SimpleNamespace(stdout="c" * 40 + "\n", returncode=0))
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "_process_preflight", lambda _: None)
    monkeypatch.setattr(cli, "load_state", lambda _: (_ for _ in ()).throw(SyncError("state_missing", "x", 6)))
    with pytest.raises(SyncError) as caught: cli.enroll(tmp_path / "config.local.json", apply=True)
    assert caught.value.code == "vault_not_reconciled" and "validate" not in vault.calls


def test_enroll_missing_live_extracts_restores_then_saves_baseline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path); saved: list[tuple[SyncState, SyncState]] = []
    def extract(_commit: str, destination: Path, **_kw: object) -> None:
        vault.calls.append("extract"); destination.mkdir()
    vault.extract_save = extract  # type: ignore[method-assign]
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "_process_preflight", lambda *_: None)
    monkeypatch.setattr(cli, "load_state", lambda _: (_ for _ in ()).throw(SyncError("state_missing", "x", 6)))
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": ROOT, "file_count": 1})
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True})
    monkeypatch.setattr(cli, "restore_from_directory", lambda _src, live, *_a, **_k: (live.mkdir(), {"root_hash": ROOT})[1])
    monkeypatch.setattr(
        cli, "save_state_if_unchanged",
        lambda _p, expected, state: saved.append((expected, state)),
    )
    result = cli.enroll(tmp_path / "state" / "config.local.json", apply=True)
    assert result["idempotent"] is False and vault.calls.count("extract") == 1
    assert saved == [(
        SyncState(),
        SyncState(last_applied_remote_commit=OID, last_applied_manifest_root_hash=ROOT, machine_id="terminal-b"),
    )]


def test_enroll_foreign_state_cas_failure_retries_without_second_restore(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path); saves: list[tuple[SyncState, SyncState]] = []; extracts: list[Path] = []
    vault.extract_save = lambda _c, destination, **_kw: (extracts.append(destination), destination.mkdir())  # type: ignore[method-assign]
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "_process_preflight", lambda *_: None)
    monkeypatch.setattr(cli, "load_state", lambda _: (_ for _ in ()).throw(SyncError("state_missing", "x", 6)))
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": ROOT, "file_count": 1})
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True})
    monkeypatch.setattr(cli, "restore_from_directory", lambda _src, live, *_a, **_k: (live.mkdir(), {"root_hash": ROOT})[1])
    monkeypatch.setattr(
        cli, "save_state_if_unchanged",
        lambda *_a: (_ for _ in ()).throw(SyncError("selection_stale", "foreign state", 4)),
    )
    with pytest.raises(SyncError) as caught:
        cli.enroll(tmp_path / "state" / "config.local.json", apply=True)
    assert caught.value.code == "selection_stale"
    monkeypatch.setattr(
        cli, "save_state_if_unchanged",
        lambda _p, expected, state: saves.append((expected, state)),
    )
    assert cli.enroll(tmp_path / "state" / "config.local.json", apply=True)["idempotent"] is False
    assert len(extracts) == 1 and saves == [(
        SyncState(),
        SyncState(last_applied_remote_commit=OID, last_applied_manifest_root_hash=ROOT, machine_id="terminal-b"),
    )]


def test_enroll_dry_run_rejects_head_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path)
    vault.runner = SimpleNamespace(run=lambda *_a, **_kw: SimpleNamespace(stdout="c" * 40 + "\n", returncode=0))
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault); monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None)
    with pytest.raises(SyncError) as caught: cli.enroll(tmp_path / "config.local.json", apply=False)
    assert caught.value.code == "vault_not_reconciled"


@pytest.mark.parametrize("kind", ["empty", "file"])
def test_enroll_rejects_empty_or_non_directory_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str) -> None:
    vault = Vault(); config = _config(tmp_path)
    if kind == "empty": config.save_root.mkdir()
    else: config.save_root.write_text("not a save", encoding="utf-8")
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "_process_preflight", lambda *_: None)
    monkeypatch.setattr(cli, "load_state", lambda _: (_ for _ in ()).throw(SyncError("state_missing", "x", 6)))
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": ROOT, "file_count": 0})
    with pytest.raises(SyncError) as caught: cli.enroll(tmp_path / "config.local.json", apply=True)
    assert caught.value.code == ("enroll_live_conflict" if kind == "empty" else "enroll_live_unsafe")


def test_enroll_rejects_invalid_existing_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path); config.save_root.mkdir()
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "_process_preflight", lambda *_: None)
    monkeypatch.setattr(cli, "load_state", lambda _: (_ for _ in ()).throw(SyncError("state_missing", "x", 6)))
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": ROOT, "file_count": 1})
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": False, "classification": "invalid_save"})
    with pytest.raises(SyncError) as caught: cli.enroll(tmp_path / "config.local.json", apply=True)
    assert caught.value.code == "invalid_save"


def test_enroll_final_lock_race_does_not_save_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path); config.save_root.mkdir(); writes: list[object] = []; locks = iter([None, None, object()])
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: next(locks)); monkeypatch.setattr(cli, "_process_preflight", lambda *_: None)
    monkeypatch.setattr(cli, "load_state", lambda _: (_ for _ in ()).throw(SyncError("state_missing", "x", 6)))
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": ROOT, "file_count": 1}); monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True}); monkeypatch.setattr(cli, "save_state_if_unchanged", lambda *_a: writes.append(True))
    with pytest.raises(SyncError) as caught: cli.enroll(tmp_path / "config.local.json", apply=True)
    assert caught.value.code == "enroll_remote_changed" and writes == []


def test_enroll_rechecks_process_after_extract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path); restored: list[object] = []; calls = 0
    vault.extract_save = lambda _c, destination, **_kw: destination.mkdir()  # type: ignore[method-assign]
    def preflight(*_a: object) -> None:
        nonlocal calls; calls += 1
        if calls == 2: raise SyncError("game_already_running", "x", 5)
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault); monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "_process_preflight", preflight)
    monkeypatch.setattr(cli, "load_state", lambda _: (_ for _ in ()).throw(SyncError("state_missing", "x", 6))); monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": ROOT, "file_count": 1}); monkeypatch.setattr(cli, "restore_from_directory", lambda *_a, **_k: restored.append(True))
    with pytest.raises(SyncError) as caught: cli.enroll(tmp_path / "state" / "config.local.json", apply=True)
    assert caught.value.code == "game_already_running" and restored == []


def test_enroll_accepts_behind_after_fast_forward_head_proof(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vault = Vault(); config = _config(tmp_path); config.save_root.mkdir()
    vault.update_fast_forward = lambda: SimpleNamespace(relation="behind")  # type: ignore[method-assign]
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: vault); monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "_process_preflight", lambda *_: None)
    monkeypatch.setattr(cli, "load_state", lambda _: (_ for _ in ()).throw(SyncError("state_missing", "x", 6))); monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": ROOT, "file_count": 1}); monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True}); monkeypatch.setattr(cli, "save_state_if_unchanged", lambda *_a: None)
    assert cli.enroll(tmp_path / "config.local.json", apply=True)["commit"] == OID


def test_enroll_behind_real_git_clone_fast_forwards_and_preserves_remote_refs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def git(cwd: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()
    remote = tmp_path / "remote.git"; git(tmp_path, "init", "--bare", str(remote))
    a, b = tmp_path / "a", tmp_path / "b"; git(tmp_path, "clone", str(remote), str(a))
    git(a, "config", "user.email", "test@example.invalid"); git(a, "config", "user.name", "test")
    source1 = tmp_path / "source1"; (source1 / "main" / "char").mkdir(parents=True); (source1 / "main" / "char" / "data.bin").write_bytes(b"one")
    first = GitVault(a); first.push(first.snapshot(source1, machine_id="terminal-a", session_id="first"))
    git(tmp_path, "clone", str(remote), str(b)); git(b, "checkout", "-b", "main", "origin/main")
    source2 = tmp_path / "source2"; shutil.copytree(source1, source2); (source2 / "main" / "char" / "data.bin").write_bytes(b"two")
    second = first.snapshot(source2, machine_id="terminal-a", session_id="second"); first.push(second)
    before_refs = git(tmp_path, "--git-dir", str(remote), "show-ref")
    config = SimpleNamespace(save_root=tmp_path / "live", vault_repo=b, remote="origin", branch="main", machine_id="terminal-b", stable_scan_retries=1, stable_window_seconds=0)
    shutil.copytree(source2, config.save_root)
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_process_preflight", lambda *_: None)
    result = cli.enroll(tmp_path / "state" / "config.local.json", apply=True)
    assert result["commit"] == second and GitVault(b).runner.run("rev-parse", "HEAD").stdout.strip() == second
    assert cli.load_state(tmp_path / "state" / "state.json") == SyncState(last_applied_remote_commit=second, last_applied_manifest_root_hash=result["root_hash"], machine_id="terminal-b")
    assert git(tmp_path, "--git-dir", str(remote), "show-ref") == before_refs
