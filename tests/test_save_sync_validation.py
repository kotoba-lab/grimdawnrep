from pathlib import Path
from unittest.mock import patch
import pytest

from grim_dawn_lab.gdc import GdcError, UnsupportedGdcVersion
from grim_dawn_sync.validation import destructive_change, validate_players
from grim_dawn_sync.errors import SyncError


def manifest(files: list[dict], *, characters: int = 1, total: int = 10) -> dict:
    return {"files": files, "character_count": characters, "total_bytes": total}


def test_unsupported_player_version_blocks_push_without_corruption(tmp_path: Path) -> None:
    item = {"path": "main/private/player.gdc", "size": 1, "sha256": "0" * 64}
    (tmp_path / "main/private").mkdir(parents=True); (tmp_path / "main/private/player.gdc").write_bytes(b"x")
    with patch("grim_dawn_sync.validation.import_player_gdc", side_effect=UnsupportedGdcVersion("new")):
        result = validate_players(tmp_path, manifest([item]))
    assert result == {"ok": False, "classification": "unsupported_save_version", "push_allowed": False}


def test_destructive_change_never_exposes_character_paths() -> None:
    old = manifest([{"path": "main/private/player.gdc"}], characters=1, total=10)
    current = manifest([], characters=0, total=0)
    result = destructive_change(old, current)
    assert result["destructive_change"] is True
    assert "private" not in str(result)


def test_traversal_manifest_path_never_calls_parser(tmp_path: Path) -> None:
    item = {"path": "../outside/player.gdc", "size": 1, "sha256": "0" * 64}
    with patch("grim_dawn_sync.validation.import_player_gdc") as parser:
        with pytest.raises(SyncError, match="invalid") as error: validate_players(tmp_path, manifest([item]))
    parser.assert_not_called()
    assert error.value.code == "invalid_manifest_path"


def test_uppercase_player_path_is_validated_and_gdc_error_is_invalid(tmp_path: Path) -> None:
    (tmp_path / "MAIN/x").mkdir(parents=True); (tmp_path / "MAIN/x/PLAYER.GDC").write_bytes(b"x")
    item = {"path": "MAIN/x/PLAYER.GDC", "size": 1, "sha256": "0" * 64}
    with patch("grim_dawn_sync.validation.import_player_gdc", side_effect=GdcError("bad")):
        with pytest.raises(SyncError, match="failed") as error: validate_players(tmp_path, manifest([item]))
    assert error.value.code == "invalid_save"


def test_parser_secret_result_is_not_exposed(tmp_path: Path) -> None:
    item = {"path": "main/private/player.gdc", "size": 1, "sha256": "0" * 64}
    (tmp_path / "main/private").mkdir(parents=True); (tmp_path / "main/private/player.gdc").write_bytes(b"x")
    with patch("grim_dawn_sync.validation.import_player_gdc", return_value={"header": {"character_name": "SECRET"}}):
        result = validate_players(tmp_path, manifest([item]))
    assert result == {"ok": True, "classification": "valid", "push_allowed": True}
    assert "SECRET" not in str(result)


def test_parser_exception_with_post_reparse_is_unsafe_tree(tmp_path: Path) -> None:
    item = {"path": "main/private/player.gdc", "size": 1, "sha256": "0" * 64}
    (tmp_path / "main/private").mkdir(parents=True); (tmp_path / "main/private/player.gdc").write_bytes(b"x")
    unsafe = SyncError("unsafe_save_tree", "safe message", 3)
    with patch("grim_dawn_sync.validation.assert_safe_save_file", side_effect=[tmp_path / "main/private/player.gdc", unsafe]), patch("grim_dawn_sync.validation.import_player_gdc", side_effect=GdcError("bad")):
        with pytest.raises(SyncError) as error:
            validate_players(tmp_path, manifest([item]))
    assert error.value.code == "unsafe_save_tree"


def test_destructive_change_classifies_individual_removal_and_bytes() -> None:
    old = manifest([{"path": "main/private/PLAYER.GDC"}, {"path": "other/file"}], characters=1, total=10)
    no_player = manifest([{"path": "other/file"}], characters=1, total=10)
    assert "player_gdc_missing" in destructive_change(old, no_player)["reasons"]
    only_file_removed = destructive_change(manifest([{"path": "other/file"}], characters=0, total=10), manifest([], characters=0, total=10))
    assert only_file_removed["reasons"] == ["files_removed"]
    only_bytes_decreased = destructive_change(manifest([], characters=0, total=10), manifest([], characters=0, total=9))
    assert only_bytes_decreased["reasons"] == ["total_bytes_decreased"]
