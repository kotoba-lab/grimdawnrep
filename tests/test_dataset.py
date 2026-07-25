from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from grim_dawn_lab.dataset import build_dataset_from_dbr_roots, diff_datasets, evaluate_level_expression, evaluate_numeric_expression, parse_dbr, stable_input_manifest, write_versioned_dataset
from test_arc import make_arc


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "dbr"


class DatasetTests(unittest.TestCase):
    def test_dbr_parser_preserves_duplicate_fields(self) -> None:
        fields = parse_dbr(FIXTURE / "base" / "records" / "controllers" / "enemy.dbr")
        self.assertEqual(["50", "25"], fields["skillChance"])

    def test_reference_closure_and_expansion_override(self) -> None:
        dataset = build_dataset_from_dbr_roots(
            [("base", FIXTURE / "base"), ("gdx1", FIXTURE / "gdx1")],
            ["records/creatures/enemy.dbr"],
        )
        self.assertEqual(3, len(dataset["records"]))
        skill = dataset["records"]["records/skills/hit.dbr"]
        self.assertEqual("15;25;35;45;55;65;75;85;95;105;125", skill["fields"]["offensivePhysicalMin"])
        self.assertEqual("gdx1", skill["provenance"]["source_layer"])
        self.assertEqual(["base"], skill["provenance"]["overrides"])
        self.assertEqual(
            ["records/controllers/enemy.dbr", "records/skills/hit.dbr"],
            dataset["records"]["records/creatures/enemy.dbr"]["references"][-2:],
        )
        view = dataset["views"]["enemies"]["records/creatures/enemy.dbr"]
        hit = next(item for item in view["skills"] if item["record_id"] == "records/skills/hit.dbr")
        self.assertEqual(11, hit["skill_level"])
        packets = {packet["damage_type"]: packet for packet in hit["damage_packets"]}
        self.assertEqual(125, packets["physical"]["minimum"])
        self.assertIn("aether", packets)
        self.assertIn("acid", packets)
        self.assertEqual("da_reduction", hit["applies"][0]["kind"])
        self.assertEqual("phase:enemy", view["phase"])
        candidate = view["attack_candidates"][0]
        self.assertEqual(2, candidate["initial_timeout_seconds"])
        self.assertEqual(0.75, candidate["chance"])
        self.assertEqual("ShortRange", candidate["range"])

    def test_level_expression_rejects_code_and_floors_result(self) -> None:
        self.assertEqual(11, evaluate_level_expression("(charLevel/10)+1", 100))
        with self.assertRaises(ValueError):
            evaluate_level_expression("__import__('os').getcwd()", 100)
        self.assertEqual(27, evaluate_numeric_expression("(charLevel^2)+2", 5))

    def test_same_inputs_produce_same_hash_and_coexisting_output(self) -> None:
        args = ([("base", FIXTURE / "base")], ["records/creatures/enemy.dbr"])
        first = build_dataset_from_dbr_roots(*args)
        second = build_dataset_from_dbr_roots(*args)
        self.assertEqual(first["dataset_id"], second["dataset_id"])
        with tempfile.TemporaryDirectory() as temporary:
            first_path = write_versioned_dataset(first, Path(temporary))
            second_path = write_versioned_dataset(second, Path(temporary))
            self.assertEqual(first_path, second_path)
            self.assertEqual(first, json.loads(first_path.read_text(encoding="utf-8")))

    def test_dataset_cli_prints_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "manifest.json"
            manifest.write_text(json.dumps({"channel": "fixture", "files": []}), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-m", "grim_dawn_lab", "dataset-build", "--base", str(FIXTURE / "base"), "--select", "records/creatures/enemy.dbr", "--output-root", temporary, "--input-manifest", str(manifest)],
                cwd=ROOT,
                env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
                check=True,
                capture_output=True,
                text=True,
            )
            summary = json.loads(completed.stdout)
            self.assertEqual(3, summary["record_count"])

    def test_localization_archives_override_in_layer_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "base.arc"
            second = Path(temporary) / "gdx.arc"
            first.write_bytes(make_arc("tags.txt", b"tagEnemyName=Base\n"))
            second.write_bytes(make_arc("tags.txt", b"tagEnemyName=Expansion\n"))
            dataset = build_dataset_from_dbr_roots(
                [("base", FIXTURE / "base")],
                ["records/creatures/enemy.dbr"],
                localization_arcs={"en": [first, second]},
            )
            self.assertEqual("Expansion", dataset["localization"]["en"]["tagEnemyName"])

    def test_semantic_diff_distinguishes_record_and_field_changes(self) -> None:
        previous = {
            "dataset_id": "old",
            "records": {
                "removed.dbr": {"fields": {"x": "1"}},
                "changed.dbr": {"fields": {"same": "x", "modified": "before", "removed": "gone"}},
            },
        }
        current = {
            "dataset_id": "new",
            "records": {
                "added.dbr": {"fields": {"x": "1"}},
                "changed.dbr": {"fields": {"same": "x", "modified": "after", "added": "new"}},
            },
        }
        result = diff_datasets(previous, current)
        self.assertEqual(["added.dbr"], result["records_added"])
        self.assertEqual(["removed.dbr"], result["records_removed"])
        change = result["records_changed"]["changed.dbr"]
        self.assertEqual(["added"], change["fields_added"])
        self.assertEqual(["removed"], change["fields_removed"])
        self.assertEqual({"before": "before", "after": "after"}, change["fields_modified"]["modified"])
        self.assertEqual(3, len(result["revalidation_queue"]))
        changed_queue = next(item for item in result["revalidation_queue"] if item["reason"] == "fields_changed")
        self.assertEqual(["normalized_view"], changed_queue["claims"])

    def test_stable_manifest_excludes_only_observation_time(self) -> None:
        manifest = {"generated_at": "now", "files": [{"sha256": "abc"}], "channel": "stable"}
        self.assertEqual({"files": [{"sha256": "abc"}], "channel": "stable"}, stable_input_manifest(manifest))


if __name__ == "__main__":
    unittest.main()
