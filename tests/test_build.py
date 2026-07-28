from __future__ import annotations

import unittest

from pathlib import Path
import tempfile

from grim_dawn_lab.build import build_from_gdc, diff_builds, resolve_baseline_defenses, resolve_equipment_defenses


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parsed = {
            "header": {"character_name": "Private", "level": 100, "class_record": "tagClass", "hardcore": False, "expansion_character": 2},
            "attributes": {"physique": 100.0, "cunning": 200.0, "spirit": 300.0, "health": 9000.0, "energy": 1000.0},
            "inventory": {"equipment": [{"base": "records/items/helm.dbr", "prefix": "records/prefix.dbr", "suffix": "", "modifier": "", "transmute": "", "component": "records/component.dbr", "component_bonus": "", "augment": ""}]},
            "skills": {"skills": [{"record": "records/skills/test.dbr", "level": 10, "sublevel": 0, "enabled": True, "devotion_level": 0}]},
            "provenance": {"source_hash": "a" * 64},
        }

    def test_gdc_maps_to_common_model_without_name_by_default(self) -> None:
        build = build_from_gdc(self.parsed)
        self.assertNotIn("name", build["character"])
        self.assertEqual(build["equipment"][0]["slot"], "head")
        self.assertEqual(build["skills"][0]["level"], 10)
        self.assertEqual(build["unknowns"][0]["code"], "derived_defenses_require_game_record_resolution")

    def test_build_id_is_deterministic_and_diff_is_field_level(self) -> None:
        left = build_from_gdc(self.parsed)
        right = build_from_gdc(self.parsed)
        self.assertEqual(left["build_id"], right["build_id"])
        right["character"]["level"] = 99
        diff = diff_builds(left, right)
        self.assertFalse(diff["equivalent"])
        self.assertEqual(diff["differences"][0]["field"], "character")

    def test_static_equipment_resolution_is_partial_and_traceable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root / "records/items/helm.dbr"
            record.parent.mkdir(parents=True)
            record.write_text(
                "defensiveProtection,1000,\n"
                "defensiveElementalResistance,10,\n"
                "defensiveAether,20,\n"
                "characterDefensiveAbility,30,\n",
                encoding="utf-8",
            )
            build = resolve_equipment_defenses(build_from_gdc(self.parsed), [("base", root)])
            snapshot = build["defense_snapshots"]["equipment_static"]
            self.assertEqual(snapshot["status"], "partial")
            self.assertEqual(snapshot["armor_by_slot"]["head"], 1000.0)
            self.assertEqual(snapshot["resistance_percent_points"]["fire"], 10.0)
            self.assertEqual(snapshot["resistance_percent_points"]["aether"], 20.0)
            self.assertEqual(snapshot["evidence"], [{"record": "records/items/helm.dbr", "layer": "base"}])
            self.assertIn("not_applied", snapshot["unknowns"][-1]["code"])

    def test_baseline_uses_versioned_da_formula_and_level_scaled_passive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = root / "records/items/helm.dbr"
            skill = root / "records/skills/test.dbr"
            item.parent.mkdir(parents=True)
            skill.parent.mkdir(parents=True)
            item.write_text("characterDefensiveAbility,30,\n", encoding="utf-8")
            skill.write_text("Class,Skill_Passive,\ncharacterDefensiveAbility,10;20;30,\n", encoding="utf-8")
            parsed = {**self.parsed, "skills": {"skills": [{"record": "records/skills/test.dbr", "level": 2, "sublevel": 0, "enabled": True, "devotion_level": 0}]}}
            snapshot = resolve_baseline_defenses(build_from_gdc(parsed), [("base", root)])["defense_snapshots"]["baseline"]
            expected = (30 + 20 + 100 * 12 + 100 * 0.5) + 53
            self.assertEqual(snapshot["defensive_ability"], expected)
            self.assertEqual(snapshot["status"], "approximate")
            self.assertEqual(snapshot["formula_trace"]["source_record"], "records/game/combatformulas.dbr")
            self.assertEqual(snapshot["combat_snapshot"]["armor"]["absorption"], 0.7)
            self.assertEqual(snapshot["combat_snapshot"]["defensive_ability"], expected)


if __name__ == "__main__":
    unittest.main()
