from pathlib import Path
from grim_dawn_sync.discovery import inspect_path, resolve_save_root
from grim_dawn_sync.discovery import cloud_candidates
from unittest.mock import patch


def test_explicit_save_root_wins_and_is_not_created(tmp_path: Path) -> None:
    target = tmp_path / "missing"
    root, source = resolve_save_root(target)
    assert (root, source) == (target, "config")
    assert not target.exists()
    assert inspect_path(target)["exists"] is False


def test_cloud_unreadable_is_aggregate_safe(tmp_path: Path) -> None:
    game = tmp_path / "steam/steamapps/common/Grim Dawn"
    game.mkdir(parents=True)
    (tmp_path / "steam/userdata").mkdir()
    with patch("pathlib.Path.iterdir", side_effect=PermissionError):
        assert cloud_candidates(game)["status"] == "unreadable"
