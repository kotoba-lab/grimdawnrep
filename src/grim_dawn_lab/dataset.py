"""Versioned, deterministic datasets built from read-only DBR extractions."""

from __future__ import annotations

import csv
import ast
import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile
from typing import Iterable, Mapping

from grim_dawn_lab.arc import parse_localization_arc
from grim_dawn_lab.doctor import create_manifest, sha256_file


NORMALIZATION_RULE_ID = "dbr-lossless-v1"
ENCOUNTER_VIEW_RULE_ID = "monster-skill-view-v2"


def parse_dbr(path: Path) -> dict[str, str | list[str]]:
    """Parse ArchiveTool CSV-like DBR output without discarding duplicate keys."""
    fields: dict[str, str | list[str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        for row in csv.reader(source):
            if len(row) < 2 or not row[0]:
                continue
            key, value = row[0], row[1]
            previous = fields.get(key)
            if previous is None:
                fields[key] = value
            elif isinstance(previous, list):
                previous.append(value)
            else:
                fields[key] = [previous, value]
    return fields


def _portable_record_path(root: Path, record_id: str) -> Path:
    return root.joinpath(*record_id.split("/"))


def _references(fields: Mapping[str, str | list[str]]) -> list[str]:
    references: set[str] = set()
    for value in fields.values():
        values = value if isinstance(value, list) else [value]
        for item in values:
            for candidate in item.split(";"):
                normalized = candidate.strip().replace("\\", "/").lower()
                if normalized.startswith("records/") and normalized.endswith(".dbr"):
                    references.add(normalized)
    return sorted(references)


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def stable_input_manifest(manifest: Mapping) -> dict:
    """Remove observation time while retaining every input identity field."""
    return {key: value for key, value in manifest.items() if key != "generated_at"}


def evaluate_level_expression(expression: str, char_level: int) -> int:
    """Evaluate the arithmetic subset used by DBR level expressions."""
    operators = {
        ast.Add: lambda a, b: a + b,
        ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b,
        ast.Pow: lambda a, b: a**b,
    }

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id == "charLevel":
            return float(char_level)
        if isinstance(node, ast.BinOp) and type(node.op) in operators:
            return operators[type(node.op)](visit(node.left), visit(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        raise ValueError(f"unsupported level expression: {expression}")

    return max(1, math.floor(visit(ast.parse(expression.replace("^", "**"), mode="eval"))))


def evaluate_numeric_expression(expression: str, char_level: int) -> float:
    """Evaluate a level expression without integer coercion."""
    operators = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b, ast.Pow: lambda a, b: a**b}
    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression): return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)): return float(node.value)
        if isinstance(node, ast.Name) and node.id == "charLevel": return float(char_level)
        if isinstance(node, ast.BinOp) and type(node.op) in operators: return operators[type(node.op)](visit(node.left), visit(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand); return value if isinstance(node.op, ast.UAdd) else -value
        raise ValueError(f"unsupported numeric expression: {expression}")
    return visit(ast.parse(expression.replace("^", "**"), mode="eval"))


def _level_value(value: str | list[str] | None, level: int) -> float | None:
    if value is None or isinstance(value, list):
        return None
    values = [item for item in value.split(";") if item]
    if not values:
        return None
    try:
        return float(values[min(level - 1, len(values) - 1)])
    except ValueError:
        return None


def normalize_enemy_view(dataset: Mapping, record_id: str, enemy_level: int, difficulty: str, player_count: int) -> dict:
    records = dataset["records"]
    monster = records[record_id]
    fields = monster["fields"]
    difficulty_index = {"normal": 0, "elite": 4, "ultimate": 8}[difficulty] + player_count - 1
    adjustment_record_id = "records/game/balancingadjustment_mp+difficulty_enemies01.dbr"
    adjustment = records.get(adjustment_record_id, {}).get("fields", {})
    total_damage_modifier = _level_value(adjustment.get("offensiveTotalDamageModifier"), difficulty_index + 1) or 0.0
    name_tag = fields.get("description") if isinstance(fields.get("description"), str) else None
    names = {
        locale: values.get(name_tag) if name_tag else None
        for locale, values in dataset.get("localization", {}).items()
    }
    skills: list[dict] = []
    for index in range(1, 100):
        skill_id = fields.get(f"skillName{index}")
        if not isinstance(skill_id, str) or not skill_id:
            continue
        skill_id = skill_id.lower()
        raw_skill = records.get(skill_id)
        level_expression = fields.get(f"skillLevel{index}", "1")
        if not isinstance(level_expression, str):
            level_expression = "1"
        try:
            skill_level = evaluate_level_expression(level_expression, enemy_level)
        except (ValueError, SyntaxError, ZeroDivisionError):
            skill_level = None
        if raw_skill is None:
            skills.append({"record_id": skill_id, "skill_level": skill_level, "status": "missing"})
            continue
        skill_fields = raw_skill["fields"]
        packets: list[dict] = []
        for field_prefix, damage_type in (
            ("Physical", "physical"),
            ("Pierce", "pierce"),
            ("Fire", "fire"),
            ("Cold", "cold"),
            ("Lightning", "lightning"),
            ("Poison", "acid"),
            ("Life", "vitality"),
            ("Aether", "aether"),
            ("Chaos", "chaos"),
        ):
            if skill_level is None:
                continue
            minimum = _level_value(skill_fields.get(f"offensive{field_prefix}Min"), skill_level)
            maximum = _level_value(skill_fields.get(f"offensive{field_prefix}Max"), skill_level)
            if minimum is None or minimum <= 0:
                continue
            if maximum is None or maximum <= 0:
                maximum = minimum
            packets.append(
                {
                    "damage_type": damage_type,
                    "minimum": minimum,
                    "maximum": maximum,
                    "difficulty_scaled_minimum": minimum * (1 + total_damage_modifier / 100),
                    "difficulty_scaled_maximum": maximum * (1 + total_damage_modifier / 100),
                    "provenance": {
                        "record_id": skill_id,
                        "minimum_field": f"offensive{field_prefix}Min",
                        "maximum_field": f"offensive{field_prefix}Max",
                        "skill_level": skill_level,
                        "rule_id": "semicolon-array-one-based-v1",
                    },
                }
            )
        skills.append(
            {
                "record_id": skill_id,
                "skill_level": skill_level,
                "skill_level_expression": level_expression,
                "template": skill_fields.get("templateName"),
                "behavior": {
                    "cooldown_seconds": _level_value(skill_fields.get("skillCooldownTime"), skill_level or 1),
                    "target_radius": _level_value(skill_fields.get("skillTargetRadius"), skill_level or 1),
                    "projectile_count": _level_value(skill_fields.get("numProjectiles"), skill_level or 1),
                },
                "damage_packets": packets,
                "applies": _normalize_skill_state_effects(skill_fields, skill_level),
                "provenance": raw_skill["provenance"],
            }
        )
    attack_candidates: list[dict] = []
    for suffix in ("", *map(str, range(2, 20))):
        prefix = f"specialAttack{suffix}"
        skill_id = fields.get(f"{prefix}SkillName")
        if not isinstance(skill_id, str) or not skill_id:
            continue
        attack_candidates.append(
            {
                "slot": suffix or "1",
                "skill_record_id": skill_id.lower(),
                "initial_timeout_seconds": _level_value(fields.get(f"{prefix}Timeout"), 1),
                "delay_seconds": _level_value(fields.get(f"{prefix}Delay"), 1),
                "chance": (_level_value(fields.get(f"{prefix}Chance"), 1) or 0.0) / 100,
                "range": fields.get(f"{prefix}Range"),
                "provenance": {"record_id": record_id, "field_prefix": prefix, "rule_id": "monster-special-attack-slot-v1"},
            }
        )
    return {
        "record_id": record_id,
        "enemy_level": enemy_level,
        "difficulty": difficulty,
        "player_count": player_count,
        "classification": "nemesis" if "/nemesis/" in record_id else "unknown",
        "phase": fields.get("FileDescription") or f"phase:{Path(record_id).stem}",
        "name_tag": name_tag,
        "names": names,
        "controller_record_id": fields.get("controller"),
        "attributes": _normalize_enemy_attributes(records, fields, enemy_level, adjustment, difficulty_index),
        "difficulty_adjustment": {
            "record_id": adjustment_record_id,
            "array_index": difficulty_index,
            "offensive_total_damage_modifier_percent": total_damage_modifier,
            "rule_id": "difficulty-major-playercount-minor-v1",
        },
        "skills": skills,
        "attack_candidates": attack_candidates,
        "provenance": monster["provenance"],
        "normalization_rule_id": ENCOUNTER_VIEW_RULE_ID,
    }


def _normalize_skill_state_effects(fields: Mapping, skill_level: int | None) -> list[dict]:
    if skill_level is None:
        return []
    effects: list[dict] = []
    da_reduction = _level_value(fields.get("offensiveSlowDefensiveAbilityMin"), skill_level)
    da_duration = _level_value(fields.get("offensiveSlowDefensiveAbilityDurationMin"), skill_level)
    if da_reduction and da_reduction > 0 and da_duration and da_duration > 0:
        effects.append({
            "kind": "da_reduction", "value": da_reduction, "duration_seconds": da_duration,
            "provenance": {"fields": ["offensiveSlowDefensiveAbilityMin", "offensiveSlowDefensiveAbilityDurationMin"], "rule_id": "semicolon-array-one-based-v1"},
        })
    return effects


def _normalize_enemy_attributes(records: Mapping, monster_fields: Mapping, enemy_level: int, adjustment: Mapping, index: int) -> dict:
    equation_id = monster_fields.get("characterAttributeEquations")
    equation_fields = records.get(equation_id, {}).get("fields", {}) if isinstance(equation_id, str) else {}
    result: dict[str, dict] = {}
    for output_name, field in (("offensive_ability", "characterOffensiveAbility"), ("defensive_ability", "characterDefensiveAbility"), ("health", "characterLife")):
        expression = equation_fields.get(field)
        if not isinstance(expression, str):
            continue
        try:
            base = evaluate_numeric_expression(expression, enemy_level)
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
            continue
        flat = _level_value(adjustment.get(field), index + 1) or 0.0
        modifier = _level_value(adjustment.get(f"{field}Modifier"), index + 1) or 0.0
        result[output_name] = {
            "value": (base + flat) * (1 + modifier / 100),
            "base": base,
            "flat_adjustment": flat,
            "percent_modifier": modifier,
            "provenance": {"equation_record_id": equation_id, "field": field, "expression": expression, "rule_id": "base-plus-flat-times-percent-v1"},
        }
    return result


def build_dataset_from_dbr_roots(
    roots: Iterable[tuple[str, Path]],
    selected_records: Iterable[str],
    *,
    input_manifest: Mapping | None = None,
    localization_arcs: Mapping[str, Path | Iterable[Path]] | None = None,
    enemy_level: int = 100,
    difficulty: str = "ultimate",
    player_count: int = 1,
) -> dict:
    """Merge DBR roots in order and follow references from selected records."""
    layers = [(name, root.resolve()) for name, root in roots]
    if difficulty not in {"normal", "elite", "ultimate"} or not 1 <= player_count <= 4:
        raise ValueError("difficulty/player_count is out of range")
    queue = [record.replace("\\", "/").lower() for record in selected_records]
    support = "records/game/gameengine.dbr"
    if any(_portable_record_path(root, support).is_file() for _, root in layers):
        queue.append(support)
    seen: set[str] = set()
    records: dict[str, dict] = {}
    missing: list[str] = []
    while queue:
        record_id = queue.pop(0)
        if record_id in seen:
            continue
        seen.add(record_id)
        matches: list[tuple[str, Path]] = []
        for layer, root in layers:
            candidate = _portable_record_path(root, record_id)
            if candidate.is_file():
                matches.append((layer, candidate))
        if not matches:
            missing.append(record_id)
            continue
        source_layer, source_path = matches[-1]
        fields = parse_dbr(source_path)
        refs = _references(fields)
        queue.extend(ref for ref in refs if ref not in seen)
        records[record_id] = {
            "fields": fields,
            "references": refs,
            "provenance": {
                "source_layer": source_layer,
                "source_record": record_id,
                "overrides": [layer for layer, _ in matches[:-1]],
                "normalization_rule_id": NORMALIZATION_RULE_ID,
            },
        }
    referenced_tags = sorted(
        {
            value
            for record in records.values()
            for field_value in record["fields"].values()
            for value in (field_value if isinstance(field_value, list) else [field_value])
            if value.startswith("tag")
        }
    )
    localization: dict[str, dict[str, str | None]] = {}
    for locale, archives in sorted((localization_arcs or {}).items()):
        archive_paths = [archives] if isinstance(archives, Path) else list(archives)
        tags: dict[str, str] = {}
        for archive in archive_paths:
            tags.update(parse_localization_arc(archive))
        localization[locale] = {tag: tags.get(tag) for tag in referenced_tags}
    core = {
        "schema_version": "0.1.0",
        "normalization_rule_id": NORMALIZATION_RULE_ID,
        "layer_precedence": [name for name, _ in layers],
        "selected_records": sorted(record.replace("\\", "/").lower() for record in selected_records),
        "records": dict(sorted(records.items())),
        "missing_references": sorted(missing),
        "referenced_tags": referenced_tags,
        "localization": localization,
        "input_manifest": input_manifest,
    }
    core["views"] = {
        "enemies": {
            record_id: normalize_enemy_view(core, record_id, enemy_level, difficulty, player_count)
            for record_id in core["selected_records"]
            if record_id in records and records[record_id]["fields"].get("Class") == "Monster"
        }
    }
    digest = hashlib.sha256(canonical_json(core)).hexdigest()
    return {**core, "dataset_id": digest, "content_sha256": digest}


def write_versioned_dataset(dataset: Mapping, output_root: Path) -> Path:
    target = output_root / str(dataset["dataset_id"])
    target.mkdir(parents=True, exist_ok=True)
    output = target / "dataset.json"
    rendered = json.dumps(dataset, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if output.exists() and output.read_text(encoding="utf-8") != rendered:
        raise RuntimeError(f"dataset id collision at {output}")
    output.write_text(rendered, encoding="utf-8")
    return output


def diff_datasets(previous: Mapping, current: Mapping) -> dict:
    before = previous.get("records", {})
    after = current.get("records", {})
    before_ids, after_ids = set(before), set(after)
    changed: dict[str, dict] = {}
    for record_id in sorted(before_ids & after_ids):
        old_fields = before[record_id]["fields"]
        new_fields = after[record_id]["fields"]
        old_keys, new_keys = set(old_fields), set(new_fields)
        modified = {
            key: {"before": old_fields[key], "after": new_fields[key]}
            for key in sorted(old_keys & new_keys)
            if old_fields[key] != new_fields[key]
        }
        if modified or old_keys != new_keys:
            changed[record_id] = {
                "fields_added": sorted(new_keys - old_keys),
                "fields_removed": sorted(old_keys - new_keys),
                "fields_modified": modified,
            }
    diff = {
        "schema_version": "0.1.0",
        "previous_dataset_id": previous.get("dataset_id"),
        "current_dataset_id": current.get("dataset_id"),
        "records_added": sorted(after_ids - before_ids),
        "records_removed": sorted(before_ids - after_ids),
        "records_changed": changed,
    }
    diff["revalidation_queue"] = build_revalidation_queue(diff)
    return diff


def build_revalidation_queue(diff: Mapping) -> list[dict[str, str | list[str]]]:
    queue: list[dict[str, str | list[str]]] = []
    for record_id in diff.get("records_added", []):
        queue.append({"record": record_id, "reason": "record_added", "claims": ["reference_closure", "encounter_coverage"]})
    for record_id in diff.get("records_removed", []):
        queue.append({"record": record_id, "reason": "record_removed", "claims": ["reference_closure", "saved_scenarios"]})
    for record_id, change in sorted(diff.get("records_changed", {}).items()):
        fields = sorted(set(change.get("fields_added", [])) | set(change.get("fields_removed", [])) | set(change.get("fields_modified", {})))
        lowered = " ".join(fields).lower()
        claims = {"normalized_view"}
        if any(token in lowered for token in ("offensive", "defensive", "damage", "armor", "character")):
            claims.update(("single_hit_damage", "encounter_ranking", "sensitivity"))
        if any(token in lowered for token in ("timeout", "delay", "chance", "duration", "period", "controller")):
            claims.update(("attack_timeline", "classification"))
        if any(token in lowered for token in ("skill", "template", "spawn", "pet")):
            claims.add("reference_closure")
        queue.append({"record": record_id, "reason": "fields_changed", "fields": fields, "claims": sorted(claims)})
    return queue


def extract_arz(archive_tool: Path, arz: Path, output: Path) -> None:
    """Invoke the owner-supplied ArchiveTool without modifying its inputs."""
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(mode="w+b") as stdout, tempfile.TemporaryFile(mode="w+b") as stderr:
        completed = subprocess.run(
            [str(archive_tool), str(arz), "-database", str(output.resolve())],
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
        records = output / "records"
        has_records = records.is_dir() and any(records.rglob("*.dbr"))
        stdout.seek(0, 2)
        stdout.seek(max(0, stdout.tell() - 4096))
        completed_signal = b"Operation completed" in stdout.read()
        # ArchiveTool 1.3.0.0 returns -1 despite writing a complete DBR tree.
        # Accept only that observed code and only with the expected structure.
        accepted = completed.returncode == 0 and has_records
        accepted = accepted or (completed.returncode == -1 and has_records and completed_signal)
        if not accepted:
            stderr.seek(0, 2)
            size = stderr.tell()
            stderr.seek(max(0, size - 4096))
            detail = stderr.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ArchiveTool failed ({completed.returncode}) for {arz}; records={has_records}; completed={completed_signal}: {detail[-4096:]}")


def extract_and_build_dataset(
    install_path: Path,
    work_root: Path,
    output_root: Path,
    selected_records: Iterable[str],
    *,
    channel: str = "unknown",
    enemy_level: int = 100,
    difficulty: str = "ultimate",
    player_count: int = 1,
) -> tuple[dict, Path]:
    """Regenerate a dataset from owner-supplied game files in one read-only flow."""
    before = create_manifest(install_path, channel=channel)
    if before["warnings"]:
        raise RuntimeError(f"game input diagnostics failed: {before['warnings']}")
    stable_manifest = stable_input_manifest(before)
    input_id = hashlib.sha256(canonical_json(stable_manifest)).hexdigest()
    archive_tool = install_path / "ArchiveTool.exe"
    if not archive_tool.is_file():
        raise RuntimeError(f"ArchiveTool.exe is missing: {archive_tool}")
    layer_archives = [
        ("base", install_path / "database" / "database.arz"),
        ("gdx1", install_path / "gdx1" / "database" / "GDX1.arz"),
        ("gdx2", install_path / "gdx2" / "database" / "GDX2.arz"),
        ("gdx3", install_path / "gdx3" / "database" / "GDX3.arz"),
    ]
    roots: list[tuple[str, Path]] = []
    for layer, archive in layer_archives:
        if not archive.is_file():
            continue
        target = work_root / input_id / layer
        marker = target / ".extraction.json"
        expected = {"archive_sha256": sha256_file(archive), "archive_size": archive.stat().st_size}
        cached = False
        if marker.is_file():
            try:
                cached = json.loads(marker.read_text(encoding="utf-8")) == expected and any(target.rglob("*.dbr"))
            except (OSError, json.JSONDecodeError):
                cached = False
        if not cached:
            extract_arz(archive_tool, archive, target)
            marker.write_text(json.dumps(expected, sort_keys=True) + "\n", encoding="utf-8")
        roots.append((layer, target))

    localization: dict[str, list[Path]] = {
        "en": [
            install_path / "resources" / "Text_EN.arc",
            install_path / "gdx1" / "resources" / "Text_EN.arc",
            install_path / "gdx2" / "resources" / "Text_EN.arc",
            install_path / "gdx3" / "resources" / "Text_EN.arc",
        ],
        "ja": [install_path / "resources" / "Text_JA.arc"],
    }
    localization = {locale: [path for path in paths if path.is_file()] for locale, paths in localization.items()}
    dataset = build_dataset_from_dbr_roots(
        roots,
        selected_records,
        input_manifest=stable_manifest,
        localization_arcs=localization,
        enemy_level=enemy_level,
        difficulty=difficulty,
        player_count=player_count,
    )
    output = write_versioned_dataset(dataset, output_root)
    after = stable_input_manifest(create_manifest(install_path, channel=channel))
    if stable_manifest != after:
        raise RuntimeError("game input identity changed during extraction")
    return dataset, output
