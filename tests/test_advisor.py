from __future__ import annotations

import unittest

from grim_dawn_lab.advisor import analyze_encounters, render_advisor_markdown, scenarios_from_dataset


def build() -> dict:
    return {
        "schema_version": "0.1.0", "health": 1000, "defensive_ability": 1000,
        "resistances": {"fire": {"current": 60, "maximum": 80, "uncapped": 60}},
        "armor": {"absorption": 0.7, "slots": {slot: 100 for slot in ("head", "shoulders", "torso", "arms", "legs", "feet")}},
        "provenance": {"source": "fixture"},
    }


def skill(amount=100):
    return {"damage_packets": [{"damage_type": "fire", "minimum": amount, "maximum": amount}]}


class AdvisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = {"game_version": "fixture", "channel": "stable", "difficulty": "ultimate", "enemy_level": 100, "content": "campaign"}

    def test_ranks_and_recommends_effective_resistance(self) -> None:
        scenarios = [
            {"id": "small", "enemy": "A", "phase": "1", "attacks": [{"id": "hit", "time": 0, "skill": skill(100)}]},
            {"id": "large", "enemy": "B", "phase": "2", "attacks": [{"id": "hit", "time": 0, "skill": skill(500)}]},
        ]
        result = analyze_encounters(build(), scenarios, self.context)
        self.assertEqual(result["ranking"][0]["scenario_id"], "large")
        self.assertEqual(result["ranking"][0]["rank"], 1)
        candidates = result["ranking"][0]["improvement_candidates"]
        resistance = next(value for value in candidates if value["change"] == "fire_resistance:+10pp")
        self.assertGreater(resistance["damage_reduction"], 0)
        self.assertIn("Worst case:", render_advisor_markdown(result))
        self.assertEqual(result["evidence_policy"]["unknown"], "never silently approximated")

    def test_four_attack_classes_remain_visible(self) -> None:
        scenarios = [
            {"id": "single", "enemy": "E", "phase": "1", "attacks": [{"id": "hit", "time": 0, "skill": skill()}]},
            {"id": "shotgun", "enemy": "E", "phase": "1", "attacks": [{"id": "volley", "time": 0, "shape": "projectile", "hit_count": 3, "hit_interval_seconds": 0, "skill": skill()}]},
            {"id": "combo", "enemy": "E", "phase": "1", "attacks": [{"id": "debuff", "time": 0, "skill": skill(), "applies": [{"kind": "resistance_reduction", "damage_type": "fire", "value": 20, "duration_seconds": 3}]}, {"id": "followup", "time": 1, "skill": skill()}]},
            {"id": "overlap", "enemy": "E", "phase": "1", "attacks": [{"id": "floor", "source": "floor", "shape": "ground", "time": 0, "skill": skill()}, {"id": "enemy", "source": "enemy", "time": 0.05, "skill": skill()}]},
        ]
        classes = {row["classification"] for row in analyze_encounters(build(), scenarios, self.context)["ranking"]}
        self.assertEqual(classes, {"single_hit", "shotgun", "combo", "overlap"})

    def test_requires_versioned_context(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing_context"):
            analyze_encounters(build(), [], {})

    def test_normalized_enemy_view_becomes_projectile_scenario(self) -> None:
        dataset = {"views": {"enemies": {"enemy.dbr": {
            "phase": "P1", "names": {"en": "Enemy"},
            "attributes": {"offensive_ability": {"value": 2000}},
            "attack_candidates": [{"skill_record_id": "skill.dbr", "initial_timeout_seconds": 2, "delay_seconds": 5, "chance": 0.75, "range": "ShortRange"}],
            "skills": [
                {"record_id": "skill.dbr", "template": "skill_attackprojectile.tpl", "behavior": {"projectile_count": 3, "cooldown_seconds": 2}, "damage_packets": [{"damage_type": "fire", "minimum": 1, "maximum": 2, "difficulty_scaled_minimum": 10, "difficulty_scaled_maximum": 20}], "applies": [{"kind": "da_reduction", "value": 100, "duration_seconds": 3}]},
                {"record_id": "followup.dbr", "template": "skill_attack.tpl", "behavior": {}, "damage_packets": [{"damage_type": "fire", "minimum": 30, "maximum": 40}]},
            ],
        }}}}
        scenarios = scenarios_from_dataset(dataset)
        self.assertEqual(scenarios[0]["attacks"][0]["hit_count"], 3)
        self.assertEqual(scenarios[0]["attacks"][0]["skill"]["damage_packets"][0]["maximum"], 20)
        self.assertEqual(scenarios[0]["attacks"][0]["time"], 2)
        self.assertEqual(scenarios[0]["behavior"]["use_chance"], 0.75)
        combo = next(value for value in scenarios if value.get("behavior", {}).get("kind") == "debuff_followup_window")
        self.assertEqual(len(combo["attacks"]), 2)
        self.assertEqual(combo["confidence_override"], "low")


if __name__ == "__main__":
    unittest.main()
