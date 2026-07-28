from __future__ import annotations

import unittest
import json
from pathlib import Path
import subprocess
import sys

from grim_dawn_lab.timeline import compare_observation, simulate_attack_sequence


def build() -> dict:
    return {
        "health": 1000,
        "defensive_ability": 1000,
        "resistances": {"fire": {"current": 80, "maximum": 80, "uncapped": 80}},
        "armor": {"absorption": 0.7, "slots": {slot: 0 for slot in ("head", "shoulders", "torso", "arms", "legs", "feet")}},
    }


def fire_skill(amount: float = 100) -> dict:
    return {"damage_packets": [{"damage_type": "fire", "minimum": amount, "maximum": amount}]}


class TimelineTests(unittest.TestCase):
    def test_debuff_followup_is_recalculated_and_classified_combo(self) -> None:
        result = simulate_attack_sequence(
            build(),
            [
                {"id": "debuff", "time": 0, "skill": fire_skill(), "applies": [{"kind": "resistance_reduction", "damage_type": "fire", "value": 30, "duration_seconds": 3}]},
                {"id": "followup", "time": 1, "skill": fire_skill()},
            ],
        )
        self.assertEqual("combo", result["classification"])
        self.assertAlmostEqual(20, result["timeline"][0]["damage"]["range"]["maximum"])
        self.assertAlmostEqual(50, result["timeline"][1]["damage"]["range"]["maximum"])
        self.assertEqual({"fire": 30.0}, result["timeline"][1]["state_before"]["resistance_reductions"])

    def test_simultaneous_projectiles_are_shotgun(self) -> None:
        result = simulate_attack_sequence(build(), [{"id": "volley", "time": 0, "shape": "projectile", "hit_count": 3, "hit_interval_seconds": 0, "skill": fire_skill()}])
        self.assertEqual("shotgun", result["classification"])
        self.assertEqual(3, result["summary"]["hit_count"])

    def test_distinct_sources_are_overlap(self) -> None:
        result = simulate_attack_sequence(build(), [{"id": "floor", "source": "floor", "shape": "ground", "time": 0, "skill": fire_skill()}, {"id": "enemy", "source": "enemy", "time": 0.05, "skill": fire_skill()}])
        self.assertEqual("overlap", result["classification"])

    def test_unsupported_dot_is_reported_as_unknown(self) -> None:
        skill = {"damage_packets": [{"damage_type": "fire", "minimum": 100, "maximum": 100, "duration_seconds": 3}]}
        result = simulate_attack_sequence(build(), [{"id": "dot", "time": 0, "skill": skill}])
        self.assertEqual("unknown", result["classification"])
        self.assertEqual("unsupported_effect", result["unknowns"][0]["code"])

    def test_observation_comparison_reports_error(self) -> None:
        simulation = simulate_attack_sequence(build(), [{"id": "hit", "time": 0, "skill": fire_skill()}])
        observation = {"events": [{"kind": "hit", "observed_value": 25}], "provenance": {"method": "fixture"}}
        comparison = compare_observation(simulation, observation)
        self.assertAlmostEqual(5, comparison["comparisons"][0]["absolute_error"])

    def test_sequence_cli_with_observation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        fixture = root / "tests" / "fixtures" / "combat"
        completed = subprocess.run(
            [sys.executable, "-m", "grim_dawn_lab", "sequence", "--build", str(fixture / "build.json"), "--attacks", str(fixture / "attacks.json"), "--observation", str(fixture / "observation.json")],
            cwd=root,
            env={**__import__("os").environ, "PYTHONPATH": str(root / "src")},
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual("combo", result["classification"])
        self.assertEqual(0, result["observation_comparison"]["comparisons"][1]["absolute_error"])


if __name__ == "__main__":
    unittest.main()
