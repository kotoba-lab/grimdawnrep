from __future__ import annotations

from copy import deepcopy
from typing import Any

from grim_dawn_lab.timeline import simulate_attack_sequence


def scenarios_from_dataset(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    scenarios = []
    for enemy_id, enemy in sorted(dataset.get("views", {}).get("enemies", {}).items()):
        enemy_start = len(scenarios)
        for skill in enemy.get("skills", []):
            packets = skill.get("damage_packets", [])
            if not packets:
                continue
            normalized_packets = [
                {
                    "damage_type": {"poison": "acid", "acid_poison": "acid"}.get(packet["damage_type"], packet["damage_type"]),
                    "minimum": packet.get("difficulty_scaled_minimum", packet["minimum"]),
                    "maximum": packet.get("difficulty_scaled_maximum", packet["maximum"]),
                }
                for packet in packets
            ]
            behavior = skill.get("behavior", {})
            candidate = next((value for value in enemy.get("attack_candidates", []) if value["skill_record_id"] == skill["record_id"]), None)
            projectile_count = int(behavior.get("projectile_count") or 1)
            template = str(skill.get("template", ""))
            attack = {
                "id": skill["record_id"], "source": enemy_id, "time": float((candidate or {}).get("initial_timeout_seconds") or 0.0),
                "shape": "projectile" if "projectile" in template else "direct",
                "hit_count": projectile_count, "hit_interval_seconds": 0.0,
                "skill": {"offensive_ability": enemy["attributes"]["offensive_ability"]["value"], "damage_packets": normalized_packets},
                "applies": skill.get("applies", []),
            }
            scenarios.append({
                "id": f"{enemy_id}::{skill['record_id']}",
                "enemy": enemy.get("names", {}).get("en") or enemy_id,
                "phase": enemy.get("phase") or enemy_id,
                "attacks": [attack],
                "references": {"monster_database": "https://www.grimtools.com/monsterdb/"},
                "unknowns": [{"code": "positioning_and_actual_projectile_contacts_unknown"}] if projectile_count > 1 else [],
            })
            if candidate:
                scenarios[-1]["behavior"] = {
                    "initial_timeout_seconds": candidate["initial_timeout_seconds"],
                    "reuse_delay_seconds": candidate["delay_seconds"],
                    "use_chance": candidate["chance"],
                    "range": candidate["range"],
                    "cooldown_seconds": behavior.get("cooldown_seconds"),
                }
        enemy_scenarios = scenarios[enemy_start:]
        for debuff_scenario in [value for value in enemy_scenarios if value["attacks"][0].get("applies")]:
            alternatives = [value for value in enemy_scenarios if value is not debuff_scenario]
            if not alternatives:
                continue
            followup_scenario = max(
                alternatives,
                key=lambda value: sum(packet["maximum"] for packet in value["attacks"][0]["skill"]["damage_packets"]) * int(value["attacks"][0].get("hit_count", 1)),
            )
            first = deepcopy(debuff_scenario["attacks"][0])
            followup = deepcopy(followup_scenario["attacks"][0])
            duration = min(effect["duration_seconds"] for effect in first["applies"])
            first["time"] = 0.0
            followup["time"] = max(0.1, duration / 2)
            scenarios.append({
                "id": f"{debuff_scenario['id']}::followed-by::{followup['id']}",
                "enemy": debuff_scenario["enemy"], "phase": debuff_scenario["phase"],
                "attacks": [first, followup], "confidence_override": "low",
                "references": debuff_scenario["references"],
                "unknowns": [{"code": "ai_order_and_contact_timing_not_guaranteed"}],
                "behavior": {"kind": "debuff_followup_window", "debuff_duration_seconds": duration},
            })
    return scenarios


def _damage(result: dict[str, Any]) -> float:
    return float(result["summary"]["worst_case_total_damage"])


def _mutations(build: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    candidates: list[tuple[str, dict[str, Any]]] = []
    da = deepcopy(build)
    da["defensive_ability"] = float(da.get("defensive_ability", 0)) + 100
    candidates.append(("defensive_ability:+100", da))
    health = deepcopy(build)
    health["health"] = float(health["health"]) * 1.1
    candidates.append(("health:+10%", health))
    armor = deepcopy(build)
    for slot in armor.get("armor", {}).get("slots", {}):
        armor["armor"]["slots"][slot] = float(armor["armor"]["slots"][slot]) + 200
    candidates.append(("armor_all_slots:+200", armor))
    absorb = deepcopy(build)
    absorb.setdefault("absorption", {})["percent"] = min(1.0, float(absorb.get("absorption", {}).get("percent", 0)) + 0.05)
    candidates.append(("damage_absorption:+5pp", absorb))
    for damage_type in sorted(build.get("resistances", {})):
        resistance = deepcopy(build)
        entry = resistance["resistances"][damage_type]
        if "uncapped" in entry:
            entry["uncapped"] = float(entry["uncapped"]) + 10
        else:
            entry["current"] = float(entry.get("current", 0)) + 10
        candidates.append((f"{damage_type}_resistance:+10pp", resistance))
    return candidates


def analyze_encounters(build: dict[str, Any], scenarios: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
    source_build = build
    if "health" not in build and build.get("defense_snapshots", {}).get("baseline", {}).get("combat_snapshot"):
        build = build["defense_snapshots"]["baseline"]["combat_snapshot"]
    required_context = ("game_version", "channel", "difficulty", "enemy_level", "content")
    missing_context = [key for key in required_context if key not in context]
    if missing_context:
        raise ValueError(f"missing_context:{','.join(missing_context)}")
    ranked = []
    all_unknowns: list[dict[str, Any]] = []
    for scenario in scenarios:
        result = simulate_attack_sequence(build, scenario["attacks"])
        base_damage = _damage(result)
        sensitivities = []
        for label, candidate in _mutations(build):
            changed = _damage(simulate_attack_sequence(candidate, scenario["attacks"]))
            changed_fraction = changed / float(candidate["health"])
            sensitivities.append({
                "change": label,
                "worst_case_damage": changed,
                "damage_reduction": base_damage - changed,
                "damage_reduction_percent": 0.0 if base_damage == 0 else (base_damage - changed) / base_damage * 100,
                "health_fraction": changed_fraction,
                "health_fraction_reduction": base_damage / float(build["health"]) - changed_fraction,
            })
        sensitivities.sort(key=lambda value: (-value["health_fraction_reduction"], value["change"]))
        row = {
            "scenario_id": scenario["id"],
            "enemy": scenario["enemy"],
            "phase": scenario["phase"],
            "classification": result["classification"],
            "confidence": scenario.get("confidence_override", result["confidence"]),
            "worst_case_damage": base_damage,
            "expected_damage": result["summary"]["expected_total_damage"],
            "health_fraction": base_damage / float(build["health"]),
            "lethal_worst_case": base_damage >= float(build["health"]),
            "timeline": result["timeline"],
            "evidence": result["evidence"],
            "improvement_candidates": sensitivities,
            "unknowns": result["unknowns"] + scenario.get("unknowns", []),
            "references": scenario.get("references", {}),
        }
        all_unknowns.extend({"scenario_id": scenario["id"], **unknown} for unknown in row["unknowns"])
        ranked.append(row)
    ranked.sort(key=lambda row: (-row["health_fraction"], row["scenario_id"]))
    for index, row in enumerate(ranked, 1):
        row["rank"] = index
    return {
        "schema_version": "0.1.0",
        "context": context,
        "build": {"schema_version": build.get("schema_version"), "provenance": build.get("provenance"), "build_id": source_build.get("build_id")},
        "ranking": ranked,
        "unknowns": all_unknowns,
        "evidence_policy": {
            "game_data": "scenario records and normalized fields",
            "official_specification": "combat formula trace",
            "observation": "Observation comparisons when supplied separately",
            "estimate": "only when explicitly labeled",
            "unknown": "never silently approximated",
        },
    }


def render_advisor_markdown(report: dict[str, Any]) -> str:
    context = report["context"]
    lines = [
        "# Grim Dawn Encounter Advisor",
        "",
        f"Game {context['game_version']} / {context['channel']} / {context['difficulty']} / enemy level {context['enemy_level']} / {context['content']}",
        "",
    ]
    for row in report["ranking"]:
        lines.extend([
            f"## {row['rank']}. {row['enemy']} — {row['phase']} ({row['classification']})",
            "",
            f"Worst case: {row['worst_case_damage']:.1f} damage ({row['health_fraction'] * 100:.1f}% of health). Confidence: {row['confidence']}.",
            "",
        ])
        if row["improvement_candidates"]:
            best = row["improvement_candidates"][0]
            lines.extend([
                f"Best tested change: `{best['change']}` → {best['health_fraction'] * 100:.1f}% of health ({best['health_fraction_reduction'] * 100:.1f} percentage-point improvement).",
                "",
            ])
        if row["unknowns"]:
            lines.append("Unknowns: " + ", ".join(value["code"] for value in row["unknowns"]) + ".")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
