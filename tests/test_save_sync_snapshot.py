import json
from pathlib import Path
from unittest.mock import patch
import pytest

from grim_dawn_sync.errors import SyncError
from grim_dawn_sync.snapshot import apply_restore, plan_restore, restore_from_directory


def valid(_: Path, __: dict) -> dict: return {"ok": True}

def tree(root: Path, value: bytes) -> None:
    (root / "main/a").mkdir(parents=True); (root / "main/a/player.gdc").write_bytes(value)

def restore(source: Path, live: Path, tmp_path: Path, **kwargs):
    return restore_from_directory(source, live, tmp_path / "archives", tmp_path / "state", machine_id="test", validator=valid, **kwargs)


def test_dry_run_has_no_mutation(tmp_path: Path) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    assert restore(source, live, tmp_path)["dry_run"]
    assert (live / "main/a/player.gdc").read_bytes() == b"old"
    assert not (tmp_path / "archives").exists() and not (tmp_path / "state").exists()


def test_apply_archives_then_swaps(tmp_path: Path) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    result = restore(source, live, tmp_path, apply=True)
    assert (live / "main/a/player.gdc").read_bytes() == b"new"
    assert result["archive_created"] is True
    assert next((tmp_path / "archives").iterdir()).joinpath("main/a/player.gdc").read_bytes() == b"old"


def test_journal_keeps_recovery_paths_through_every_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    import grim_dawn_sync.snapshot as module
    original = module._write_journal; payloads = []
    def record(root, payload):
        payloads.append(dict(payload)); return original(root, payload)
    monkeypatch.setattr(module, "_write_journal", record)
    restore(source, live, tmp_path, apply=True)
    assert [item["phase"] for item in payloads] == ["prepared", "live_parked", "promoted", "complete"]
    for item in payloads:
        assert {"source", "live", "stage", "rollback", "archive"} <= set(item)
    persisted = json.loads((tmp_path / "state/restore-journal.json").read_text(encoding="utf-8"))
    assert persisted["phase"] == "complete" and persisted["live"] == str(live)


def test_dangling_live_symlink_is_unsafe_not_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new")
    original_lstat, original_link = Path.lstat, Path.is_symlink
    class Reparse: st_file_attributes = 0x400
    monkeypatch.setattr(Path, "lstat", lambda path: Reparse() if path == live else original_lstat(path))
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == live or original_link(path))
    with pytest.raises(SyncError) as error:
        restore(source, live, tmp_path, apply=True)
    assert error.value.code == "unsafe_save_tree"


def test_journal_prepared_failure_leaves_live_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    import grim_dawn_sync.snapshot as module
    monkeypatch.setattr(module, "_write_journal", lambda *_: (_ for _ in ()).throw(SyncError("journal_write_failed", "x", 6)))
    with pytest.raises(SyncError): restore(source, live, tmp_path, apply=True)
    assert (live / "main/a/player.gdc").read_bytes() == b"old"


def test_archive_publish_failure_leaves_live_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    import grim_dawn_sync.snapshot as module
    original = module.os.rename
    def fail_archive(a, b):
        if Path(a).name.startswith(".save-sync-archive-stage"): raise OSError("no")
        return original(a, b)
    monkeypatch.setattr(module.os, "rename", fail_archive)
    with pytest.raises(SyncError, match="Archive") as error: restore(source, live, tmp_path, apply=True)
    assert error.value.code == "archive_publish_failed" and (live / "main/a/player.gdc").read_bytes() == b"old"


def test_live_missing_bootstrap_apply(tmp_path: Path) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new")
    restore(source, live, tmp_path, apply=True)
    assert (live / "main/a/player.gdc").read_bytes() == b"new"


def test_empty_directories_survive_archive_and_restore(tmp_path: Path) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    (source / "empty-mod").mkdir(); (live / "empty-old").mkdir()
    restore(source, live, tmp_path, apply=True)
    assert (live / "empty-mod").is_dir()
    archive = next((tmp_path / "archives").iterdir())
    assert (archive / "empty-old").is_dir()


def test_source_and_live_changes_after_plan_are_rejected(tmp_path: Path) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    plan = plan_restore(source, live, tmp_path / "archives", tmp_path / "state", machine_id="test", validator=valid)
    (source / "extra").write_bytes(b"changed")
    with pytest.raises(SyncError, match="source changed"):
        apply_restore(plan, tmp_path / "state", machine_id="test", validator=valid)
    assert (live / "main/a/player.gdc").read_bytes() == b"old"
    plan = plan_restore(live, source, tmp_path / "archives2", tmp_path / "state2", machine_id="test", validator=valid)
    (source / "extra-live").write_bytes(b"changed")
    with pytest.raises(SyncError, match="Live save changed"):
        apply_restore(plan, tmp_path / "state2", machine_id="test", validator=valid)


def test_copy_rejects_nested_unsafe_tree_and_post_copy_source_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    import grim_dawn_sync.snapshot as module
    original_tree = module._safe_tree
    monkeypatch.setattr(module, "_safe_tree", lambda path: (_ for _ in ()).throw(SyncError("unsafe_save_tree", "x", 3)) if path == source else original_tree(path))
    with pytest.raises(SyncError, match="unsafe"):
        restore(source, live, tmp_path, apply=True)
    monkeypatch.setattr(module, "_safe_tree", original_tree)
    original_manifest = module.stable_manifest; calls = 0
    def mutate_after_copy(path, **kwargs):
        nonlocal calls
        result = original_manifest(path, **kwargs); calls += 1
        if path == source and calls >= 3: (source / "changed-after-copy").write_bytes(b"x")
        return result
    monkeypatch.setattr(module, "stable_manifest", mutate_after_copy)
    with pytest.raises(SyncError, match="source changed"):
        restore(source, live, tmp_path / "two", apply=True)


def test_archive_collision_parent_and_live_parent_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    from grim_dawn_sync import snapshot
    original = snapshot._copy_verified
    def create_final_then_copy(*args, **kwargs):
        if args[1].name.startswith(".save-sync-archive-stage"):
            args[1].parent.joinpath(args[1].name.replace(".save-sync-archive-stage-", "save-before-restore-")).mkdir()
        return original(*args, **kwargs)
    monkeypatch.setattr(snapshot, "_copy_verified", create_final_then_copy)
    with pytest.raises(SyncError, match="Archive") as error: restore(source, live, tmp_path, apply=True)
    assert error.value.code == "archive_publish_failed" and (live / "main/a/player.gdc").read_bytes() == b"old"
    monkeypatch.setattr(snapshot, "_copy_verified", original)
    missing_live = tmp_path / "missing-parent" / "live"
    plan = plan_restore(source, missing_live, tmp_path / "archives-parent", tmp_path / "state-parent", machine_id="test", validator=valid)
    original_stat = snapshot.os.stat
    def unavailable(path, *args, **kwargs):
        if Path(path) == missing_live.parent: raise OSError("gone")
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(snapshot.os, "stat", unavailable)
    with pytest.raises(SyncError) as error: apply_restore(plan, tmp_path / "state-parent", machine_id="test", validator=valid)
    assert error.value.code == "live_parent_unavailable"


def test_journal_phase_failures_and_private_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    import grim_dawn_sync.snapshot as module
    original = module._write_journal
    monkeypatch.setattr(module, "_write_journal", lambda *_: (_ for _ in ()).throw(SyncError("journal_write_failed", "secret", 6)))
    with pytest.raises(SyncError) as error: restore(source, live, tmp_path, apply=True)
    assert error.value.code == "journal_write_failed" and error.value.exit_code == 3
    monkeypatch.setattr(module, "_write_journal", original)
    calls = []
    def fail_promoted(root, payload):
        calls.append(payload["phase"])
        if payload["phase"] == "promoted": raise SyncError("journal_write_failed", "secret", 6)
        return original(root, payload)
    monkeypatch.setattr(module, "_write_journal", fail_promoted)
    with pytest.raises(SyncError) as error: restore(source, live, tmp_path / "promoted", apply=True)
    assert error.value.code == "journal_write_failed_after_promote" and error.value.exit_code == 6
    assert "private" not in str(error.value.details) and calls == ["prepared", "live_parked", "promoted"]


def test_live_parked_journal_failure_rolls_back_and_post_promote_mismatch_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    import grim_dawn_sync.snapshot as module
    original_write = module._write_journal
    def fail_parked(root, payload):
        if payload["phase"] == "live_parked": raise SyncError("journal_write_failed", "x", 6)
        return original_write(root, payload)
    monkeypatch.setattr(module, "_write_journal", fail_parked)
    with pytest.raises(SyncError) as error: restore(source, live, tmp_path, apply=True)
    assert error.value.code == "journal_write_failed_after_park" and error.value.exit_code == 3
    assert (live / "main/a/player.gdc").read_bytes() == b"old"
    monkeypatch.setattr(module, "_write_journal", original_write)
    original_manifest = module._manifest; live_calls = 0
    def mismatch_after_promotion(path, *args, **kwargs):
        nonlocal live_calls
        result = original_manifest(path, *args, **kwargs)
        if path == live:
            live_calls += 1
            if live_calls >= 3: return {**result, "root_hash": "0" * 64}
        return result
    monkeypatch.setattr(module, "_manifest", mismatch_after_promotion)
    with pytest.raises(SyncError) as error: restore(source, live, tmp_path / "mismatch", apply=True)
    assert error.value.code == "recovery_required" and error.value.exit_code == 6


def test_copy_mismatch_leaves_live_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    from grim_dawn_sync import snapshot
    original = snapshot._copy_verified
    def mismatch(*args, **kwargs):
        if args[1].name.startswith(".save-sync-stage"): raise SyncError("snapshot_hash_mismatch", "x", 3)
        return original(*args, **kwargs)
    monkeypatch.setattr(snapshot, "_copy_verified", mismatch)
    with pytest.raises(SyncError) as error: restore(source, live, tmp_path, apply=True)
    assert error.value.code == "snapshot_hash_mismatch" and (live / "main/a/player.gdc").read_bytes() == b"old"


def test_promote_failure_rolls_back_and_rollback_failure_is_recoverable(tmp_path: Path) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    import grim_dawn_sync.snapshot as module
    original = module.os.rename
    def promote_fail(a, b):
        if Path(a).name.startswith(".save-sync-stage"): raise OSError("no")
        return original(a, b)
    with patch("grim_dawn_sync.snapshot.os.rename", side_effect=promote_fail):
        with pytest.raises(SyncError) as error: restore(source, live, tmp_path, apply=True)
    assert error.value.code == "swap_failed_rolled_back" and (live / "main/a/player.gdc").read_bytes() == b"old"
    def live_rename_only(a, b):
        if Path(a) == live: raise OSError("no")
        return original(a, b)
    with patch("grim_dawn_sync.snapshot.os.rename", side_effect=live_rename_only):
        with pytest.raises(SyncError) as error: restore(source, live, tmp_path / "two", apply=True)
    assert error.value.code == "live_rename_failed"


def test_live_rename_failure_and_name_collision_leave_live_unchanged(tmp_path: Path) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    import grim_dawn_sync.snapshot as module
    original = module.os.rename
    def live_rename_only(a, b):
        if Path(a) == live: raise OSError("no")
        return original(a, b)
    with patch("grim_dawn_sync.snapshot.os.rename", side_effect=live_rename_only):
        with pytest.raises(SyncError) as error: restore(source, live, tmp_path, apply=True)
    assert error.value.code == "live_rename_failed" and (live / "main/a/player.gdc").read_bytes() == b"old"
    session = "collision"
    (live.parent / f".save-sync-stage-{session}").mkdir()
    from grim_dawn_sync.snapshot import plan_restore
    with pytest.raises(SyncError) as error:
        plan_restore(source, live, tmp_path / "archives", tmp_path / "state", machine_id="test", session_id=session, validator=valid)
    assert error.value.code == "snapshot_name_collision"


def test_rollback_failure_keeps_recovery_artifacts(tmp_path: Path) -> None:
    source, live = tmp_path / "source", tmp_path / "live"; tree(source, b"new"); tree(live, b"old")
    import grim_dawn_sync.snapshot as module
    original = module.os.rename
    def fail_promote_and_rollback(a, b):
        if Path(a).name.startswith(".save-sync-stage") or Path(a).name.startswith(".save-sync-rollback"):
            raise OSError("no")
        return original(a, b)
    with patch("grim_dawn_sync.snapshot.os.rename", side_effect=fail_promote_and_rollback):
        with pytest.raises(SyncError) as error: restore(source, live, tmp_path, apply=True)
    assert error.value.code == "recovery_required"
    assert (tmp_path / "state/restore-journal.json").exists()
    assert any(path.name.startswith(".save-sync-") for path in tmp_path.iterdir())
