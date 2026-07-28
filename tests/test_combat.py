from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

from grim_dawn_lab.combat import calculate_single_hit, hit_outcome, probability_to_hit


def build(*, armor: float = 50, absorption: float = 0.70) -> dict:
    return {
        "health": 1000,
        "defensive_ability": 1000,
        "resistances": {"fire": {"current": 80, "maximum": 80, "uncapped": 100}},
        "armor": {
            "absorption": absorption,
            "slots": {slot: armor for slot in ("head", "shoulders", "torso", "arms", "legs", "feet")},
        },
        "absorption": {"percent": 0, "flat": 0},
    }


class CombatTests(unittest.TestCase):
    def test_official_armor_example_50_armor_takes_65(self) -> None:
        result = calculate_single_hit(build(), {"damage_packets": [{"damage_type": "physical", "minimum": 100, "maximum": 100}]})
        self.assertEqual(65, result["range"]["minimum"])
        self.assertEqual(65, result["range"]["maximum"])

    def test_official_armor_example_armor_above_hit_takes_30(self) -> None:
        result = calculate_single_hit(build(armor=124), {"damage_packets": [{"damage_type": "physical", "minimum": 100, "maximum": 100}]})
        self.assertAlmostEqual(30, result["range"]["minimum"])

    def test_official_armor_example_84_percent_absorption_takes_16(self) -> None:
        result = calculate_single_hit(build(armor=124, absorption=0.84), {"damage_packets": [{"damage_type": "physical", "minimum": 100, "maximum": 100}]})
        self.assertAlmostEqual(16, result["range"]["minimum"])

    def test_resistance_reduction_consumes_overcap_before_resistance(self) -> None:
        result = calculate_single_hit(build(), {"damage_packets": [{"damage_type": "fire", "minimum": 100, "maximum": 100}]}, resistance_reductions={"fire": 30})
        self.assertAlmostEqual(30, result["range"]["minimum"])
        self.assertAlmostEqual(0.70, result["components"][0]["effective_resistance"])

    def test_absorption_order_is_percent_then_flat(self) -> None:
        result = calculate_single_hit(build(armor=0), {"damage_packets": [{"damage_type": "physical", "minimum": 100, "maximum": 100}]}, percent_absorption=0.20, flat_absorption=10)
        self.assertEqual(70, result["range"]["minimum"])

    def test_order_regression_resistance_precedes_armor(self) -> None:
        subject = build(armor=50)
        subject["physical_resistance"] = 0.5
        result = calculate_single_hit(subject, {"damage_packets": [{"damage_type": "physical", "minimum": 100, "maximum": 100}]})
        self.assertAlmostEqual(15, result["range"]["minimum"])

    def test_unknown_effect_is_reported_not_approximated(self) -> None:
        result = calculate_single_hit(build(), {"damage_packets": [{"damage_type": "retaliation_reflect", "minimum": 100, "maximum": 100}]})
        self.assertEqual("unsupported_effect", result["warnings"][0]["code"])
        self.assertEqual(0, result["range"]["maximum"])

    def test_shield_branches_are_preserved_and_weighted(self) -> None:
        subject = build(armor=0)
        subject["shield"] = {"block_chance": 0.25, "block_amount": 40, "recovery_seconds": 1}
        result = calculate_single_hit(subject, {"damage_packets": [{"damage_type": "physical", "minimum": 100, "maximum": 100}]})
        torso = result["by_armor_slot"]["torso"]
        self.assertEqual(60, torso["shield_branches"]["blocked"]["minimum"])
        self.assertEqual(100, torso["shield_branches"]["unblocked"]["minimum"])
        self.assertEqual(90, torso["average"])

    def test_full_defense_order_trace(self) -> None:
        subject = build(armor=20)
        subject["physical_resistance"] = 0.25
        subject["shield"] = {"block_chance": 1, "block_amount": 20, "recovery_seconds": 1}
        subject["monster_type_reduction"] = {"percent": 0.10, "flat": 5}
        subject["absorption"] = {"percent": 0.20, "flat": 3}
        result = calculate_single_hit(subject, {"damage_packets": [{"damage_type": "physical", "minimum": 100, "maximum": 100}]}, shield_state="blocked")
        trace = result["by_armor_slot"]["head"]["shield_branches"]["blocked"]["trace"]["minimum"]
        self.assertEqual(80, trace[0]["after_shield"])
        self.assertEqual(72, trace[0]["after_percent_monster_reduction"])
        self.assertEqual(54, trace[0]["after_resistance"])
        self.assertEqual(40, trace[0]["after_armor"])
        self.assertEqual(25, trace[-1]["final"])

    def test_critical_multiplier_changes_maximum_on_hit(self) -> None:
        subject = build(armor=0)
        skill = {"offensive_ability": 2000, "damage_packets": [{"damage_type": "physical", "minimum": 100, "maximum": 100}]}
        result = calculate_single_hit(subject, skill)
        self.assertIn("hit_outcome", result)
        self.assertGreater(result["range"]["maximum_on_hit"], 100)
        outcome = result["hit_outcome"]
        total_probability = outcome["normal_hit_probability"] + outcome["miss_probability"] + sum(outcome["critical_probabilities"].values())
        self.assertAlmostEqual(1, total_probability)
        self.assertEqual(0, result["health_delta"]["worst_case_damage"] - result["range"]["maximum_on_hit"])

    def test_equal_oa_da_has_documented_formula_result(self) -> None:
        self.assertAlmostEqual(90, probability_to_hit(1000, 1000))
        outcome = hit_outcome(1000, 1000)
        self.assertAlmostEqual(0.89, outcome.normal_hit_probability)
        self.assertEqual({1.1: 0.01}, outcome.critical_probabilities)
        self.assertAlmostEqual(0.10, outcome.miss_probability)

    def test_official_pth_97_example(self) -> None:
        # Use a direct result-shaped OA/DA pair found by binary search to avoid
        # duplicating the production formula in this regression assertion.
        low, high = 1000.0, 2000.0
        for _ in range(80):
            midpoint = (low + high) / 2
            if probability_to_hit(midpoint, 1000) < 97:
                low = midpoint
            else:
                high = midpoint
        outcome = hit_outcome(high, 1000)
        self.assertAlmostEqual(0.89, outcome.normal_hit_probability, places=6)
        self.assertAlmostEqual(0.08, outcome.critical_probabilities[1.1], places=6)
        self.assertAlmostEqual(0.03, outcome.miss_probability, places=6)

    def test_low_pth_has_floor_and_reduced_damage(self) -> None:
        outcome = hit_outcome(1, 10000)
        self.assertEqual(55, outcome.probability_to_hit)
        self.assertAlmostEqual(55 / 70, outcome.noncritical_multiplier)

    def test_cli_explains_hit_from_json_inputs(self) -> None:
        root = Path(__file__).resolve().parents[1]
        fixture = root / "tests" / "fixtures" / "combat"
        completed = subprocess.run(
            [sys.executable, "-m", "grim_dawn_lab", "single-hit", "--build", str(fixture / "build.json"), "--skill", str(fixture / "skill.json")],
            cwd=root,
            env={**__import__("os").environ, "PYTHONPATH": str(root / "src")},
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(85, result["range"]["minimum"])
        self.assertIn("hit_outcome", result)


if __name__ == "__main__":
    unittest.main()
