from datetime import datetime, timezone
import os
from pathlib import Path
import pytest
from unittest.mock import patch

from grim_dawn_sync.errors import SyncError
from grim_dawn_sync import manifest as manifest_module
from grim_dawn_sync.manifest import build_manifest, stable_manifest


def test_manifest_is_deterministic_and_excludes_metadata(tmp_path: Path) -> None:
    (tmp_path / "main/a").mkdir(parents=True)
    (tmp_path / "main/a/player.gdc").write_bytes(b"synthetic")
    first = build_manifest(tmp_path, machine_id="a", now=datetime(2020, 1, 1, tzinfo=timezone.utc))
    second = build_manifest(tmp_path, machine_id="b", now=datetime(2021, 1, 1, tzinfo=timezone.utc))
    assert first["root_hash"] == second["root_hash"]
    assert first["character_count"] == 1
    assert stable_manifest(tmp_path, machine_id="a", retries=1)["file_count"] == 1


def test_manifest_rejects_casefold_collision(tmp_path: Path) -> None:
    (tmp_path / "é").mkdir()
    (tmp_path / "e\u0301").mkdir()
    (tmp_path / "é/x").write_bytes(b"a")
    (tmp_path / "e\u0301/x").write_bytes(b"b")
    with pytest.raises(SyncError, match="colliding"):
        build_manifest(tmp_path, machine_id="a")


def test_nfc_and_nfd_trees_have_the_same_hash(tmp_path: Path) -> None:
    nfc, nfd = tmp_path / "nfc", tmp_path / "nfd"
    (nfc / "é").mkdir(parents=True); (nfd / "e\u0301").mkdir(parents=True)
    (nfc / "é/file").write_bytes(b"same"); (nfd / "e\u0301/file").write_bytes(b"same")
    assert build_manifest(nfc, machine_id="a")["root_hash"] == build_manifest(nfd, machine_id="a")["root_hash"]


@pytest.mark.parametrize("after", ["added", "removed"])
def test_manifest_rejects_tree_changed_between_enumerations(tmp_path: Path, after: str) -> None:
    item = tmp_path / "item"; item.write_bytes(b"x")
    first = [item]
    second = [item, tmp_path / "new"] if after == "added" else []
    with patch("grim_dawn_sync.manifest._entries", side_effect=[first, second]):
        with pytest.raises(SyncError) as error:
            build_manifest(tmp_path, machine_id="a")
    assert error.value.code == "save_changed_during_scan"


def test_manifest_rehash_detects_same_metadata_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = tmp_path / "item"; item.write_bytes(b"old")
    original_read = Path.read_bytes
    original_stat = item.stat()
    calls = 0
    def read_then_replace(path: Path) -> bytes:
        nonlocal calls
        data = original_read(path)
        calls += 1
        if calls == 1:
            path.write_bytes(b"new")
            os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        return data
    monkeypatch.setattr(Path, "read_bytes", read_then_replace)
    with pytest.raises(SyncError) as error:
        build_manifest(tmp_path, machine_id="a")
    assert error.value.code == "save_changed_during_scan"


def test_manifest_rejects_root_and_nested_reparse_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nested = tmp_path / "nested"; nested.mkdir()
    file = nested / "item"; file.write_bytes(b"x")
    original_lstat = Path.lstat
    monkeypatch.setattr(Path, "is_symlink", lambda _: False)
    class ReparseStat:
        st_file_attributes = 0x400
    monkeypatch.setattr(Path, "lstat", lambda path: ReparseStat() if path == tmp_path else original_lstat(path))
    with pytest.raises(SyncError) as error:
        build_manifest(tmp_path, machine_id="a")
    assert error.value.code == "unsafe_save_tree"
    monkeypatch.setattr(Path, "lstat", lambda path: ReparseStat() if path in (nested, file) else original_lstat(path))
    with pytest.raises(SyncError) as error:
        build_manifest(tmp_path, machine_id="a")
    assert error.value.code == "unsafe_save_tree"


def test_stable_manifest_retries_exhausted_on_different_roots(tmp_path: Path) -> None:
    first = {"root_hash": "a"}; second = {"root_hash": "b"}
    with patch("grim_dawn_sync.manifest.build_manifest", side_effect=[first, second, first, second]):
        with pytest.raises(SyncError) as error:
            stable_manifest(tmp_path, machine_id="a", retries=2)
    assert error.value.code == "save_not_stable"


def test_manifest_counts_fifty_synthetic_characters(tmp_path: Path) -> None:
    for index in range(50):
        player = tmp_path / "main" / f"character-{index:02d}" / "player.gdc"
        player.parent.mkdir(parents=True)
        player.write_bytes(f"synthetic-{index}".encode())
    result = build_manifest(tmp_path, machine_id="a")
    assert result["character_count"] == 50
    assert result["file_count"] == 50
    assert len(result["root_hash"]) == 64
