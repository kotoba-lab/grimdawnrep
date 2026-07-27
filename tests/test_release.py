from __future__ import annotations

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from grim_dawn_lab.release import audit_git_distribution


class ReleaseAuditTests(unittest.TestCase):
    def test_audit_fails_closed_for_generated_game_data_and_saves(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "README.md\ndata/generated/dataset.json\ncharacter.gdc\n", "")
        with patch("grim_dawn_lab.release.subprocess.run", return_value=completed):
            result = audit_git_distribution(Path("."))
        self.assertFalse(result["safe"])
        self.assertEqual(
            ["data/generated/dataset.json", "character.gdc"],
            [entry["path"] for entry in result["violations"]],
        )
