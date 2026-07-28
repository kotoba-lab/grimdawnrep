from __future__ import annotations

import hashlib
from html import unescape
import json
from pathlib import Path
import re
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class GrimToolsError(ValueError):
    pass


_ID = re.compile(r"^[A-Za-z0-9]{8}$")
_TITLE = re.compile(r"^(?P<class>.+?),\s*Level\s+(?P<level>\d+)\s+\(GD\s+(?P<version>[^)]+)\)\s+-\s+Grim Dawn Build Calculator$")


def canonical_build_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("grimtools.com", "www.grimtools.com"):
        raise GrimToolsError("invalid_grimtools_origin")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "calc" or not _ID.fullmatch(parts[1]):
        raise GrimToolsError("invalid_grimtools_build_url")
    build_id = parts[1]
    return build_id, f"https://www.grimtools.com/calc/{build_id}"


def _parse_title(html: str) -> re.Match[str]:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        raise GrimToolsError("missing_page_title")
    title = unescape(re.sub(r"\s+", " ", match.group(1))).strip()
    parsed = _TITLE.match(title)
    if not parsed:
        raise GrimToolsError("unsupported_page_title")
    return parsed


def _extract_build_info(html: str) -> dict[str, Any]:
    marker = "window['buildInfo']"
    start = html.find(marker)
    if start < 0:
        raise GrimToolsError("missing_build_info")
    equals = html.find("=", start + len(marker))
    if equals < 0:
        raise GrimToolsError("invalid_build_info")
    try:
        value, _ = json.JSONDecoder().raw_decode(html[equals + 1 :].lstrip())
    except json.JSONDecodeError as error:
        raise GrimToolsError("invalid_build_info") from error
    if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
        raise GrimToolsError("invalid_build_info")
    return value


def build_from_grimtools_html(url: str, html: str) -> dict[str, Any]:
    shared_id, canonical = canonical_build_url(url)
    title = _parse_title(html)
    info = _extract_build_info(html)
    data = info["data"]
    bio = data.get("bio", {})
    equipment = []
    for slot, item in sorted(data.get("equipment", {}).items()):
        equipment.append({
            "slot": slot,
            "base_external_id": item.get("item", ""),
            "prefix_external_id": item.get("prefix", ""),
            "suffix_external_id": item.get("suffix", ""),
            "component_external_id": item.get("component", ""),
            "augment_external_id": item.get("augment", ""),
            "relic_bonus_external_id": item.get("relicBonus", ""),
        })
    skills = [
        {
            "external_id": skill.get("name", ""),
            "level": skill.get("level", 0),
            "autocast_external_id": skill.get("autoCastSkill", ""),
        }
        for skill in data.get("skills", [])
    ]
    result: dict[str, Any] = {
        "schema_version": "0.1.0",
        "source": {"type": "grimtools_url", "id": shared_id, "url": canonical, "content_hash": hashlib.sha256(html.encode("utf-8")).hexdigest()},
        "character": {"level": int(bio.get("level", title["level"])), "class_display": title["class"], "mastery_external_ids": info.get("masteries", [])},
        "attributes": {key: bio.get(key) for key in ("physique", "cunning", "spirit")},
        "equipment": equipment,
        "skills": skills,
        "defense_snapshots": {},
        "game_version": title["version"],
        "unknowns": [{
            "code": "grimtools_external_ids_require_local_record_mapping",
            "detail": "The public build payload uses Grim Tools IDs; local DBR record identities and final defense values require a versioned mapping.",
        }],
        "references": {
            "build": canonical,
            "item_database": "https://www.grimtools.com/db/items/",
            "monster_database": "https://www.grimtools.com/monsterdb/",
        },
    }
    canonical_bytes = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result["build_id"] = hashlib.sha256(canonical_bytes).hexdigest()
    return result


_UPLOAD_SLOT_NAMES = {
    "ring1": "ring_1", "ring2": "ring_2",
    "weapon1": "weapon_1", "weapon2": "weapon_2",
    "weapon1Alt": "weapon_1_alt", "weapon2Alt": "weapon_2_alt",
}


def build_from_grimtools_upload_response(payload: dict[str, Any], *, source_hash: str | None = None) -> dict[str, Any]:
    """Normalize the official upload_save.php response without retaining the save."""
    data = payload.get("data")
    if not isinstance(data, dict):
        raise GrimToolsError("invalid_upload_response")
    bio = data.get("bio")
    equipment_data = data.get("equipment")
    skills_data = data.get("skills")
    if not isinstance(bio, dict) or not isinstance(equipment_data, dict) or not isinstance(skills_data, list):
        raise GrimToolsError("invalid_upload_response")
    equipment = []
    for original_slot, item in equipment_data.items():
        if not isinstance(item, dict) or not item:
            continue
        equipment.append({
            "slot": _UPLOAD_SLOT_NAMES.get(original_slot, original_slot),
            "base_external_id": item.get("item", ""),
            "prefix_external_id": item.get("prefix", ""),
            "suffix_external_id": item.get("suffix", ""),
            "component_external_id": item.get("component", ""),
            "augment_external_id": item.get("augment", ""),
            "relic_bonus_external_id": item.get("relicBonus", ""),
        })
    skills = [{
        "external_id": skill.get("name") or "",
        "level": int(skill.get("level", 0)),
        "enabled": bool(skill.get("enabled", 0)),
        "devotion_level": int(skill.get("devotionLevel", 0)),
        "autocast_external_id": skill.get("autoCastSkill") or "",
    } for skill in skills_data if isinstance(skill, dict)]
    result: dict[str, Any] = {
        "schema_version": "0.1.0",
        "source": {"type": "grimtools_save_upload", "source_hash": source_hash},
        "character": {"level": int(bio.get("level", 0))},
        "attributes": {key: bio.get(key) for key in ("physique", "cunning", "spirit", "health", "energy")},
        "equipment": equipment,
        "skills": skills,
        "defense_snapshots": {},
        "unknowns": [{
            "code": "grimtools_external_ids_require_local_record_mapping",
            "detail": "The official upload response uses Grim Tools IDs; same-save comparison can establish scoped record pairs.",
        }],
    }
    canonical_bytes = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result["build_id"] = hashlib.sha256(canonical_bytes).hexdigest()
    return result


def compare_same_save_builds(local: dict[str, Any], uploaded: dict[str, Any]) -> dict[str, Any]:
    """Compare two independent decoders of one save and expose scoped ID pairs."""
    checks = []
    checks.append({"field": "character.level", "local": local.get("character", {}).get("level"), "grimtools": uploaded.get("character", {}).get("level")})
    for key in ("physique", "cunning", "spirit", "health", "energy"):
        checks.append({"field": f"attributes.{key}", "local": local.get("attributes", {}).get(key), "grimtools": uploaded.get("attributes", {}).get(key)})
    for check in checks:
        check["matches"] = check["local"] == check["grimtools"]

    local_equipment = {item["slot"]: item for item in local.get("equipment", [])}
    uploaded_equipment = {item["slot"]: item for item in uploaded.get("equipment", [])}
    equipment_pairs = []
    for slot, item in sorted(local_equipment.items()):
        external = uploaded_equipment.get(slot)
        equipment_pairs.append({
            "slot": slot,
            "present_in_both": external is not None,
            "base_record": item.get("base_record", ""),
            "base_external_id": "" if external is None else external.get("base_external_id", ""),
        })

    local_skills = local.get("skills", [])
    uploaded_skills = uploaded.get("skills", [])
    skill_checks = []
    for index, (local_skill, external_skill) in enumerate(zip(local_skills, uploaded_skills)):
        matches = (
            int(local_skill.get("level", 0)) == int(external_skill.get("level", 0))
            and bool(local_skill.get("enabled", False)) == bool(external_skill.get("enabled", False))
            and int(local_skill.get("devotion_level", 0)) == int(external_skill.get("devotion_level", 0))
        )
        skill_checks.append({
            "index": index, "matches": matches,
            "local_record": local_skill.get("record", ""),
            "external_id": external_skill.get("external_id", ""),
            "local_level": local_skill.get("level", 0), "grimtools_level": external_skill.get("level", 0),
        })
    mismatches = [value for value in checks if not value["matches"]]
    mismatches.extend(value for value in equipment_pairs if not value["present_in_both"])
    mismatches.extend(value for value in skill_checks if not value["matches"])
    return {
        "equivalent_on_comparable_fields": not mismatches and len(local_skills) == len(uploaded_skills),
        "scalar_checks": checks,
        "equipment_pairs": equipment_pairs,
        "skill_checks": skill_checks,
        "counts": {
            "scalar_matches": sum(value["matches"] for value in checks), "scalar_total": len(checks),
            "equipment_slots_in_both": sum(value["present_in_both"] for value in equipment_pairs), "local_equipment_slots": len(equipment_pairs),
            "skill_matches": sum(value["matches"] for value in skill_checks), "local_skills": len(local_skills), "grimtools_skills": len(uploaded_skills),
        },
        "mismatches": mismatches,
        "boundary": "Opaque IDs are paired only for this exact save and are not claimed as a global versioned mapping.",
    }


def fetch_grimtools_build(
    url: str,
    *,
    cache_root: Path = Path("data/generated/grimtools-cache"),
    timeout: float = 10.0,
    max_age_seconds: int = 86400,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    shared_id, canonical = canonical_build_url(url)
    cache_root.mkdir(parents=True, exist_ok=True)
    cache = cache_root / f"{shared_id}.html"
    if cache.is_file() and time.time() - cache.stat().st_mtime <= max_age_seconds:
        html = cache.read_text(encoding="utf-8")
        result = build_from_grimtools_html(canonical, html)
        result["source"]["cache"] = "hit"
        return result
    request = Request(canonical, headers={"User-Agent": "Grim-Dawn-Encounter-Lab/0.1 (+read-only shared build metadata)"})
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
    except HTTPError as error:
        raise GrimToolsError(f"http_error:{error.code}") from error
    except (URLError, TimeoutError) as error:
        raise GrimToolsError("network_error") from error
    if len(raw) > 2_000_000:
        raise GrimToolsError("response_too_large")
    html = raw.decode("utf-8", errors="strict")
    result = build_from_grimtools_html(canonical, html)
    cache.write_text(html, encoding="utf-8")
    result["source"]["cache"] = "miss"
    return result
