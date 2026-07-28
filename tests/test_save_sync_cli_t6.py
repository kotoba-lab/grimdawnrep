from __future__ import annotations

import json
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from grim_dawn_sync import cli
from grim_dawn_sync.errors import EXIT_CONFIGURATION, EXIT_RECOVERY_REQUIRED, SyncError
from grim_dawn_sync.process_monitor import ProcessInfo, ProcessScan


def test_parser_exposes_full_t6_command_contract() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["recover"]).command == "recover"
    assert parser.parse_args(["snapshot"]).command == "snapshot"
    assert parser.parse_args(["launch"]).command == "launch"
    assert parser.parse_args(["restore", "--commit", "deadbeef"]).apply is False
    assert parser.parse_args(["restore", "--commit", "deadbeef", "--apply"]).apply is True
    assert parser.parse_args(["bootstrap", "--source-cloud", "cloud"]).apply is False
    assert parser.parse_args(["bootstrap", "--source-cloud", "cloud", "--apply"]).apply is True
    assert parser.parse_args(["install-shortcut"]).apply is False
    with pytest.raises(SystemExit):
        parser.parse_args(["restore"])
    with pytest.raises(SystemExit):
        parser.parse_args(["bootstrap"])


def test_main_routes_every_t6_command_and_keeps_json_serializable(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def fake(name: str):
        def handler(*args: object, **kwargs: object) -> dict[str, object]:
            calls.append((name, args, kwargs))
            return {"schema_version": "1.0.0", "command": name, "path": tmp_path / name}
        return handler

    for name in ("status", "recover", "restore", "snapshot", "bootstrap"):
        monkeypatch.setattr(cli, name, fake(name))
    config = tmp_path / "config.local.json"
    for command in (
        ["status"], ["recover"], ["snapshot"],
        ["restore", "--commit", "c0ffee", "--apply"],
        ["bootstrap", "--source-cloud", str(tmp_path / "cloud"), "--apply"],
    ):
        assert cli.main(["--config", str(config), "--json", *command]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["path"] == str(tmp_path / payload["command"])
    assert [name for name, _, _ in calls] == ["status", "recover", "snapshot", "restore", "bootstrap"]
    assert calls[-2][2] == {"apply": True}
    assert calls[-1][2] == {"apply": True}


def test_restore_dry_run_and_apply_only_delegate_apply_to_domain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.json"
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1, stable_window_seconds=1)
    calls: list[bool] = []

    class Vault:
        def preflight(self) -> None: pass
        def extract_save(self, *args: object, **kwargs: object) -> None: pass

    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "_inspect_restore", lambda *_: {"dry_run": True})
    monkeypatch.setattr(cli, "_validate_restore_ancestry", lambda *_: None)
    monkeypatch.setattr(cli, "restore_from_directory", lambda *args, **kwargs: calls.append(kwargs["apply"]) or {"dry_run": not kwargs["apply"]})
    assert cli.restore(config_path, "commit", apply=False)["dry_run"] is True
    assert cli.restore(config_path, "commit", apply=True)["dry_run"] is False
    assert calls == [True]
    assert not config.save_root.exists()


def test_restore_apply_prepares_its_owned_staging_parent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.json"
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1, stable_window_seconds=1)

    class Vault:
        def preflight(self) -> None: pass
        def extract_save(self, _commit: str, destination: Path, **_kwargs: object) -> None:
            assert destination.parent == tmp_path / "staging"
            assert destination.parent.is_dir() and not destination.parent.is_symlink()
            destination.mkdir()

    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "_validate_restore_ancestry", lambda *_: None)
    monkeypatch.setattr(cli, "_process_preflight", lambda *_: {"status": "stopped"})
    monkeypatch.setattr(cli, "restore_from_directory", lambda *args, **kwargs: {"dry_run": False})

    assert cli.restore(config_path, "commit", apply=True)["dry_run"] is False
    assert (tmp_path / "staging").is_dir()


def test_restore_apply_rejects_unsafe_staging_before_extract_or_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "state" / "config.local.json"
    staging = config_path.parent / "staging"; staging.parent.mkdir()
    staging.write_text("not a directory", encoding="utf-8")
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1, stable_window_seconds=0)
    calls: list[str] = []

    class Vault:
        def preflight(self) -> None: pass
        def extract_save(self, *_args: object, **_kwargs: object) -> None: calls.append("extract")

    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "_validate_restore_ancestry", lambda *_: None)
    monkeypatch.setattr(cli, "_process_preflight", lambda *_: {"status": "stopped"})
    monkeypatch.setattr(cli, "restore_from_directory", lambda *_args, **_kwargs: calls.append("restore"))

    with pytest.raises(SyncError) as error:
        cli.restore(config_path, "a" * 40, apply=True)
    assert error.value.code == "unsafe_archive_path"
    assert calls == [] and not config.save_root.exists()


def test_restore_apply_rejects_reparse_staging_before_extract_or_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "state" / "config.local.json"
    staging = config_path.parent / "staging"; staging.mkdir(parents=True)
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1, stable_window_seconds=0)
    calls: list[str] = []; original_lstat = Path.lstat

    def reparse_lstat(path: Path):
        if path == staging:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original_lstat(path)

    class Vault:
        def preflight(self) -> None: pass
        def extract_save(self, *_args: object, **_kwargs: object) -> None: calls.append("extract")

    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "_validate_restore_ancestry", lambda *_: None)
    monkeypatch.setattr(cli, "_process_preflight", lambda *_: {"status": "stopped"})
    monkeypatch.setattr(Path, "lstat", reparse_lstat)

    with pytest.raises(SyncError) as error:
        cli.restore(config_path, "a" * 40, apply=True)
    assert error.value.code == "unsafe_archive_path"
    assert calls == [] and not config.save_root.exists()


def test_restore_apply_uses_a_fresh_create_only_destination_each_time(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.json"
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1, stable_window_seconds=0)
    destinations: list[Path] = []

    class Vault:
        def preflight(self) -> None: pass
        def extract_save(self, _commit: str, destination: Path, **_kwargs: object) -> None:
            assert not destination.exists()
            destination.mkdir(); destinations.append(destination)

    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "_validate_restore_ancestry", lambda *_: None)
    monkeypatch.setattr(cli, "_process_preflight", lambda *_: {"status": "stopped"})
    monkeypatch.setattr(cli, "restore_from_directory", lambda *_args, **_kwargs: {"dry_run": False})

    cli.restore(config_path, "a" * 40, apply=True)
    cli.restore(config_path, "a" * 40, apply=True)
    assert len(destinations) == 2 and destinations[0] != destinations[1]


def test_restore_dry_run_inspects_only_and_leaves_filesystem_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.json"
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1, stable_window_seconds=1)
    calls: list[tuple[str, tuple[str, ...]]] = []

    class Vault:
        branch = "main"
        runner = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0))
        def preflight(self) -> None: calls.append(("preflight", ()))
        def validate_commit_snapshot(self, commit: str):
            calls.append(("validate_commit_snapshot", (commit,)))
            return {"root_hash": "a" * 64, "file_count": 1, "total_bytes": 2}

    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    result = cli.restore(config_path, "a" * 40, apply=False)
    assert result["dry_run"] and result["root_hash"] == "a" * 64
    assert calls == [("preflight", ()), ("validate_commit_snapshot", ("a" * 40,))]
    assert not any(tmp_path.iterdir())


def test_bootstrap_default_dry_run_never_preflights_or_snapshots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.json"; source = tmp_path / "cloud"; source.mkdir()
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1, stable_window_seconds=1)
    calls: list[str] = []

    class Vault:
        def preflight(self) -> None: calls.append("preflight")
        def remote_oid(self) -> None: calls.append("remote_oid"); return None
        def snapshot(self, *args: object, **kwargs: object) -> str: calls.append("snapshot"); return "local"
        def validate_commit_snapshot(self, _): calls.append("validate"); return {"root_hash": "a" * 64}
        def extract_save(self, *_a, **_k): calls.append("extract")
        runner = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="local"))

    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "restore_from_directory", lambda *args, **kwargs: calls.append(f"restore:{kwargs['apply']}") or ({"dry_run": True} if not kwargs["apply"] else {"root_hash": "a" * 64}))
    monkeypatch.setattr(cli, "stable_manifest", lambda *a, **k: {"root_hash": "a" * 64})
    monkeypatch.setattr(cli, "validate_players", lambda *a: {"ok": True})
    monkeypatch.setattr(cli, "_copy_verified", lambda *a: calls.append("archive"))
    monkeypatch.setattr(cli, "prepare_bootstrap", lambda *_a, **_k: calls.append("prepare") or cli.SyncState(machine_id="t6", phase="bootstrap_pending", local_commit="local", last_applied_manifest_root_hash="a" * 64))
    monkeypatch.setattr(cli, "mark_bootstrap_live_applied", lambda *_a, **_k: calls.append("mark"))
    monkeypatch.setattr(cli, "push_bootstrap", lambda _vault, _machine, oid, root_hash, **_kwargs: calls.extend(["push", "save_state"]) or "remote")
    assert cli.bootstrap(config_path, source, apply=False)["dry_run"] is True
    assert calls == ["restore:False"]
    assert cli.bootstrap(config_path, source, apply=True)["commit"] == "remote"
    assert calls == ["restore:False", "restore:False", "preflight", "remote_oid", "archive", "snapshot", "remote_oid", "prepare", "validate", "remote_oid", "extract", "restore:True", "mark", "push", "save_state"]
    assert not config.save_root.exists()


def test_bootstrap_persists_remote_commit_and_applied_root_after_push(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.json"; source = tmp_path / "cloud"; source.mkdir()
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1, stable_window_seconds=1)
    saved: list[object] = []
    class Vault:
        def preflight(self) -> None: pass
        def remote_oid(self) -> None: return None
        def snapshot(self, *args: object, **kwargs: object) -> str: return "b" * 40
        def validate_commit_snapshot(self, _): return {"root_hash": "a" * 64}
        def extract_save(self, *_a, **_k): pass
        runner = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="b" * 40))
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "restore_from_directory", lambda *args, **kwargs: {"dry_run": True} if not kwargs["apply"] else {"root_hash": "a" * 64})
    monkeypatch.setattr(cli, "stable_manifest", lambda *a, **k: {"root_hash": "a" * 64})
    monkeypatch.setattr(cli, "validate_players", lambda *a: {"ok": True})
    monkeypatch.setattr(cli, "_copy_verified", lambda *a: None)
    monkeypatch.setattr(cli, "prepare_bootstrap", lambda *_a, **_k: cli.SyncState(machine_id="t6", phase="bootstrap_pending", local_commit="b" * 40, last_applied_manifest_root_hash="a" * 64))
    monkeypatch.setattr(cli, "mark_bootstrap_live_applied", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "push_bootstrap", lambda _vault, _machine, oid, root_hash, **_kwargs: saved.append((oid, root_hash)) or oid)
    assert cli.bootstrap(config_path, source, apply=True)["commit"] == "b" * 40
    assert saved == [("b" * 40, "a" * 64)]


def test_bootstrap_does_not_persist_baseline_when_push_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.json"; source = tmp_path / "cloud"; source.mkdir()
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1, stable_window_seconds=1)
    class Vault:
        def preflight(self) -> None: pass
        def remote_oid(self) -> None: return None
        def snapshot(self, *args: object, **kwargs: object) -> str: return "b" * 40
        def validate_commit_snapshot(self, _): return {"root_hash": "a" * 64}
        def extract_save(self, *_a, **_k): pass
        runner = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="b" * 40))
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "restore_from_directory", lambda *args, **kwargs: {"dry_run": True} if not kwargs["apply"] else {"root_hash": "a" * 64})
    monkeypatch.setattr(cli, "stable_manifest", lambda *a, **k: {"root_hash": "a" * 64})
    monkeypatch.setattr(cli, "validate_players", lambda *a: {"ok": True})
    monkeypatch.setattr(cli, "_copy_verified", lambda *a: None)
    monkeypatch.setattr(cli, "prepare_bootstrap", lambda *_a, **_k: cli.SyncState(machine_id="t6", phase="bootstrap_pending", local_commit="b" * 40, last_applied_manifest_root_hash="a" * 64))
    monkeypatch.setattr(cli, "mark_bootstrap_live_applied", lambda *_a, **_k: None)
    pending: list[tuple[str, str]] = []
    def fail_push(_vault, _machine, oid, root_hash, **_kwargs):
        pending.append((oid, root_hash))
        raise SyncError("push_incomplete", "safe", EXIT_RECOVERY_REQUIRED, {"local_commit": oid})
    monkeypatch.setattr(cli, "push_bootstrap", fail_push)
    with pytest.raises(SyncError, match="safe"):
        cli.bootstrap(config_path, source, apply=True)
    assert pending == [("b" * 40, "a" * 64)]


def test_launch_and_shortcut_apply_are_explicit_and_dry_run_does_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    config_path = tmp_path / "config.local.json"; calls: list[object] = []
    monkeypatch.setattr(cli, "install_shortcut", lambda desktop: calls.append(desktop))
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path))
    assert cli.main(["--json", "install-shortcut"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True and calls == []
    assert cli.main(["--json", "install-shortcut", "--apply"]) == 0
    assert calls == [tmp_path / "Desktop"]
    capsys.readouterr()

    config = object()
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    class FakeLaunch:
        def __init__(self, got_config: object, root: Path) -> None: calls.extend([got_config, root])
        def run(self) -> dict[str, str]: return {"phase": "complete"}
    monkeypatch.setattr(cli, "LaunchWorkflow", FakeLaunch)
    assert cli.main(["--config", str(config_path), "--json", "launch"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == {"phase": "complete"}
    assert calls[-2:] == [config, tmp_path]


@pytest.mark.parametrize(("error", "expected"), [
    (SyncError("bad_config", "bad", EXIT_CONFIGURATION), "grim-dawn-sync status"),
    (SyncError("recovery_required", "recover", EXIT_RECOVERY_REQUIRED), "grim-dawn-sync recover"),
])
def test_expected_errors_have_exit_codes_and_json_next_steps(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: SyncError, expected: str) -> None:
    monkeypatch.setattr(cli, "status", lambda _: (_ for _ in ()).throw(error))
    assert cli.main(["--json", "status"]) == error.exit_code
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["next_command"] == expected


def test_unexpected_cli_failure_is_safe_recovery_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "status", lambda _: (_ for _ in ()).throw(RuntimeError("https://secret.example/save/character")))
    assert cli.main(["--json", "status"]) == EXIT_RECOVERY_REQUIRED
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["next_command"] == "grim-dawn-sync recover"
    assert "secret" not in json.dumps(payload)


class _Monitor:
    def __init__(self, scan: ProcessScan) -> None: self.scan_value = scan
    def scan(self) -> ProcessScan: return self.scan_value


@pytest.mark.parametrize("scan", [
    ProcessScan((), complete=False),
    ProcessScan((ProcessInfo(1, "Grim Dawn.exe", None, None),)),
    ProcessScan((ProcessInfo(2, "DPYes.exe", None, None),)),
])
def test_process_preflight_refuses_unknown_or_running_without_mutation(scan: ProcessScan) -> None:
    config = SimpleNamespace(game_process_names=("Grim Dawn.exe",))
    with pytest.raises(SyncError):
        cli._process_preflight(config, _Monitor(scan))


def test_bootstrap_apply_refuses_existing_live_before_restore(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "cloud"; source.mkdir(); live = tmp_path / "live"; live.mkdir()
    config = SimpleNamespace(save_root=live, machine_id="t6", stable_scan_retries=1, stable_window_seconds=0, game_process_names=("Grim Dawn.exe",))
    calls: list[str] = []
    class Vault:
        def preflight(self) -> None: calls.append("preflight")
        def remote_oid(self): return None
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "_process_preflight", lambda *_: calls.append("process") or {})
    monkeypatch.setattr(cli, "restore_from_directory", lambda *a, **k: calls.append("restore") or {"dry_run": True})
    with pytest.raises(SyncError, match="missing live"):
        cli.bootstrap(tmp_path / "config.local.json", source, apply=True)
    assert calls == ["restore", "process", "preflight"]


def test_snapshot_archives_before_lock_and_retains_state_on_push_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live = tmp_path / "live"; live.mkdir(); config = SimpleNamespace(save_root=live, vault_repo=tmp_path / "vault", machine_id="t6", stable_scan_retries=1, stable_window_seconds=0, game_process_names=("Grim Dawn.exe",))
    calls: list[str] = []; saved: list[object] = []
    class Lock: oid="lock"; local_tag="tag"; session=SimpleNamespace(session_id="session", machine_id="t6", base_commit="b" * 40)
    class Vault:
        def update_fast_forward(self): return SimpleNamespace(relation="equal")
        def remote_oid(self): return "b" * 40
        def snapshot(self, *a, **k): calls.append("snapshot"); return "c" * 40
        def push(self, _): calls.append("push"); raise SyncError("push_incomplete", "safe", 6)
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "_process_preflight", lambda *_: calls.append("process") or {})
    monkeypatch.setattr(cli, "stable_manifest", lambda *a, **k: {"root_hash":"a" * 64})
    monkeypatch.setattr(cli, "validate_players", lambda *a: {"ok": True})
    monkeypatch.setattr(cli, "_copy_verified", lambda *a: calls.append("archive"))
    monkeypatch.setattr(cli, "acquire_lock", lambda *a, **k: calls.append("lock") or Lock())
    monkeypatch.setattr(cli, "save_state", lambda *a: saved.append(a[1]))
    with pytest.raises(SyncError, match="safe"): cli.snapshot(tmp_path / "config.local.json")
    assert calls == ["process", "archive", "lock", "snapshot", "push"]
    assert saved and saved[-1].local_commit == "c" * 40


def test_status_reports_archive_and_vault_usage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "archives").mkdir(); (tmp_path / "archives" / "a").write_bytes(b"abc")
    vault_root = tmp_path / "vault" / "save"; vault_root.mkdir(parents=True); (vault_root / "b").write_bytes(b"1234")
    config = SimpleNamespace(vault_repo=tmp_path / "vault", machine_id="t6")
    class Vault:
        def preflight(self): pass
        def remote_oid(self): return "a" * 40
        runner = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="a" * 40))
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None); monkeypatch.setattr(cli, "load_state", lambda _: cli.SyncState())
    result = cli.status(tmp_path / "config.local.json", monitor=_Monitor(ProcessScan(())))
    assert result["readiness"] == "ready" and result["archive_usage"]["bytes"] == 3 and result["vault_usage"]["bytes"] == 4


def test_doctor_is_read_only_and_does_not_emit_config_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = SimpleNamespace(save_root=tmp_path / "live", vault_repo=tmp_path / "vault", game_install=tmp_path / "game", launcher_path=tmp_path / "game" / "DPYes.exe", machine_id="t6", stable_scan_retries=1, stable_window_seconds=0)
    class Vault:
        def preflight(self): pass
        def remote_oid(self): return "a" * 40
        runner = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="a" * 40))
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "inspect_path", lambda _: {"exists": False})
    monkeypatch.setattr(cli, "cloud_candidates", lambda _: {"detected": False})
    monkeypatch.setattr(cli, "game_candidates", lambda *_: {"dpyes": False})
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: None)
    report = cli.doctor(tmp_path / "secret-config.local.json", monitor=_Monitor(ProcessScan(())))
    encoded = json.dumps(report)
    assert report["read_only"] and report["checks"]["processes"]["status"] == "clear"
    assert "secret-config" not in encoded and str(tmp_path) not in encoded


def test_restore_dry_run_uses_read_manifest_and_never_extracts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1, stable_window_seconds=0)
    calls: list[str] = []
    class Vault:
        branch = "main"
        runner = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0))
        def preflight(self): pass
        def validate_commit_snapshot(self, _): calls.append("validate_commit_snapshot"); return {"root_hash":"a" * 64, "file_count":1, "total_bytes":2}
    monkeypatch.setattr(cli, "load_config", lambda _: config); monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    result = cli.restore(tmp_path / "config.local.json", "b" * 40, apply=False)
    assert result["dry_run"] and calls == ["validate_commit_snapshot"] and not list(tmp_path.iterdir())


def test_restore_rejects_commit_outside_configured_branch_before_manifest_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    calls: list[tuple[object, ...]] = []
    class Vault:
        branch = "main"
        def preflight(self): pass
        runner = SimpleNamespace(run=lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=1))
        def validate_commit_snapshot(self, _): pytest.fail("unreachable commit must not be read")
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1, stable_window_seconds=0)
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    with pytest.raises(SyncError) as error:
        cli.restore(tmp_path / "config.local.json", "b" * 40, apply=False)
    assert error.value.code == "restore_commit_not_in_history"
    assert calls == [("merge-base", "--is-ancestor", "b" * 40, "refs/heads/main")]


def test_bootstrap_prepare_failure_never_creates_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    source = tmp_path / "cloud"; source.mkdir()
    live = tmp_path / "live"; calls: list[str] = []
    config = SimpleNamespace(save_root=live, machine_id="t6", stable_scan_retries=1, stable_window_seconds=0)
    class Vault:
        def preflight(self): calls.append("preflight")
        def remote_oid(self): calls.append("remote"); return None
        def snapshot(self, *_a, **_k): calls.append("snapshot"); return "b" * 40
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": "a" * 64})
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True})
    monkeypatch.setattr(cli, "_copy_verified", lambda *_a: calls.append("archive"))
    monkeypatch.setattr(cli, "restore_from_directory", lambda *_a, **k: calls.append(f"restore:{k['apply']}") or {"dry_run": True})
    def fail_prepare(*_a, **_k):
        calls.append("prepare")
        raise SyncError("state_write_failed", "safe", EXIT_RECOVERY_REQUIRED)
    monkeypatch.setattr(cli, "prepare_bootstrap", fail_prepare)
    with pytest.raises(SyncError, match="safe"):
        cli.bootstrap(tmp_path / "config.local.json", source, apply=True)
    assert calls == ["restore:False", "preflight", "remote", "archive", "snapshot", "remote", "prepare"]
    assert not live.exists()


def test_bootstrap_matching_pending_resumes_without_overwriting_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    source = tmp_path / "cloud"; source.mkdir()
    live = tmp_path / "live"; live.mkdir()
    (live / "player.gdc").write_bytes(b"pending")
    oid, root_hash = "b" * 40, "a" * 64
    config = SimpleNamespace(save_root=live, machine_id="t6", stable_scan_retries=1, stable_window_seconds=0)
    pending = cli.SyncState(
        machine_id="t6", phase="bootstrap_pending", local_commit=oid,
        last_applied_manifest_root_hash=root_hash, bootstrap_live_applied=True,
    )
    calls: list[str] = []
    class Vault:
        def preflight(self): pass
        def remote_oid(self): return None
        def snapshot(self, *_a, **_k): calls.append("snapshot"); return oid
        def validate_commit_snapshot(self, _): calls.append("validate"); return {"root_hash": root_hash}
        runner = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=oid))
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "load_state", lambda _: pending)
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": root_hash})
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True})
    monkeypatch.setattr(cli, "_copy_verified", lambda *_a: calls.append("archive"))
    monkeypatch.setattr(cli, "restore_from_directory", lambda *_a, **k: calls.append(f"restore:{k['apply']}") or {"dry_run": True})
    monkeypatch.setattr(cli, "prepare_bootstrap", lambda *_a, **_k: calls.append("prepare"))
    monkeypatch.setattr(cli, "mark_bootstrap_live_applied", lambda *_a, **_k: calls.append("mark"))
    monkeypatch.setattr(cli, "push_bootstrap", lambda *_a, **_k: calls.append("push") or oid)
    assert cli.bootstrap(tmp_path / "config.local.json", source, apply=True)["commit"] == oid
    assert calls == ["restore:False", "validate", "push"]


def test_bootstrap_competing_recovery_state_fails_before_archive_or_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    source = tmp_path / "cloud"; source.mkdir()
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1, stable_window_seconds=0)
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: SimpleNamespace(preflight=lambda: None))
    monkeypatch.setattr(cli, "load_state", lambda _: SimpleNamespace(phase="lock_held"))
    monkeypatch.setattr(cli, "restore_from_directory", lambda *_a, **_k: {"dry_run": True})
    monkeypatch.setattr(cli, "_copy_verified", lambda *_a: pytest.fail("must fail before archive"))
    with pytest.raises(SyncError) as error:
        cli.bootstrap(tmp_path / "config.local.json", source, apply=True)
    assert error.value.code == "recovery_required"
    assert not config.save_root.exists()


def test_doctor_reports_running_and_uses_readonly_lock_inspection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    calls: list[str] = []
    config = SimpleNamespace(
        save_root=tmp_path / "live", vault_repo=tmp_path / "vault",
        game_install=tmp_path / "game", launcher_path=tmp_path / "launcher",
        machine_id="t6", stable_scan_retries=1, stable_window_seconds=0,
        game_process_names=("Grim Dawn.exe",),
    )
    class Vault:
        def preflight(self): calls.append("preflight")
        def remote_oid(self): calls.append("ls-remote"); return "a" * 40
        runner = SimpleNamespace(run=lambda *a, **k: calls.append(a[0]) or SimpleNamespace(returncode=0, stdout="a" * 40))
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "inspect_path", lambda _: {"exists": False})
    monkeypatch.setattr(cli, "cloud_candidates", lambda _: {"detected": False})
    monkeypatch.setattr(cli, "game_candidates", lambda *_: {"detected": False})
    monkeypatch.setattr(cli, "inspect_remote_lock_readonly", lambda _: calls.append("readonly-lock") or None)
    report = cli.doctor(
        tmp_path / "config.local.json",
        monitor=_Monitor(ProcessScan((ProcessInfo(1, "Grim Dawn.exe", None, None),))),
    )
    assert report["checks"]["processes"] == {"status": "running", "complete": True}
    assert calls == ["preflight", "ls-remote", "rev-parse", "readonly-lock"]
    assert not {"fetch", "update-ref"}.intersection(calls)


def test_safe_error_payload_emits_allowlisted_recovery_metadata_only() -> None:
    oid, root_hash = "b" * 40, "a" * 64
    details = {
        "safe_oid": oid, "safe_root_hash": root_hash,
        "archive_root": root_hash, "quarantine_root": root_hash,
        "archive_id": "save-20260729T000000Z-aaaaaaaaaaaaaaaa-" + "c" * 32,
        "quarantine_id": "save-20260729T000000Z-aaaaaaaaaaaaaaaa-" + "d" * 32,
        "machine_id": "machine", "session_id": "session", "last_state": "PUSH",
        "next_command": "grim-dawn-sync recover",
        "stderr": "secret", "path": "C:/secret", "url": "https://secret.example",
    }
    payload = cli._safe_error_payload(SyncError("push_incomplete", "safe", 6, details))
    encoded = json.dumps(payload)
    assert set(details) - {"stderr", "path", "url"} <= set(payload["error"]["details"])
    assert not {"stderr", "path", "url"}.intersection(payload["error"]["details"])
    assert "secret" not in encoded


def test_safe_archive_parent_validates_all_ancestors_before_creation(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"; blocker.write_text("x", encoding="utf-8")
    target = blocker / "outside" / "archives"
    with pytest.raises(SyncError) as error:
        cli._safe_archive_parent(target)
    assert error.value.code == "unsafe_archive_path"
    assert not (blocker / "outside").exists()


def test_bootstrap_crash_after_prepare_resumes_same_timestamped_oid_without_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    source = tmp_path / "cloud"; source.mkdir()
    live = tmp_path / "live"; root_hash = "a" * 64
    config = SimpleNamespace(save_root=live, machine_id="t6", stable_scan_retries=1, stable_window_seconds=0)
    state: list[cli.SyncState] = []
    mutation_calls: list[str] = []

    class Runner:
        def run(self, *args, **_kwargs):
            return SimpleNamespace(returncode=0, stdout=(state[0].local_commit if state else ""))

    class Vault:
        runner = Runner()
        snapshots = 0
        def preflight(self): pass
        def remote_oid(self): return None
        def snapshot(self, *_a, **_k):
            self.snapshots += 1
            mutation_calls.append(f"snapshot:{self.snapshots}")
            # A second snapshot would deliberately produce another OID.
            return f"{1_800_000_000 + self.snapshots:040x}"
        def validate_commit_snapshot(self, _oid):
            mutation_calls.append("validate")
            return {"root_hash": root_hash}
        def extract_save(self, _oid, destination, **_kwargs):
            mutation_calls.append("extract")
            destination.mkdir(parents=True)

    vault = Vault()
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: vault)
    monkeypatch.setattr(cli, "load_state", lambda _: state[0] if state else (_ for _ in ()).throw(SyncError("state_missing", "missing", 6)))
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": root_hash})
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True})
    monkeypatch.setattr(cli, "_copy_verified", lambda *_a: mutation_calls.append("archive"))
    def restore(_source, destination, *_a, **kwargs):
        if not kwargs["apply"]:
            return {"dry_run": True}
        destination.mkdir(exist_ok=True)
        (destination / "player.gdc").write_bytes(b"pending")
        mutation_calls.append("apply")
        return {"dry_run": False, "root_hash": root_hash}
    monkeypatch.setattr(cli, "restore_from_directory", restore)
    def crash_after_prepare(_vault, machine, oid, root, **_kwargs):
        state.append(cli.SyncState(
            machine_id=machine, phase="bootstrap_pending", local_commit=oid,
            last_applied_manifest_root_hash=root, bootstrap_live_applied=False,
        ))
        raise SyncError("simulated_crash", "safe", EXIT_RECOVERY_REQUIRED)
    monkeypatch.setattr(cli, "prepare_bootstrap", crash_after_prepare)
    with pytest.raises(SyncError, match="safe"):
        cli.bootstrap(tmp_path / "config.local.json", source, apply=True)
    prepared_oid = state[0].local_commit
    assert vault.snapshots == 1 and not live.exists()

    monkeypatch.setattr(cli, "prepare_bootstrap", lambda *_a, **_k: pytest.fail("resume must not prepare again"))
    monkeypatch.setattr(cli, "mark_bootstrap_live_applied", lambda *_a, **_k: mutation_calls.append("mark"))
    monkeypatch.setattr(cli, "push_bootstrap", lambda *_a, **_k: mutation_calls.append("push") or prepared_oid)
    before_resume = vault.snapshots
    result = cli.bootstrap(tmp_path / "config.local.json", source, apply=True)
    assert result["commit"] == prepared_oid
    assert vault.snapshots == before_resume == 1
    assert vault.snapshots - before_resume == 0
    assert mutation_calls.count("snapshot:1") == 1
    assert mutation_calls[-5:] == ["validate", "extract", "apply", "mark", "push"]


def test_bootstrap_resume_creates_only_safe_state_staging_parent_before_extract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A pending bootstrap may resume before any state-local staging exists."""
    root_hash = "a" * 64; oid = "b" * 40
    config_path = tmp_path / "state" / "config.local.json"
    config = SimpleNamespace(
        save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1,
        stable_window_seconds=0,
    )
    pending = cli.SyncState(
        machine_id="t6", phase="bootstrap_pending", local_commit=oid,
        last_applied_manifest_root_hash=root_hash, bootstrap_live_applied=False,
    )
    calls: list[str] = []

    class Vault:
        runner = SimpleNamespace(run=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=oid))
        def remote_oid(self): return None
        def validate_commit_snapshot(self, actual_oid):
            assert actual_oid == oid; calls.append("validate")
            return {"root_hash": root_hash}
        def extract_save(self, actual_oid, destination, **_kwargs):
            assert actual_oid == oid
            assert destination.parent == config_path.parent / "staging"
            assert destination.parent.is_dir() and not destination.parent.is_symlink()
            calls.append("extract")
            destination.mkdir()
            return destination

    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": root_hash})
    monkeypatch.setattr(cli, "restore_from_directory", lambda _source, destination, *_a, **_k: (destination.mkdir(), calls.append("apply"), {"root_hash": root_hash})[-1])
    monkeypatch.setattr(cli, "mark_bootstrap_live_applied", lambda *_a, **_k: calls.append("mark"))
    monkeypatch.setattr(cli, "push_bootstrap", lambda *_a, **_k: calls.append("push") or oid)

    result = cli._resume_bootstrap_cli(config_path, config, Vault(), pending, {"root_hash": root_hash})
    assert result["commit"] == oid
    assert calls == ["validate", "extract", "apply", "mark", "push"]
    assert config.save_root.is_dir()


def test_bootstrap_resume_rejects_unmanaged_staging_parent_before_live_or_push(
    tmp_path: Path,
) -> None:
    root_hash = "a" * 64; oid = "b" * 40
    config_path = tmp_path / "state" / "config.local.json"; config_path.parent.mkdir()
    (config_path.parent / "staging").write_text("not a directory", encoding="utf-8")
    config = SimpleNamespace(
        save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1,
        stable_window_seconds=0,
    )
    pending = cli.SyncState(
        machine_id="t6", phase="bootstrap_pending", local_commit=oid,
        last_applied_manifest_root_hash=root_hash, bootstrap_live_applied=False,
    )
    calls: list[str] = []

    class Vault:
        runner = SimpleNamespace(run=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=oid))
        def remote_oid(self): calls.append("remote"); return None
        def validate_commit_snapshot(self, _oid): calls.append("validate"); return {"root_hash": root_hash}
        def extract_save(self, *_a, **_k): calls.append("extract")

    with pytest.raises(SyncError) as error:
        cli._resume_bootstrap_cli(config_path, config, Vault(), pending, {"root_hash": root_hash})
    assert error.value.code == "unsafe_archive_path"
    assert calls == ["validate", "remote"]
    assert not config.save_root.exists()
    assert pending.local_commit == oid and pending.bootstrap_live_applied is False


def test_bootstrap_resume_rejects_reparse_staging_parent_before_live_or_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root_hash = "a" * 64; oid = "b" * 40
    config_path = tmp_path / "state" / "config.local.json"; config_path.parent.mkdir()
    staging = config_path.parent / "staging"; staging.mkdir()
    config = SimpleNamespace(
        save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1,
        stable_window_seconds=0,
    )
    pending = cli.SyncState(machine_id="t6", phase="bootstrap_pending", local_commit=oid,
                            last_applied_manifest_root_hash=root_hash, bootstrap_live_applied=False)
    calls: list[str] = []; original_lstat = Path.lstat
    def reparse_lstat(path: Path):
        if path == staging:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original_lstat(path)

    class Vault:
        runner = SimpleNamespace(run=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=oid))
        def remote_oid(self): calls.append("remote"); return None
        def validate_commit_snapshot(self, _oid): calls.append("validate"); return {"root_hash": root_hash}
        def extract_save(self, *_a, **_k): calls.append("extract")

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    with pytest.raises(SyncError) as error:
        cli._resume_bootstrap_cli(config_path, config, Vault(), pending, {"root_hash": root_hash})
    assert error.value.code == "unsafe_archive_path"
    assert calls == ["validate", "remote"] and not config.save_root.exists()
    assert pending.local_commit == oid and pending.bootstrap_live_applied is False


def test_bootstrap_resume_accepts_existing_empty_safe_staging_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root_hash = "a" * 64; oid = "b" * 40
    config_path = tmp_path / "state" / "config.local.json"; staging = config_path.parent / "staging"
    staging.mkdir(parents=True)
    config = SimpleNamespace(save_root=tmp_path / "live", machine_id="t6", stable_scan_retries=1, stable_window_seconds=0)
    pending = cli.SyncState(machine_id="t6", phase="bootstrap_pending", local_commit=oid,
                            last_applied_manifest_root_hash=root_hash, bootstrap_live_applied=False)
    calls: list[str] = []

    class Vault:
        runner = SimpleNamespace(run=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=oid))
        def remote_oid(self): return None
        def validate_commit_snapshot(self, _oid): return {"root_hash": root_hash}
        def extract_save(self, _oid, destination, **_k):
            assert destination.parent == staging and destination.parent.is_dir()
            calls.append("extract"); destination.mkdir(); return destination

    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": root_hash})
    monkeypatch.setattr(cli, "restore_from_directory", lambda _source, destination, *_a, **_k: (destination.mkdir(), calls.append("restore"), {"root_hash": root_hash})[-1])
    monkeypatch.setattr(cli, "mark_bootstrap_live_applied", lambda *_a, **_k: calls.append("mark"))
    monkeypatch.setattr(cli, "push_bootstrap", lambda *_a, **_k: calls.append("push") or oid)
    assert cli._resume_bootstrap_cli(config_path, config, Vault(), pending, {"root_hash": root_hash})["commit"] == oid
    assert calls == ["extract", "restore", "mark", "push"]


def test_bootstrap_retry_wrong_cloud_source_is_rejected_before_vault_or_live_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    source = tmp_path / "cloud"; source.mkdir()
    live = tmp_path / "live"
    pending = cli.SyncState(
        machine_id="t6", phase="bootstrap_pending", local_commit="b" * 40,
        last_applied_manifest_root_hash="a" * 64, bootstrap_live_applied=False,
    )
    config = SimpleNamespace(save_root=live, machine_id="t6", stable_scan_retries=1, stable_window_seconds=0)
    mutations: list[str] = []
    class Vault:
        def preflight(self): pass
        def snapshot(self, *_a, **_k): mutations.append("snapshot")
        def validate_commit_snapshot(self, *_a): mutations.append("validate")
        def extract_save(self, *_a, **_k): mutations.append("extract")
        runner = SimpleNamespace(run=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="b" * 40))
    monkeypatch.setattr(cli, "load_config", lambda _: config)
    monkeypatch.setattr(cli, "_vault", lambda _: Vault())
    monkeypatch.setattr(cli, "load_state", lambda _: pending)
    monkeypatch.setattr(cli, "stable_manifest", lambda *_a, **_k: {"root_hash": "c" * 64})
    monkeypatch.setattr(cli, "validate_players", lambda *_a: {"ok": True})
    monkeypatch.setattr(cli, "_copy_verified", lambda *_a: mutations.append("archive"))
    monkeypatch.setattr(cli, "prepare_bootstrap", lambda *_a, **_k: mutations.append("prepare"))
    monkeypatch.setattr(cli, "mark_bootstrap_live_applied", lambda *_a, **_k: mutations.append("mark"))
    monkeypatch.setattr(cli, "push_bootstrap", lambda *_a, **_k: mutations.append("push"))
    monkeypatch.setattr(cli, "restore_from_directory", lambda *_a, **_k: {"dry_run": True})
    with pytest.raises(SyncError) as error:
        cli.bootstrap(tmp_path / "config.local.json", source, apply=True)
    assert error.value.code == "bootstrap_recovery_mismatch"
    assert mutations == []
    assert not live.exists()
    assert pending.local_commit == "b" * 40 and pending.bootstrap_live_applied is False
