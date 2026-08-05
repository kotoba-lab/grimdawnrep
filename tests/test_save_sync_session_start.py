"""Unit tests for session-start local snapshot archives (T-D, plan section 6)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import uuid

import pytest

from grim_dawn_sync.errors import SyncError
from grim_dawn_sync.manifest import build_manifest
from grim_dawn_sync.session_start import (
    SESSION_START_ID_PATTERN,
    SESSION_START_METADATA_NAME,
    create_session_start_archive,
    extract_session_start_archive,
    read_session_start_metadata,
    scan_session_start_archives,
    session_start_archive_id,
    session_start_archive_manifest,
    session_start_usage,
    write_session_start_metadata,
)
from test_save_sync_git_vault import save


def _manifest_of(root: Path, machine_id: str = "m") -> dict:
    return build_manifest(root, machine_id=machine_id)


def test_create_archive_round_trip_id_pattern_and_metadata(tmp_path: Path) -> None:
    live = save(tmp_path / "live", b"payload")
    manifest = _manifest_of(live)
    archives = tmp_path / "archives"
    archive_id = create_session_start_archive(
        archives, live, manifest=manifest, machine_id="m", session_id=str(uuid.uuid4()),
        launched_from_candidate_kind="remote_head",
    )
    assert SESSION_START_ID_PATTERN.fullmatch(archive_id)
    destination = archives / archive_id
    assert destination.is_dir()
    assert (destination / "main" / "a" / "player.gdc").read_bytes() == b"payload"
    metadata = read_session_start_metadata(destination)
    assert metadata["root_hash"] == manifest["root_hash"]
    assert metadata["kind"] == "grim_dawn_session_start_snapshot"
    assert metadata["launched_from_candidate_kind"] == "remote_head"
    # No incomplete staging directories are left behind after a clean publish.
    assert not any(item.name.startswith(".session-start-incomplete-") for item in archives.iterdir())


def test_create_archive_rejects_name_collision(tmp_path: Path) -> None:
    live = save(tmp_path / "live", b"x")
    manifest = _manifest_of(live)
    archives = tmp_path / "archives"
    archive_id = create_session_start_archive(
        archives, live, manifest=manifest, machine_id="m", session_id=str(uuid.uuid4()),
        launched_from_candidate_kind="live",
    )
    # Force an exact-name collision by pre-creating the same destination.
    root_hash = manifest["root_hash"]
    forced = archives / f"save-session-start-{root_hash[:16]}-{'0' * 32}"
    forced.mkdir()
    import grim_dawn_sync.session_start as ss
    original = uuid.uuid4
    ss.uuid.uuid4 = lambda: uuid.UUID("0" * 32)  # type: ignore[assignment]
    try:
        with pytest.raises(SyncError) as caught:
            create_session_start_archive(
                archives, live, manifest=manifest, machine_id="m", session_id=str(uuid.uuid4()),
                launched_from_candidate_kind="live",
            )
    finally:
        ss.uuid.uuid4 = original
    assert caught.value.code == "snapshot_name_collision"


def test_scan_finds_valid_archive_and_ignores_incomplete_and_foreign_names(tmp_path: Path) -> None:
    live = save(tmp_path / "live", b"a")
    manifest = _manifest_of(live)
    archives = tmp_path / "archives"
    archive_id = create_session_start_archive(
        archives, live, manifest=manifest, machine_id="m", session_id=str(uuid.uuid4()),
        launched_from_candidate_kind="live",
    )
    (archives / "save-before-restore-not-a-session-start").mkdir()
    (archives / ".session-start-incomplete-deadbeef").mkdir()
    found = scan_session_start_archives(archives, machine_id="m")
    assert [item[0] for item in found] == [archive_id]
    _, found_manifest, found_meta = found[0]
    assert found_manifest["root_hash"] == manifest["root_hash"]
    assert found_meta["root_hash"] == manifest["root_hash"]


def test_scan_skips_archive_whose_content_was_tampered_after_publish(tmp_path: Path) -> None:
    live = save(tmp_path / "live", b"a")
    manifest = _manifest_of(live)
    archives = tmp_path / "archives"
    archive_id = create_session_start_archive(
        archives, live, manifest=manifest, machine_id="m", session_id=str(uuid.uuid4()),
        launched_from_candidate_kind="live",
    )
    (archives / archive_id / "main" / "a" / "player.gdc").write_bytes(b"tampered")
    assert scan_session_start_archives(archives, machine_id="m") == []


def test_scan_skips_archive_missing_metadata(tmp_path: Path) -> None:
    live = save(tmp_path / "live", b"a")
    manifest = _manifest_of(live)
    archives = tmp_path / "archives"
    archive_id = create_session_start_archive(
        archives, live, manifest=manifest, machine_id="m", session_id=str(uuid.uuid4()),
        launched_from_candidate_kind="live",
    )
    (archives / archive_id / SESSION_START_METADATA_NAME).unlink()
    assert scan_session_start_archives(archives, machine_id="m") == []


@pytest.mark.skipif(os.name != "nt", reason="junction/reparse-point rejection is Windows-specific")
def test_scan_skips_archive_reached_through_a_junction(tmp_path: Path) -> None:
    import subprocess
    live = save(tmp_path / "live", b"a")
    manifest = _manifest_of(live)
    real_archives = tmp_path / "real-archives"
    archive_id = create_session_start_archive(
        real_archives, live, manifest=manifest, machine_id="m", session_id=str(uuid.uuid4()),
        launched_from_candidate_kind="live",
    )
    junctioned_archives = tmp_path / "archives"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junctioned_archives), str(real_archives)], check=True, capture_output=True)
    assert scan_session_start_archives(junctioned_archives, machine_id="m") == []
    # The archive_id itself, reached via the junction, must also be rejected.
    assert scan_session_start_archives(real_archives, machine_id="m") != []


def test_scan_returns_empty_for_missing_or_symlinked_root(tmp_path: Path) -> None:
    assert scan_session_start_archives(tmp_path / "does-not-exist", machine_id="m") == []


def test_usage_counts_only_matching_valid_looking_directories(tmp_path: Path) -> None:
    archives = tmp_path / "archives"
    live = save(tmp_path / "live", b"12345")
    manifest = _manifest_of(live)
    create_session_start_archive(
        archives, live, manifest=manifest, machine_id="m", session_id=str(uuid.uuid4()),
        launched_from_candidate_kind="live",
    )
    (archives / "save-before-restore-ignored").mkdir()
    (archives / "save-before-restore-ignored" / "junk.bin").write_bytes(b"x" * 100)
    usage = session_start_usage(archives)
    assert usage["count"] == 1
    assert usage["bytes"] > 0


def test_usage_is_zero_for_missing_root(tmp_path: Path) -> None:
    assert session_start_usage(tmp_path / "nope") == {"count": 0, "bytes": 0}


def test_extract_excludes_metadata_sidecar_and_matches_expected_manifest(tmp_path: Path) -> None:
    live = save(tmp_path / "live", b"payload")
    manifest = _manifest_of(live)
    archives = tmp_path / "archives"
    archive_id = create_session_start_archive(
        archives, live, manifest=manifest, machine_id="m", session_id=str(uuid.uuid4()),
        launched_from_candidate_kind="live",
    )
    archive = archives / archive_id
    logical = session_start_archive_manifest(archive, machine_id="m")
    destination = tmp_path / "extracted"
    extract_session_start_archive(archive, destination, expected_manifest=logical, machine_id="m")
    assert not (destination / SESSION_START_METADATA_NAME).exists()
    assert (destination / "main" / "a" / "player.gdc").read_bytes() == b"payload"
    assert build_manifest(destination, machine_id="m")["root_hash"] == logical["root_hash"]


def test_extract_rejects_mismatched_expected_manifest(tmp_path: Path) -> None:
    live = save(tmp_path / "live", b"payload")
    manifest = _manifest_of(live)
    archives = tmp_path / "archives"
    archive_id = create_session_start_archive(
        archives, live, manifest=manifest, machine_id="m", session_id=str(uuid.uuid4()),
        launched_from_candidate_kind="live",
    )
    archive = archives / archive_id
    bad_expected = {**manifest, "root_hash": "0" * 64}
    with pytest.raises(SyncError) as caught:
        extract_session_start_archive(archive, tmp_path / "extracted-bad", expected_manifest=bad_expected, machine_id="m")
    assert caught.value.code == "snapshot_hash_mismatch"


def _valid_payload(**overrides) -> dict:
    payload = {
        "schema_version": "1.0.0",
        "kind": "grim_dawn_session_start_snapshot",
        "created_at": "2026-08-05T00:00:00Z",
        "root_hash": "a" * 64,
        "machine_id": "terminal-a",
        "session_id": str(uuid.uuid4()),
        "launched_from_candidate_kind": "remote_head",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("mutation", [
    lambda payload: payload.pop("schema_version"),
    lambda payload: payload.update(schema_version="2.0.0"),
    lambda payload: payload.update(kind="something_else"),
    lambda payload: payload.update(root_hash="not-hex"),
    lambda payload: payload.update(root_hash="a" * 63),
    lambda payload: payload.update(machine_id=""),
    lambda payload: payload.update(machine_id="bad id with spaces"),
    lambda payload: payload.update(session_id="not-a-uuid"),
    lambda payload: payload.update(launched_from_candidate_kind="not_a_kind"),
    lambda payload: payload.update(created_at="not-a-timestamp"),
    lambda payload: payload.update(created_at="2026-08-05T00:00:00"),  # no trailing Z
    lambda payload: payload.update(extra_field="unexpected"),
])
def test_read_metadata_rejects_every_schema_violation(tmp_path: Path, mutation) -> None:
    import json
    payload = _valid_payload()
    mutation(payload)
    archive = tmp_path / "archive"; archive.mkdir()
    (archive / SESSION_START_METADATA_NAME).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SyncError) as caught:
        read_session_start_metadata(archive)
    assert caught.value.code == "invalid_session_start_metadata"


def test_read_metadata_rejects_missing_file(tmp_path: Path) -> None:
    archive = tmp_path / "archive"; archive.mkdir()
    with pytest.raises(SyncError) as caught:
        read_session_start_metadata(archive)
    assert caught.value.code == "invalid_session_start_metadata"


def test_read_metadata_rejects_invalid_json(tmp_path: Path) -> None:
    archive = tmp_path / "archive"; archive.mkdir()
    (archive / SESSION_START_METADATA_NAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(SyncError) as caught:
        read_session_start_metadata(archive)
    assert caught.value.code == "invalid_session_start_metadata"


def test_write_metadata_round_trips_through_read(tmp_path: Path) -> None:
    stage = tmp_path / "stage"; stage.mkdir()
    session_id = str(uuid.uuid4())
    write_session_start_metadata(
        stage, root_hash="b" * 64, machine_id="m", session_id=session_id,
        launched_from_candidate_kind="history", now=datetime(2026, 8, 5, tzinfo=timezone.utc),
    )
    payload = read_session_start_metadata(stage)
    assert payload == {
        "schema_version": "1.0.0", "kind": "grim_dawn_session_start_snapshot",
        "created_at": "2026-08-05T00:00:00Z", "root_hash": "b" * 64, "machine_id": "m",
        "session_id": session_id, "launched_from_candidate_kind": "history",
    }


def test_write_metadata_rejects_collision(tmp_path: Path) -> None:
    stage = tmp_path / "stage"; stage.mkdir()
    session_id = str(uuid.uuid4())
    write_session_start_metadata(stage, root_hash="b" * 64, machine_id="m", session_id=session_id, launched_from_candidate_kind="live")
    with pytest.raises(SyncError) as caught:
        write_session_start_metadata(stage, root_hash="b" * 64, machine_id="m", session_id=session_id, launched_from_candidate_kind="live")
    assert caught.value.code == "snapshot_name_collision"


def test_session_start_archive_id_requires_valid_root_hash() -> None:
    with pytest.raises(SyncError) as caught:
        session_start_archive_id("not-hex")
    assert caught.value.code == "invalid_manifest_root"
    generated = session_start_archive_id("a" * 64)
    assert SESSION_START_ID_PATTERN.fullmatch(generated)
