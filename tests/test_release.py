from __future__ import annotations

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from grim_dawn_lab.release import audit_distribution_paths, audit_git_distribution


class ReleaseAuditTests(unittest.TestCase):
    def test_audit_fails_closed_for_generated_game_data_and_saves(self) -> None:
        completed = subprocess.CompletedProcess(
            [],
            0,
            "README.md\ndata/generated/dataset.json\ncharacter.gdc\n",
            "",
        )
        with patch("grim_dawn_lab.release.subprocess.run", return_value=completed):
            result = audit_git_distribution(Path("."))
        self.assertFalse(result["safe"])
        self.assertEqual(
            ["data/generated/dataset.json", "character.gdc"],
            [entry["path"] for entry in result["violations"]],
        )

    def test_distribution_rejects_game_data_and_saves(self) -> None:
        result = audit_distribution_paths(
            ["src/tool.py", "data/raw/database.arz", "private/player.gdc"]
        )
        self.assertFalse(result["safe"])
        self.assertEqual(
            {item["reason"] for item in result["violations"]},
            {"generated_or_raw_game_data", "game_or_save_binary"},
        )

    def test_source_fixtures_and_schemas_are_safe(self) -> None:
        result = audit_distribution_paths(
            [
                "src/tool.py",
                "tests/fixtures/example.json",
                "tests/fixtures/game_install/database/database.arz",
                "schemas/build.schema.json",
            ]
        )
        self.assertTrue(result["safe"])


if __name__ == "__main__":
    unittest.main()
