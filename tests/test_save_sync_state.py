from __future__ import annotations

import json
import os
from types import SimpleNamespace
from pathlib import Path

import pytest

from grim_dawn_sync.errors import EXIT_RECOVERY_REQUIRED, SyncError
from grim_dawn_sync.state import SyncState, load_state, parse_state, save_state, save_state_if_unchanged


def _active(**changes: str | None) -> SyncState:
    session = "00000000-0000-4000-8000-000000000001"
    fields: dict[str, str | None] = {
        "session_id": session,
        "machine_id": "machine",
        "base_commit": "a" * 40,
        "lock_oid": "b" * 40,
        "local_tag": f"grim-dawn-sync-{session}",
        "phase": "lock_held",
    }
    fields.update(changes)
    return SyncState(**fields)


def test_legacy_state_loads_and_is_upgraded_on_next_save(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    legacy = {
        "schema_version": "1.0.0",
        "last_applied_remote_commit": "a" * 40,
        "last_applied_manifest_root_hash": "b" * 64,
        "session_id": None,
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = load_state(path)
    assert loaded == SyncState(
        last_applied_remote_commit="a" * 40,
        last_applied_manifest_root_hash="b" * 64,
    )
    save_state(path, loaded)
    assert set(json.loads(path.read_text(encoding="utf-8"))) == set(loaded.as_dict())


def test_missing_state_is_empty_only_for_semantic_cas(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    active = _active()
    save_state_if_unchanged(path, SyncState(), active)
    assert load_state(path) == active

    other = tmp_path / "other.json"
    known = SyncState(
        last_applied_remote_commit="a" * 40,
        last_applied_manifest_root_hash="b" * 64,
        machine_id="machine",
    )
    with pytest.raises(SyncError) as caught:
        save_state_if_unchanged(other, known, active)
    assert caught.value.code == "selection_stale"
    assert not other.exists()


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (_active(machine_id=None), "missing session"),
        (_active(local_commit="c" * 40), "must not contain commit"),
        (_active(phase="committed"), "local commit"),
        (_active(phase="pushed", local_commit="c" * 40, pushed_commit="d" * 40), "matching"),
        (_active(phase="release_pending"), "pushed commit"),
        (SyncState(session_id="orphan"), "Inactive state"),
    ],
)
def test_current_state_transition_invariants_fail_closed(state: SyncState, message: str) -> None:
    with pytest.raises(SyncError, match=message):
        parse_state(state.as_dict())


def test_valid_commit_phases_round_trip(tmp_path: Path) -> None:
    commit = "c" * 40
    states = [
        SyncState(
            last_applied_manifest_root_hash="d" * 64,
            machine_id="machine",
            phase="bootstrap_pending",
            local_commit=commit,
        ),
        SyncState(
            last_applied_manifest_root_hash="d" * 64,
            machine_id="machine",
            phase="bootstrap_pending",
            local_commit=commit,
            bootstrap_live_applied=True,
        ),
        _active(),
        _active(phase="committed", local_commit=commit),
        _active(phase="pushed", local_commit=commit, pushed_commit=commit),
        _active(phase="release_pending", pushed_commit=commit),
        SyncState(last_applied_remote_commit=commit),
    ]
    for index, state in enumerate(states):
        path = tmp_path / f"state-{index}.json"
        save_state(path, state)
        assert load_state(path) == state


def test_inactive_machine_baseline_requires_complete_snapshot_and_round_trips(tmp_path: Path) -> None:
    baseline = SyncState(last_applied_remote_commit="c" * 40, last_applied_manifest_root_hash="d" * 64, machine_id="machine")
    path = tmp_path / "baseline.json"; save_state(path, baseline)
    assert load_state(path) == baseline
    with pytest.raises(SyncError, match="baseline requires"):
        parse_state(SyncState(machine_id="machine").as_dict())
    with pytest.raises(SyncError, match="Inactive state"):
        parse_state(SyncState(last_applied_remote_commit="c" * 40, last_applied_manifest_root_hash="d" * 64, machine_id="machine", session_id="orphan").as_dict())


@pytest.mark.parametrize(
    "state",
    [
        SyncState(machine_id="machine", phase="bootstrap_pending", local_commit="c" * 40),
        SyncState(
            last_applied_manifest_root_hash="d" * 64,
            phase="bootstrap_pending",
            local_commit="c" * 40,
        ),
        SyncState(
            last_applied_manifest_root_hash="d" * 64,
            machine_id="machine",
            phase="bootstrap_pending",
            local_commit="c" * 40,
            pushed_commit="c" * 40,
        ),
        SyncState(
            last_applied_remote_commit="c" * 40,
            last_applied_manifest_root_hash="d" * 64,
            machine_id="machine",
            phase="bootstrap_pending",
            local_commit="c" * 40,
        ),
    ],
)
def test_bootstrap_pending_requires_only_machine_commit_and_root(state: SyncState) -> None:
    with pytest.raises(SyncError, match="Bootstrap-pending"):
        parse_state(state.as_dict())


def test_pre_live_marker_current_shape_remains_backward_compatible(tmp_path: Path) -> None:
    state = SyncState(
        last_applied_manifest_root_hash="d" * 64,
        machine_id="machine",
        phase="bootstrap_pending",
        local_commit="c" * 40,
    )
    old_shape = state.as_dict()
    old_shape.pop("bootstrap_live_applied")
    parsed = parse_state(old_shape)
    assert parsed == state
    path = tmp_path / "upgraded.json"
    save_state(path, parsed)
    assert json.loads(path.read_text(encoding="utf-8"))["bootstrap_live_applied"] is False


def test_live_marker_is_boolean_and_bootstrap_only() -> None:
    invalid_type = SyncState(
        last_applied_manifest_root_hash="d" * 64,
        machine_id="machine",
        phase="bootstrap_pending",
        local_commit="c" * 40,
    ).as_dict()
    invalid_type["bootstrap_live_applied"] = "true"
    with pytest.raises(SyncError, match="boolean"):
        parse_state(invalid_type)
    with pytest.raises(SyncError, match="Inactive state"):
        parse_state(SyncState(bootstrap_live_applied=True).as_dict())


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":"1.0.0","schema_version":"1.0.0"}',
        "[]",
        "{broken",
        "\ufeff{}",
    ],
)
def test_load_normalizes_invalid_json_and_shape_to_recovery_error(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(SyncError) as caught:
        load_state(path)
    assert caught.value.code == "state_corrupt"
    assert caught.value.exit_code == EXIT_RECOVERY_REQUIRED


@pytest.mark.parametrize("relative", ["missing.json", "missing-parent/state.json"])
def test_load_missing_is_distinct_recovery_error(tmp_path: Path, relative: str) -> None:
    with pytest.raises(SyncError) as caught:
        load_state(tmp_path / relative)
    assert caught.value.code == "state_missing"
    assert caught.value.exit_code == EXIT_RECOVERY_REQUIRED


def test_replace_failure_preserves_old_state_and_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    old = SyncState(last_applied_remote_commit="a" * 40)
    new = SyncState(last_applied_remote_commit="b" * 40)
    save_state(path, old)
    original = path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(SyncError) as caught:
        save_state(path, new)
    assert caught.value.code == "state_write_failed"
    assert caught.value.exit_code == EXIT_RECOVERY_REQUIRED
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_invalid_dataclass_is_rejected_before_creating_parent(tmp_path: Path) -> None:
    path = tmp_path / "new-parent" / "state.json"
    with pytest.raises(SyncError, match="missing session"):
        save_state(path, _active(machine_id=None))
    assert not path.parent.exists()


def test_parent_symlink_is_rejected_for_load_and_save(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    path = link / "state.json"
    (real / "state.json").write_text(json.dumps(SyncState().as_dict()), encoding="utf-8")
    with pytest.raises(SyncError) as load_error:
        load_state(path)
    assert load_error.value.code == "state_corrupt"
    with pytest.raises(SyncError) as save_error:
        save_state(path, SyncState())
    assert save_error.value.code == "unsafe_state_path"


def test_destination_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("sentinel", encoding="utf-8")
    link = tmp_path / "state.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("file symlink creation is unavailable")
    with pytest.raises(SyncError) as caught:
        save_state(link, SyncState())
    assert caught.value.code == "unsafe_state_path"
    assert target.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.parametrize("target_kind", ["parent", "destination"])
def test_mocked_windows_reparse_flag_is_rejected_for_load_and_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_kind: str
) -> None:
    path = tmp_path / "state.json"; save_state(path, SyncState())
    original_lstat = Path.lstat
    marked = path.parent if target_kind == "parent" else path
    def flagged(candidate: Path):
        result = original_lstat(candidate)
        if candidate == marked:
            values = {name: getattr(result, name) for name in dir(result) if name.startswith("st_")}
            values["st_mode"] = result.st_mode; values["st_file_attributes"] = 0x400
            return SimpleNamespace(**values)
        return result
    monkeypatch.setattr(Path, "lstat", flagged)
    monkeypatch.setattr(Path, "is_symlink", lambda _: False)
    with pytest.raises(SyncError) as load_error: load_state(path)
    assert load_error.value.code == "state_corrupt"
    with pytest.raises(SyncError) as save_error: save_state(path, SyncState())
    assert save_error.value.code == "unsafe_state_path"
