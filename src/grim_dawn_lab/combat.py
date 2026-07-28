"""Deterministic, explainable calculations for a single incoming hit.

The module intentionally models only mechanics whose ordering is established.
Unsupported packet effects are returned as warnings instead of being guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


ARMOR_SLOT_WEIGHTS = {
    "head": 0.15,
    "shoulders": 0.15,
    "torso": 0.26,
    "arms": 0.12,
    "legs": 0.20,
    "feet": 0.12,
}

SUPPORTED_DAMAGE_TYPES = {
    "physical",
    "pierce",
    "fire",
    "cold",
    "lightning",
    "acid",
    "vitality",
    "aether",
    "chaos",
}


@dataclass(frozen=True)
class HitOutcome:
    probability_to_hit: float
    normal_hit_probability: float
    critical_probabilities: Mapping[float, float]
    miss_probability: float
    noncritical_multiplier: float


def probability_to_hit(offensive_ability: float, defensive_ability: float) -> float:
    """Return official OA-vs-DA PTH, with the documented 55% floor."""
    if offensive_ability < 0 or defensive_ability < 0:
        raise ValueError("OA and DA must be non-negative")
    if offensive_ability == 0 and defensive_ability == 0:
        return 55.0
    first = ((offensive_ability / ((defensive_ability / 3.5) + offensive_ability)) * 300) * 0.3
    second = ((((offensive_ability * 3.25) + 10000) - (defensive_ability * 3.25)) / 100) * 0.7
    return max(55.0, first + second - 50)


def hit_outcome(offensive_ability: float, defensive_ability: float) -> HitOutcome:
    """Split PTH into miss, normal-hit, and critical multiplier probabilities."""
    pth = probability_to_hit(offensive_ability, defensive_ability)
    if pth < 70:
        return HitOutcome(pth, pth / 100, {}, 1 - pth / 100, pth / 70)

    # The official examples use inclusive integer rolls: at PTH 97, rolls
    # 1..89 are normal and 90..97 are x1.1 critical hits.
    thresholds = ((90.0, 1.1), (105.0, 1.2), (120.0, 1.3), (135.0, 1.4), (150.0, 1.5))
    denominator = max(100.0, pth)
    critical: dict[float, float] = {}
    for index, (lower, multiplier) in enumerate(thresholds):
        upper = thresholds[index + 1][0] - 1 if index + 1 < len(thresholds) else pth
        width = max(0.0, min(pth, upper) - lower + 1)
        if width:
            critical[multiplier] = width / denominator
    normal_upper = min(pth, 89.0)
    normal = max(0.0, normal_upper) / denominator
    return HitOutcome(pth, normal, critical, max(0.0, 100 - pth) / denominator, 1.0)


def _resistance_fraction(build: Mapping, damage_type: str, reductions: Mapping[str, float]) -> float:
    entry = build.get("resistances", {}).get(damage_type, {})
    current = float(entry.get("uncapped", entry.get("current", 0)))
    maximum = float(entry.get("maximum", 80))
    effective = min(maximum, current - float(reductions.get(damage_type, 0)))
    return effective / 100


def calculate_single_hit(
    build: Mapping,
    skill: Mapping,
    *,
    resistance_reductions: Mapping[str, float] | None = None,
    percent_absorption: float | None = None,
    flat_absorption: float | None = None,
    shield_state: str = "unknown",
) -> dict:
    """Calculate min/average/max damage by armor slot and retain a stage trace."""
    reductions = resistance_reductions or {}
    absorption = build.get("absorption", {})
    percent_absorption = float(absorption.get("percent", 0) if percent_absorption is None else percent_absorption)
    flat_absorption = float(absorption.get("flat", 0) if flat_absorption is None else flat_absorption)
    if not 0 <= percent_absorption <= 1 or flat_absorption < 0:
        raise ValueError("absorption values are out of range")
    if shield_state not in {"unknown", "blocked", "unblocked"}:
        raise ValueError("shield_state must be unknown, blocked, or unblocked")

    percent_monster_reduction = float(build.get("monster_type_reduction", {}).get("percent", 0))
    flat_monster_reduction = float(build.get("monster_type_reduction", {}).get("flat", 0))
    physical_resistance = float(build.get("physical_resistance", 0))
    if not 0 <= percent_monster_reduction <= 1 or flat_monster_reduction < 0:
        raise ValueError("monster type reduction values are out of range")
    if not -1 <= physical_resistance <= 1:
        raise ValueError("physical resistance is out of range")

    warnings: list[dict[str, str]] = []
    components: list[dict] = []
    for packet in skill.get("damage_packets", []):
        damage_type = packet["damage_type"]
        if damage_type not in SUPPORTED_DAMAGE_TYPES or packet.get("duration_seconds", 0) > 0:
            warnings.append({"code": "unsupported_effect", "field": damage_type})
            continue
        minimum, maximum = float(packet["minimum"]), float(packet["maximum"])
        resistance = (
            physical_resistance - float(reductions.get("physical", 0)) / 100
            if damage_type == "physical"
            else _resistance_fraction(build, damage_type, reductions)
        )
        components.append(
            {
                "damage_type": damage_type,
                "raw": {"minimum": minimum, "maximum": maximum},
                "effective_resistance": resistance,
            }
        )

    armor = build["armor"]
    armor_absorption = float(armor["absorption"])
    if not 0 <= armor_absorption <= 1:
        raise ValueError("armor absorption is out of range")
    shield = build.get("shield")
    block_chance = float(shield.get("block_chance", 0)) if shield else 0.0
    block_amount = float(shield.get("block_amount", 0)) if shield else 0.0
    if not 0 <= block_chance <= 1 or block_amount < 0:
        raise ValueError("shield values are out of range")
    branch_probabilities = (
        {"blocked": block_chance, "unblocked": 1 - block_chance}
        if shield and shield_state == "unknown"
        else {shield_state: 1.0}
        if shield_state != "unknown"
        else {"unblocked": 1.0}
    )

    def evaluate(slot_armor: float, bound: str, blocked: bool) -> tuple[float, list[dict]]:
        stage_components: list[dict] = []
        total = 0.0
        for component in components:
            raw = component["raw"][bound]
            after_shield = max(0.0, raw - block_amount) if blocked else raw
            after_percent_monster = after_shield * (1 - percent_monster_reduction)
            after_resistance = after_percent_monster * (1 - component["effective_resistance"])
            after_armor = after_resistance
            if component["damage_type"] == "physical":
                after_armor -= min(after_resistance, slot_armor) * armor_absorption
            total += after_armor
            stage_components.append(
                {
                    "damage_type": component["damage_type"],
                    "raw": raw,
                    "after_shield": after_shield,
                    "after_percent_monster_reduction": after_percent_monster,
                    "after_resistance": after_resistance,
                    "after_armor": after_armor,
                }
            )
        after_flat_monster = max(0.0, total - flat_monster_reduction)
        after_percent_absorption = after_flat_monster * (1 - percent_absorption)
        final = max(0.0, after_percent_absorption - flat_absorption)
        return final, [
            *stage_components,
            {
                "aggregate": True,
                "after_components": total,
                "after_flat_monster_reduction": after_flat_monster,
                "after_percent_absorption": after_percent_absorption,
                "final": final,
            },
        ]

    by_slot: dict[str, dict] = {}
    for slot, weight in ARMOR_SLOT_WEIGHTS.items():
        slot_armor = float(armor["slots"][slot])
        branches: dict[str, dict] = {}
        for branch, probability in branch_probabilities.items():
            minimum, minimum_trace = evaluate(slot_armor, "minimum", branch == "blocked")
            maximum, maximum_trace = evaluate(slot_armor, "maximum", branch == "blocked")
            branches[branch] = {
                "probability": probability,
                "minimum": minimum,
                "average": (minimum + maximum) / 2,
                "maximum": maximum,
                "trace": {"minimum": minimum_trace, "maximum": maximum_trace},
            }
        totals = [value[key] for value in branches.values() for key in ("minimum", "maximum")]
        weighted_average = sum(value["probability"] * value["average"] for value in branches.values())
        by_slot[slot] = {
            "probability": weight,
            "armor": slot_armor,
            "minimum": min(totals),
            "average": weighted_average,
            "maximum": max(totals),
            "shield_branches": branches,
        }

    expected_average = sum(result["probability"] * result["average"] for result in by_slot.values())
    result = {
        "schema_version": "0.1.0",
        "components": components,
        "armor_absorption": armor_absorption,
        "physical_resistance": physical_resistance,
        "monster_type_reduction": {"percent": percent_monster_reduction, "flat": flat_monster_reduction},
        "shield_state": shield_state,
        "percent_absorption": percent_absorption,
        "flat_absorption": flat_absorption,
        "by_armor_slot": by_slot,
        "range": {
            "minimum": min(item["minimum"] for item in by_slot.values()),
            "expected_average": expected_average,
            "maximum": max(item["maximum"] for item in by_slot.values()),
        },
        "health": float(build["health"]),
        "warnings": warnings,
    }
    if skill.get("offensive_ability") is not None:
        outcome = hit_outcome(float(skill["offensive_ability"]), float(build["defensive_ability"]))
        multipliers = [outcome.noncritical_multiplier, *outcome.critical_probabilities.keys()]
        expected_multiplier = outcome.normal_hit_probability * outcome.noncritical_multiplier + sum(
            probability * multiplier for multiplier, probability in outcome.critical_probabilities.items()
        )
        result["hit_outcome"] = outcome.__dict__
        result["range"]["minimum_on_hit"] = result["range"]["minimum"] * min(multipliers)
        result["range"]["maximum_on_hit"] = result["range"]["maximum"] * max(multipliers)
        result["range"]["expected_per_attack"] = expected_average * expected_multiplier
    maximum_damage = result["range"].get("maximum_on_hit", result["range"]["maximum"])
    minimum_damage = result["range"].get("minimum_on_hit", result["range"]["minimum"])
    result["health_delta"] = {
        "best_case_remaining": max(0.0, result["health"] - minimum_damage),
        "worst_case_remaining": max(0.0, result["health"] - maximum_damage),
        "best_case_damage": minimum_damage,
        "worst_case_damage": maximum_damage,
    }
    result["lethal"] = maximum_damage >= result["health"]
    return result
