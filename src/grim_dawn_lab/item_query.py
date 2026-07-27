"""Filtering and rendering for item-view JSON Lines files."""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Iterable

_STAT = re.compile(r"^([^<>=]+?)(?:\s*(>=|<=|>|<|=)\s*(-?\d+(?:\.\d+)?))?$")

def parse_stat_filter(raw: str) -> tuple[str, str | None, float | None]:
    match=_STAT.fullmatch(raw.strip())
    if not match: raise ValueError(f"invalid --stat: {raw}")
    return match.group(1).strip(), match.group(2), float(match.group(3)) if match.group(3) is not None else None

def _number(value: object) -> float | None:
    if isinstance(value, (int,float)): return float(value)
    return None

def _stat_value(row: dict, field: str) -> float | None:
    value=row.get("stats",{}).get(field) if isinstance(row.get("stats"),dict) else None
    if isinstance(value,list): value=value[-1] if value else None
    return _number(value)

def query_items(rows: Iterable[dict], *, slots: list[str] | None = None, classifications: list[str] | None = None, min_level: float | None = None, max_level: float | None = None, stat_filters: list[str] | None = None, name: str | None = None, limit: int = 50) -> list[dict]:
    filters=[parse_stat_filter(raw) for raw in stat_filters or []]
    result=[]
    for row in rows:
        if slots and row.get("slot") not in slots: continue
        if classifications and row.get("classification") not in classifications: continue
        level=_number(row.get("level_requirement"))
        if min_level is not None and (level is None or level < min_level): continue
        if max_level is not None and (level is None or level > max_level): continue
        if name is not None:
            needle=name.casefold(); names=row.get("name",{}) if isinstance(row.get("name"),dict) else {}
            if not any(isinstance(names.get(locale),str) and needle in names[locale].casefold() for locale in ("en","ja")): continue
        accepted=True
        for field, operator, threshold in filters:
            value=_stat_value(row,field)
            if value is None or (operator is None and value == 0): accepted=False; break
            if operator == ">=" and not value >= threshold: accepted=False; break
            if operator == "<=" and not value <= threshold: accepted=False; break
            if operator == ">" and not value > threshold: accepted=False; break
            if operator == "<" and not value < threshold: accepted=False; break
            if operator == "=" and not value == threshold: accepted=False; break
        if accepted: result.append(row)
    sort_field=filters[0][0] if filters else None
    result.sort(key=lambda row: (_stat_value(row,sort_field) if sort_field else _number(row.get("level_requirement")) or float("-inf"), row.get("record", "")), reverse=True)
    return result[:limit]

def load_view(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

def render_table(rows: list[dict], stat_filters: list[str]) -> str:
    fields=[parse_stat_filter(raw)[0] for raw in stat_filters]
    headers=["record","name.en","name.ja","slot","classification","levelReq",*fields]
    lines=["\t".join(headers)]
    for row in rows:
        names=row.get("name",{}) if isinstance(row.get("name"),dict) else {}
        values=[row.get("record",""),names.get("en","") or "",names.get("ja","") or "",row.get("slot","") or "",row.get("classification","") or "",str(row.get("level_requirement","") or ""),*["" if _stat_value(row,field) is None else str(_stat_value(row,field)) for field in fields]]
        lines.append("\t".join(values))
    return "\n".join(lines)+"\n"
