from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

from grim_dawn_lab.doctor import REQUIRED_INPUTS, create_manifest, resolve_install_path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "game_install"


def fingerprint(path: Path) -> tuple[int, int, str]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


class DoctorTests(unittest.TestCase):
    def test_fixture_manifest_contains_all_required_hashes(self) -> None:
        manifest = create_manifest(FIXTURE, channel="unknown")

        self.assertEqual("explicit", manifest["install"]["detection"])
        self.assertEqual("not_established", manifest["channel_evidence"])
        self.assertEqual([], manifest["warnings"])
        self.assertEqual(5, len(manifest["files"]))
        self.assertTrue(all(manifest["content"].values()))
        for item in manifest["files"]:
            self.assertTrue(item["exists"])
            self.assertEqual(64, len(item["sha256"]))
            self.assertGreater(item["size_bytes"], 0)

    def test_diagnostic_does_not_modify_inputs(self) -> None:
        paths = [FIXTURE / relative for _, _, relative in REQUIRED_INPUTS]
        before = {path: fingerprint(path) for path in paths}
        create_manifest(FIXTURE)
        after = {path: fingerprint(path) for path in paths}
        self.assertEqual(before, after)

    def test_auto_detection_uses_candidate_with_base_database(self) -> None:
        path, detection, checked = resolve_install_path(None, [ROOT / "missing", FIXTURE])
        self.assertEqual(FIXTURE.resolve(), path)
        self.assertEqual("steam_default", detection)
        self.assertEqual(2, len(checked))

    def test_missing_file_is_a_machine_readable_warning(self) -> None:
        manifest = create_manifest(ROOT / "missing-install")
        codes = [warning["code"] for warning in manifest["warnings"]]
        self.assertIn("install_path_missing", codes)
        self.assertEqual(5, codes.count("required_file_missing"))

    def test_cli_prints_json(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "grim_dawn_lab", "doctor", "--install-path", str(FIXTURE)],
            cwd=ROOT,
            env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
            check=True,
            capture_output=True,
            text=True,
        )
        manifest = json.loads(completed.stdout)
        self.assertEqual("1.0.0", manifest["schema_version"])


if __name__ == "__main__":
    unittest.main()
