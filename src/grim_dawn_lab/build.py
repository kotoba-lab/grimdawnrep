from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from grim_dawn_lab.dataset import parse_dbr


EQUIPMENT_SLOTS = (
    "head", "amulet", "chest", "legs", "feet", "hands",
    "ring_1", "ring_2", "waist", "shoulders", "medal", "relic",
)


def _item_identity(item: dict[str, Any], slot: str) -> dict[str, Any]:
    return {
        "slot": slot,
        "base_record": item.get("base", ""),
        "prefix_record": item.get("prefix", ""),
        "suffix_record": item.get("suffix", ""),
        "modifier_record": item.get("modifier", ""),
        "transmute_record": item.get("transmute", ""),
        "component_record": item.get("component", ""),
        "component_bonus_record": item.get("component_bonus", ""),
        "augment_record": item.get("augment", ""),
    }


def build_from_gdc(parsed: dict[str, Any], *, include_character_name: bool = False) -> dict[str, Any]:
    inventory = parsed.get("inventory") or {}
    skills_block = parsed.get("skills") or {}
    attributes = parsed.get("attributes") or {}
    equipment = inventory.get("equipment") or []
    skills = [
        {
            "record": skill["record"],
            "level": skill["level"],
            "sublevel": skill["sublevel"],
            "enabled": skill["enabled"],
            "devotion_level": skill["devotion_level"],
        }
        for skill in skills_block.get("skills", [])
    ]
    source_hash = parsed["provenance"]["source_hash"]
    result: dict[str, Any] = {
        "schema_version": "0.1.0",
        "source": {"type": "player_gdc", "hash": source_hash},
        "character": {
            "level": parsed["header"]["level"],
            "class_record": parsed["header"]["class_record"],
            "hardcore": parsed["header"]["hardcore"],
            "expansion_character": parsed["header"]["expansion_character"],
        },
        "attributes": {
            key: attributes.get(key)
            for key in ("physique", "cunning", "spirit", "health", "energy")
        },
        "equipment": [
            _item_identity(item, EQUIPMENT_SLOTS[index] if index < len(EQUIPMENT_SLOTS) else f"slot_{index}")
            for index, item in enumerate(equipment)
            if item.get("base")
        ],
        "skills": skills,
        "defense_snapshots": {},
        "unknowns": [
            {
                "code": "derived_defenses_require_game_record_resolution",
                "detail": "The save stores record identities and allocations, not final DA, armor, resistances, or active buff state.",
            }
        ],
    }
    if include_character_name:
        result["character"]["name"] = parsed["header"]["character_name"]
    canonical = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result["build_id"] = hashlib.sha256(canonical).hexdigest()
    return result


def diff_builds(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    ignored = {"build_id", "source", "unknowns"}
    keys = sorted((set(left) | set(right)) - ignored)
    differences = [
        {"field": key, "left": left.get(key), "right": right.get(key)}
        for key in keys
        if left.get(key) != right.get(key)
    ]
    return {"equivalent": not differences, "differences": differences}


_RESISTANCE_FIELDS = {
    "fire": "defensiveFire", "cold": "defensiveCold", "lightning": "defensiveLightning",
    "acid": "defensivePoison", "pierce": "defensivePierce", "bleeding": "defensiveBleeding",
    "vitality": "defensiveLife", "aether": "defensiveAether", "chaos": "defensiveChaos",
    "physical": "defensivePhysical",
}


def _number(value: Any, level: int | None = None) -> float:
    if isinstance(value, list):
        value = value[-1]
    if isinstance(value, str) and ";" in value:
        values = value.split(";")
        index = 0 if level is None else max(0, min(level, len(values)) - 1)
        value = values[index]
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _find_record(roots: list[tuple[str, Path]], record: str) -> tuple[str, Path] | None:
    relative = Path(*record.replace("\\", "/").split("/"))
    for layer, root in reversed(roots):
        path = root / relative
        if path.is_file():
            return layer, path
    return None


def resolve_equipment_defenses(build: dict[str, Any], roots: list[tuple[str, Path]]) -> dict[str, Any]:
    """Resolve only unconditional static equipment fields; never claim a final sheet value."""
    totals = {name: 0.0 for name in _RESISTANCE_FIELDS}
    armor = {slot: 0.0 for slot in ("head", "shoulders", "chest", "hands", "legs", "feet")}
    da_flat = da_percent = health_flat = absorption_bonus = physique_flat = physique_percent = 0.0
    unresolved: list[dict[str, str]] = []
    evidence: list[dict[str, str]] = []
    record_keys = ("base_record", "prefix_record", "suffix_record", "component_record", "component_bonus_record", "augment_record")
    for item in build.get("equipment", []):
        for key in record_keys:
            record = item.get(key)
            if not record:
                continue
            found = _find_record(roots, record)
            if not found:
                unresolved.append({"code": "missing_record", "record": record})
                continue
            layer, path = found
            fields = parse_dbr(path)
            evidence.append({"record": record, "layer": layer})
            elemental = _number(fields.get("defensiveElementalResistance"))
            for name, field in _RESISTANCE_FIELDS.items():
                totals[name] += _number(fields.get(field))
            for name in ("fire", "cold", "lightning"):
                totals[name] += elemental
            da_flat += _number(fields.get("characterDefensiveAbility"))
            da_percent += _number(fields.get("characterDefensiveAbilityModifier"))
            health_flat += _number(fields.get("characterLife"))
            physique_flat += _number(fields.get("characterStrength"))
            physique_percent += _number(fields.get("characterStrengthModifier"))
            absorption_bonus += _number(fields.get("defensiveAbsorption"))
            if key == "base_record" and item["slot"] in armor:
                armor[item["slot"]] += _number(fields.get("defensiveProtection"))
    snapshot = {
        "status": "partial",
        "scope": "unconditional_static_equipment_only",
        "resistance_percent_points": totals,
        "armor_by_slot": armor,
        "defensive_ability_flat": da_flat,
        "defensive_ability_percent": da_percent,
        "health_flat": health_flat,
        "physique_flat": physique_flat,
        "physique_percent": physique_percent,
        "armor_absorption_bonus_percent_points": absorption_bonus,
        "evidence": evidence,
        "unknowns": unresolved + [
            {"code": "base_character_and_attribute_formulas_not_applied"},
            {"code": "skill_devotion_and_conditional_effects_not_applied"},
            {"code": "random_seed_scaled_values_not_applied"},
        ],
    }
    build["defense_snapshots"]["equipment_static"] = snapshot
    return build


def resolve_baseline_defenses(build: dict[str, Any], roots: list[tuple[str, Path]]) -> dict[str, Any]:
    resolve_equipment_defenses(build, roots)
    equipment = build["defense_snapshots"]["equipment_static"]
    resistances = dict(equipment["resistance_percent_points"])
    da_flat = equipment["defensive_ability_flat"]
    da_percent = equipment["defensive_ability_percent"]
    health_flat = equipment["health_flat"]
    physique_flat = equipment["physique_flat"]
    physique_percent = equipment["physique_percent"]
    evidence = list(equipment["evidence"])
    unresolved = [value for value in equipment["unknowns"] if value["code"] == "missing_record"]
    conditional: list[dict[str, Any]] = []
    for skill in build.get("skills", []):
        level = max(int(skill.get("level", 0)), int(skill.get("devotion_level", 0)))
        if level <= 0 or not skill.get("enabled", True):
            continue
        found = _find_record(roots, skill["record"])
        if not found:
            unresolved.append({"code": "missing_record", "record": skill["record"]})
            continue
        layer, path = found
        fields = parse_dbr(path)
        skill_class = str(fields.get("Class", ""))
        if skill_class != "Skill_Passive":
            if any(_number(fields.get(field), level) for field in list(_RESISTANCE_FIELDS.values()) + ["characterDefensiveAbility", "characterLife", "defensiveProtection"]):
                conditional.append({"record": skill["record"], "class": skill_class, "level": level})
            continue
        evidence.append({"record": skill["record"], "layer": layer})
        elemental = _number(fields.get("defensiveElementalResistance"), level)
        for name, field in _RESISTANCE_FIELDS.items():
            resistances[name] += _number(fields.get(field), level)
        for name in ("fire", "cold", "lightning"):
            resistances[name] += elemental
        da_flat += _number(fields.get("characterDefensiveAbility"), level)
        da_percent += _number(fields.get("characterDefensiveAbilityModifier"), level)
        health_flat += _number(fields.get("characterLife"), level)
        physique_flat += _number(fields.get("characterStrength"), level)
        physique_percent += _number(fields.get("characterStrengthModifier"), level)
    base_physique = float(build.get("attributes", {}).get("physique") or 0)
    calculated_physique = (base_physique + physique_flat) * (1 + physique_percent / 100)
    level = int(build["character"]["level"])
    defensive_ability = (da_flat + level * 12 + calculated_physique * 0.5) * (1 + da_percent / 100) + 53
    snapshot = {
        "status": "approximate",
        "scope": "unconditional_equipment_and_passive_skills",
        "defensive_ability": defensive_ability,
        "resistance_percent_points_uncapped": resistances,
        "armor_by_slot": equipment["armor_by_slot"],
        "health_saved_base_plus_flat": float(build.get("attributes", {}).get("health") or 0) + health_flat,
        "formula_trace": {
            "defensive_ability_equation": "(flatDA + characterLevel*12 + calculatedPhysique*0.5) * (1 + daPercent/100) + 53",
            "source_record": "records/game/combatformulas.dbr",
            "inputs": {"flat_da": da_flat, "level": level, "calculated_physique": calculated_physique, "da_percent": da_percent},
        },
        "conditional_or_toggled_skills": conditional,
        "evidence": evidence,
        "unknowns": unresolved + [
            {"code": "attribute_bonus_rounding_and_bonusDV_mapping_unverified"},
            {"code": "conditional_toggled_and_temporary_buffs_excluded"},
            {"code": "item_seed_variation_unverified"},
        ],
    }
    armor_slots = equipment["armor_by_slot"]
    snapshot["combat_snapshot"] = {
        "schema_version": "0.1.0", "game_version": "unknown_from_save", "level": level,
        "health": max(1.0, snapshot["health_saved_base_plus_flat"]), "defensive_ability": defensive_ability,
        "physical_resistance": resistances["physical"] / 100,
        "resistances": {name: {"current": min(80.0, value), "maximum": 80.0, "uncapped": value} for name, value in resistances.items() if name != "physical"},
        "armor": {
            "absorption": min(1.0, (70 + equipment["armor_absorption_bonus_percent_points"]) / 100),
            "slots": {"head": armor_slots["head"], "shoulders": armor_slots["shoulders"], "torso": armor_slots["chest"], "arms": armor_slots["hands"], "legs": armor_slots["legs"], "feet": armor_slots["feet"]},
        },
        "provenance": {"source": "player_gdc", "source_hash": build["source"]["hash"]}, "active_effects": [],
    }
    build["defense_snapshots"]["baseline"] = snapshot
    return build
