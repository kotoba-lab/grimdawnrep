"""Deterministic attack-sequence simulation with explicit uncertainty."""

from __future__ import annotations

from copy import deepcopy
from typing import Iterable, Mapping

from grim_dawn_lab.combat import calculate_single_hit


def simulate_attack_sequence(build: Mapping, attacks: Iterable[Mapping]) -> dict:
    scheduled: list[dict] = []
    attack_definitions = list(attacks)
    for attack in attack_definitions:
        count = int(attack.get("hit_count", 1))
        interval = float(attack.get("hit_interval_seconds", 0))
        for hit_index in range(count):
            scheduled.append(
                {
                    "time": float(attack["time"]) + hit_index * interval,
                    "hit_index": hit_index + 1,
                    "attack": attack,
                }
            )
    scheduled.sort(key=lambda item: (item["time"], str(item["attack"].get("id", "")), item["hit_index"]))

    active_effects: list[dict] = []
    timeline: list[dict] = []
    unknowns: list[dict[str, str]] = []
    for scheduled_hit in scheduled:
        now = scheduled_hit["time"]
        attack = scheduled_hit["attack"]
        active_effects = [effect for effect in active_effects if effect["expires_at"] > now]
        resistance_reductions: dict[str, float] = {}
        da_reduction = 0.0
        for effect in active_effects:
            if effect["kind"] == "resistance_reduction":
                damage_type = effect["damage_type"]
                resistance_reductions[damage_type] = resistance_reductions.get(damage_type, 0) + effect["value"]
            elif effect["kind"] == "da_reduction":
                da_reduction += effect["value"]
            else:
                unknowns.append({"code": "unsupported_state_effect", "effect": effect["kind"]})
        current_build = deepcopy(build)
        current_build["defensive_ability"] = max(0.0, float(current_build["defensive_ability"]) - da_reduction)
        result = calculate_single_hit(current_build, attack["skill"], resistance_reductions=resistance_reductions)
        for warning in result["warnings"]:
            unknowns.append({"code": warning["code"], "effect": warning.get("field", "unknown")})
        applied: list[dict] = []
        for effect in attack.get("applies", []):
            normalized = {
                "kind": effect["kind"],
                "value": float(effect["value"]),
                "damage_type": effect.get("damage_type"),
                "source_attack_id": attack.get("id"),
                "applied_at": now,
                "expires_at": now + float(effect["duration_seconds"]),
            }
            active_effects.append(normalized)
            applied.append(normalized)
        timeline.append(
            {
                "time": now,
                "attack_id": attack.get("id"),
                "source": attack.get("source", attack.get("id")),
                "shape": attack.get("shape", "unknown"),
                "hit_index": scheduled_hit["hit_index"],
                "state_before": {
                    "resistance_reductions": resistance_reductions,
                    "da_reduction": da_reduction,
                    "active_effects": deepcopy(active_effects[:-len(applied)] if applied else active_effects),
                },
                "damage": result,
                "effects_applied_after_hit": applied,
            }
        )

    classification, evidence, confidence = classify_sequence(attack_definitions, timeline, unknowns)
    return {
        "schema_version": "0.1.0",
        "classification": classification,
        "confidence": confidence,
        "evidence": evidence,
        "timeline": timeline,
        "summary": {
            "hit_count": len(timeline),
            "worst_case_total_damage": sum(item["damage"]["health_delta"]["worst_case_damage"] for item in timeline),
            "expected_total_damage": sum(item["damage"]["range"].get("expected_per_attack", item["damage"]["range"]["expected_average"]) for item in timeline),
        },
        "unknowns": _deduplicate_dicts(unknowns),
    }


def classify_sequence(attacks: list[Mapping], timeline: list[Mapping], unknowns: list[Mapping]) -> tuple[str, list[str], str]:
    if unknowns:
        return "unknown", ["unsupported effects can change the result"], "low"
    if any(attack.get("recovery_available") is False for attack in attacks):
        return "recovery_failure", ["a required recovery action is explicitly unavailable"], "high"
    for index, hit in enumerate(timeline):
        for other in timeline[index + 1 :]:
            if other["time"] - hit["time"] > 0.10:
                break
            if other["source"] != hit["source"] or "ground" in {hit["shape"], other["shape"]}:
                return "overlap", ["distinct sources or ground damage land within 0.10 seconds"], "high"
    if any(int(attack.get("hit_count", 1)) > 1 and float(attack.get("hit_interval_seconds", 0)) <= 0.05 for attack in attacks):
        return "shotgun", ["multiple hit checks from one attack land within 0.05 seconds"], "high"
    if any(hit["state_before"]["active_effects"] for hit in timeline):
        return "combo", ["a follow-up hit is recalculated while a prior debuff is active"], "high"
    if len(timeline) == 1:
        return "single_hit", ["exactly one supported hit is present"], "high"
    return "combo", ["multiple sequential supported hits are present"], "medium"


def _deduplicate_dicts(values: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    result: list[dict[str, str]] = []
    for value in values:
        key = tuple(sorted(value.items()))
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def compare_observation(simulation: Mapping, observation: Mapping) -> dict:
    observed_hits = [event for event in observation.get("events", []) if event.get("kind") == "hit"]
    predicted_hits = simulation.get("timeline", [])
    comparisons: list[dict] = []
    for index in range(max(len(observed_hits), len(predicted_hits))):
        observed = observed_hits[index] if index < len(observed_hits) else None
        predicted = predicted_hits[index] if index < len(predicted_hits) else None
        predicted_value = predicted["damage"]["range"]["expected_average"] if predicted else None
        observed_value = observed.get("observed_value") if observed else None
        error = observed_value - predicted_value if observed_value is not None and predicted_value is not None else None
        comparisons.append(
            {
                "index": index,
                "observed": observed_value,
                "predicted": predicted_value,
                "absolute_error": abs(error) if error is not None else None,
                "relative_error": abs(error) / observed_value if error is not None and observed_value else None,
                "status": "matched" if observed is not None and predicted is not None else "unpaired",
            }
        )
    return {
        "schema_version": "0.1.0",
        "observation_method": observation.get("provenance", {}).get("method"),
        "comparisons": comparisons,
        "unpaired_observations": max(0, len(observed_hits) - len(predicted_hits)),
        "unpaired_predictions": max(0, len(predicted_hits) - len(observed_hits)),
    }
