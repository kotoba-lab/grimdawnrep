"""Lossless-enough normalized item view for agent-oriented queries."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping

ITEM_VIEW_RULE_ID = "item-view-v1"
ITEM_VIEW_V2_RULE_ID = "item-view-v2"

CLASS_TO_SLOT = {
    "WeaponMelee_Axe": "weapon_1h", "WeaponMelee_Dagger": "weapon_1h", "WeaponMelee_Mace": "weapon_1h", "WeaponMelee_Scepter": "weapon_1h", "WeaponMelee_Sword": "weapon_1h",
    "WeaponMelee_Axe2h": "weapon_2h", "WeaponMelee_Mace2h": "weapon_2h", "WeaponMelee_Spear2h": "weapon_2h", "WeaponMelee_Sword2h": "weapon_2h",
    "WeaponHunting_Ranged1h": "ranged_1h", "WeaponHunting_Ranged2h": "ranged_2h", "WeaponArmor_Shield": "shield", "WeaponArmor_Offhand": "offhand",
    "ArmorProtective_Head": "head", "ArmorProtective_Chest": "chest", "ArmorProtective_Shoulders": "shoulders", "ArmorProtective_Hands": "hands", "ArmorProtective_Legs": "legs", "ArmorProtective_Feet": "feet", "ArmorProtective_Waist": "waist",
    "ArmorJewelry_Amulet": "amulet", "ArmorJewelry_Ring": "ring", "ArmorJewelry_Medal": "medal",
    "ItemArtifact": "relic", "ItemRelic": "component", "ItemEnchantment": "augment", "ItemUsableSkill": "consumable", "ItemNote": "note", "QuestItem": "quest",
    "ItemFactionBooster": "consumable", "ItemFactionWarrant": "consumable", "ItemAttributeReset": "consumable", "ItemDevotionReset": "consumable", "ItemDifficultyUnlock": "consumable",
}

def _numeric(value: object) -> float | list[float] | None:
    if not isinstance(value, str): return None
    parts = value.split(";")
    try: values = [float(part) for part in parts]
    except ValueError: return None
    if not any(values): return None
    return values[0] if len(values) == 1 else values

def _record_ids(fields: Mapping, prefix: str) -> list[str]:
    result=[]
    for key, value in fields.items():
        if key == prefix or key.startswith(prefix):
            values = value if isinstance(value, list) else [value]
            for raw in values:
                if isinstance(raw, str):
                    result.extend(part.strip().replace("\\", "/").lower() for part in raw.split(";") if part.strip().lower().startswith("records/"))
    return sorted(set(result))

def _name(fields: Mapping, localization: Mapping[str, Mapping[str, str | None]]) -> dict:
    tags = [fields.get(field) for field in ("itemQualityTag", "itemStyleTag", "itemNameTag")]
    tags = [tag for tag in tags if isinstance(tag, str) and tag]
    result = {"tags": tags}
    unresolved = []
    for locale in ("en", "ja"):
        parts=[]
        for tag in tags:
            value = localization.get(locale, {}).get(tag)
            if value is None: unresolved.append({"locale": locale, "tag": tag})
            else: parts.append(value)
        result[locale] = " ".join(parts)
    if unresolved: result["unresolved_tags"] = unresolved
    return result

def build_item_view(dataset: Mapping) -> tuple[list[dict], list[dict]]:
    rows=[]; excluded=[]
    for record_id, record in sorted(dataset.get("records", {}).items()):
        if not record_id.startswith("records/items/"): continue
        fields=record["fields"]; item_class=fields.get("Class")
        slot=CLASS_TO_SLOT.get(item_class)
        if slot is None:
            excluded.append({"record": record_id, "class": item_class, "reason": "unknown_class"}); continue
        stats={key: parsed for key, value in fields.items() if (parsed := _numeric(value)) is not None}
        rows.append({"record": record_id, "rule": ITEM_VIEW_RULE_ID, "slot": slot, "classification": fields.get("itemClassification"), "item_level": _numeric(fields.get("itemLevel")), "level_requirement": _numeric(fields.get("levelRequirement")), "name": _name(fields, dataset.get("localization", {})), "stats": stats, "references": {"item_skill": next(iter(_record_ids(fields, "itemSkillName")), None), "augment_skills": _record_ids(fields, "augmentSkillName"), "modified_skills": _record_ids(fields, "modifiedSkillName") + _record_ids(fields, "modifierSkillName"), "item_set": next(iter(_record_ids(fields, "itemSetName")), None)}, "provenance": {key: record["provenance"].get(key) for key in ("source_layer", "overrides")}})
    return rows, excluded

def write_item_view(dataset: Mapping, output_root: Path) -> tuple[Path, Path, int, int]:
    rows, excluded = build_item_view(dataset); target=output_root / str(dataset["dataset_id"]); target.mkdir(parents=True, exist_ok=True)
    output=target / "items-v1.jsonl"; excluded_output=target / "items-v1.excluded.jsonl"
    output.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    excluded_output.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in excluded), encoding="utf-8")
    return output, excluded_output, len(rows), len(excluded)

def _resolved_name(fields: Mapping, localization: Mapping) -> dict:
    tag = next((fields.get(key) for key in ("skillDisplayName", "itemNameTag", "itemSetNameTag", "setNameTag", "setName") if isinstance(fields.get(key), str)), None)
    result={"tag": tag}
    for locale in ("en","ja"):
        result[locale]=localization.get(locale,{}).get(tag) if tag else None
    if tag and any(result[locale] is None for locale in ("en","ja")): result["unresolved_tags"]=[tag]
    return result

def _granted(records: Mapping, localization: Mapping, record_id: str, kind: str, unresolved: list[dict]) -> dict | None:
    record=records.get(record_id)
    if record is None:
        unresolved.append({"kind":kind,"record":record_id}); return None
    fields=record["fields"]
    return {"kind":kind,"record":record_id,"name":_resolved_name(fields,localization),"stats":{key:value for key, raw in fields.items() if (value:=_numeric(raw)) is not None}}

def build_item_view_v2(dataset: Mapping) -> tuple[list[dict], list[dict], list[dict]]:
    rows, excluded=build_item_view(dataset); records=dataset.get("records",{}); localization=dataset.get("localization",{})
    set_rows={}
    for row in rows:
        source=records[row["record"]]["fields"]; unresolved=[]; granted=[]
        for kind, key in (("item_skill","itemSkillName"),("augment_skill","augmentSkillName"),("modified_skill","modifiedSkillName"),("modifier_skill","modifierSkillName")):
            for reference in _record_ids(source,key):
                resolved=_granted(records,localization,reference,kind,unresolved)
                if resolved: granted.append(resolved)
        conversions=[]
        for source_record, fields in [(row["record"],source),*[(item["record"],records[item["record"]]["fields"]) for item in granted]]:
            suffixes=sorted({match.group(1) for key in fields for match in [re.fullmatch(r"conversion(?:InType|OutType|Percentage)(\d*)",key)] if match})
            for suffix in suffixes:
                conversions.append({"source_record": source_record, "index": int(suffix or "1"), "in":fields.get(f"conversionInType{suffix}"),"out":fields.get(f"conversionOutType{suffix}"),"percentage":_numeric(fields.get(f"conversionPercentage{suffix}"))})
        item_set=row["references"]["item_set"]
        if item_set:
            set_record=records.get(item_set)
            if set_record is None: unresolved.append({"kind":"item_set","record":item_set})
            else:
                set_name=_resolved_name(set_record["fields"],localization); row["references"]["item_set"]={"record":item_set,"name":set_name}
                bonuses=[]
                for key,value in set_record["fields"].items():
                    numeric=_numeric(value)
                    if isinstance(numeric,list): bonuses.append({"field":key,"stages":numeric})
                bonuses.sort(key=lambda bonus: bonus["field"])
                set_rows[item_set]={"record":item_set,"rule":ITEM_VIEW_V2_RULE_ID,"kind":"item_set","name":set_name,"bonuses":bonuses,"provenance":set_record["provenance"]}
        row["rule"]=ITEM_VIEW_V2_RULE_ID; row["granted"]={"references":granted,"conversions":conversions,"unresolved":unresolved}
    return rows,excluded,[set_rows[key] for key in sorted(set_rows)]

def write_item_view_v2(dataset: Mapping, output_root: Path) -> tuple[Path,Path,Path,int,int,int]:
    rows,excluded,sets=build_item_view_v2(dataset); target=output_root / str(dataset["dataset_id"]); target.mkdir(parents=True,exist_ok=True)
    paths=(target/"items-v2.jsonl",target/"items-v2.excluded.jsonl",target/"item-sets-v2.jsonl")
    for path,values in zip(paths,(rows,excluded,sets)): path.write_text("".join(json.dumps(value,ensure_ascii=False,sort_keys=True)+"\n" for value in values),encoding="utf8")
    return *paths,len(rows),len(excluded),len(sets)
