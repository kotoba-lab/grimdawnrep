from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import struct
from typing import Any, Callable


class GdcError(ValueError):
    """A save could not be read safely."""


class UnsupportedGdcVersion(GdcError):
    pass


def _signed(value: int) -> int:
    return value - 0x100000000 if value & 0x80000000 else value


def _encryption_table(seed: int) -> tuple[int, ...]:
    value = seed & 0xFFFFFFFF
    table = []
    for _ in range(256):
        value = ((((value << 31) & 0xFFFFFFFF) | (value >> 1)) * 39916801) & 0xFFFFFFFF
        table.append(value)
    return tuple(table)


@dataclass
class _Reader:
    data: bytes
    position: int
    state: int
    table: tuple[int, ...]

    def _raw(self, length: int) -> bytes:
        if length < 0 or self.position + length > len(self.data):
            raise GdcError("truncated_save")
        result = self.data[self.position : self.position + length]
        self.position += length
        return result

    def raw_u32(self) -> int:
        return struct.unpack("<I", self._raw(4))[0]

    def encrypted_bytes(self, length: int) -> bytes:
        encrypted = self._raw(length)
        plain = bytearray()
        for value in encrypted:
            plain.append(value ^ (self.state & 0xFF))
            self.state = (self.state ^ self.table[value]) & 0xFFFFFFFF
        return bytes(plain)

    def byte(self) -> int:
        return self.encrypted_bytes(1)[0]

    def boolean(self) -> bool:
        value = self.byte()
        if value not in (0, 1):
            raise GdcError("invalid_boolean")
        return bool(value)

    def integer(self) -> int:
        encrypted = self._raw(4)
        encrypted_value = struct.unpack("<I", encrypted)[0]
        result = _signed(encrypted_value ^ self.state)
        for value in encrypted:
            self.state = (self.state ^ self.table[value]) & 0xFFFFFFFF
        return result

    def block_length(self) -> int:
        length = _signed(self.raw_u32() ^ self.state)
        if length < 0 or self.position + length > len(self.data):
            raise GdcError("invalid_block_length")
        return length

    def floating(self) -> float:
        bits = self.integer() & 0xFFFFFFFF
        return struct.unpack("<f", struct.pack("<I", bits))[0]

    def string(self, encoding: str = "ascii", fixed_length: int | None = None) -> str:
        length = fixed_length if fixed_length is not None else self.integer()
        if length < 0 or length > 1_000_000:
            raise GdcError("invalid_string_length")
        byte_length = length * 2 if encoding == "utf-16-le" else length
        raw = self.encrypted_bytes(byte_length)
        if encoding != "ascii":
            return raw.decode(encoding, errors="strict")
        # GDC's one-byte strings are normally ASCII record paths.  Preserve that
        # public representation, but render any other byte losslessly and without
        # changing the encrypted-stream alignment.  Backslashes and controls are
        # escaped too, so this representation is unambiguous and reversible.
        return "".join(
            chr(value) if 0x20 <= value <= 0x7E and value != 0x5C else f"\\x{value:02x}"
            for value in raw
        )

    def array(self, item_reader: Callable[[], Any]) -> list[Any]:
        count = self.integer()
        if count < 0 or count > 1_000_000:
            raise GdcError("invalid_array_length")
        return [item_reader() for _ in range(count)]


def _read_item(reader: _Reader, version: int) -> dict[str, Any]:
    item = {
        "base": reader.string(),
        "prefix": reader.string(),
        "suffix": reader.string(),
        "modifier": reader.string(),
        "transmute": reader.string(),
        "seed": reader.integer(),
        "component": reader.string(),
        "component_bonus": reader.string(),
        "component_seed": reader.integer(),
        "augment": reader.string(),
    }
    if version >= 8:
        item["ascendant_record"] = reader.string()
        item["ascendant_rerolls"] = reader.integer()
    item.update({
        "unknown": reader.integer(),
        "augment_seed": reader.integer(),
        "component_completion_level": reader.integer(),
        "stack_count": reader.integer(),
    })
    if version >= 8:
        item["seed_rerolls"] = reader.integer()
    if version >= 11:
        item["affix_rerolls"] = reader.integer()
    return item


def _read_equipment_item(reader: _Reader, version: int) -> dict[str, Any]:
    item = _read_item(reader, version)
    item["attached"] = reader.byte()
    return item


def _read_inventory_item(reader: _Reader, version: int) -> dict[str, Any]:
    item = _read_item(reader, version)
    item["x"] = reader.integer()
    item["y"] = reader.integer()
    return item


def _read_nested_sack(reader: _Reader, version: int) -> dict[str, Any]:
    block_id = reader.integer()
    length = reader.block_length()
    end = reader.position + length
    if block_id != 0:
        raise GdcError("unexpected_inventory_block")
    result = {"unused": reader.byte(), "items": reader.array(lambda: _read_inventory_item(reader, version))}
    if reader.position != end:
        raise GdcError("inventory_block_length_mismatch")
    _verify_checksum(reader)
    return result


def _read_nested_stash(reader: _Reader, version: int) -> None:
    block_id = reader.integer()
    length = reader.block_length()
    end = reader.position + length
    if block_id != 0:
        raise GdcError("unexpected_stash_block")
    reader.integer()  # width
    reader.integer()  # height
    reader.array(lambda: _read_inventory_item(reader, version))
    if version >= 9:
        reader.integer()  # border index
        reader.integer()  # border color index
        reader.integer()  # symbol index
        reader.integer()  # symbol color index
        reader.string("utf-16-le")  # button name
    if reader.position != end:
        raise GdcError("stash_block_length_mismatch")
    _verify_checksum(reader, "stash_block")


def _read_block4(reader: _Reader) -> dict[str, Any]:
    version = reader.integer()
    if version not in range(6, 12):
        raise UnsupportedGdcVersion(f"unsupported_block_version:4:{version}")
    count = reader.integer()
    if count < 0 or count > 100:
        raise GdcError("invalid_stash_count")
    for _ in range(count):
        # Stash item layout is keyed to Block 4's own version, not Block 3.
        _read_nested_stash(reader, version)
    return {"version": version, "stash_count": count}


def _read_block2(reader: _Reader) -> dict[str, Any]:
    names = (
        "version", "level_in_bio", "experience", "attribute_points", "skill_points",
        "devotion_points", "total_devotion_points_unlocked",
    )
    result = {name: reader.integer() for name in names}
    for name in ("physique", "cunning", "spirit", "health", "energy"):
        result[name] = reader.floating()
    return result


def _read_block3(reader: _Reader) -> dict[str, Any]:
    version = reader.integer()
    if version not in range(4, 12):
        raise UnsupportedGdcVersion(f"unsupported_block_version:3:{version}")
    result: dict[str, Any] = {"version": version, "has_data": reader.boolean()}
    if not result["has_data"]:
        return result
    sack_count = reader.integer()
    if sack_count < 0 or sack_count > 100:
        raise GdcError("invalid_sack_count")
    result.update({
        "focused_sack": reader.integer(),
        "selected_sack": reader.integer(),
        "inventory_sacks": [_read_nested_sack(reader, version) for _ in range(sack_count)],
        # These are selectors/indexes, not validated boolean flags.  Keeping the
        # original field names avoids a public-model shape change while retaining
        # every valid byte value from the v4 layout.
        "use_alternate_weapon_set": reader.byte(),
        "equipment": [_read_equipment_item(reader, version) for _ in range(12)],
        "alternate_set_1_enabled": reader.byte(),
        "alternate_set_1": [_read_equipment_item(reader, version) for _ in range(2)],
        "alternate_set_2_enabled": reader.byte(),
        "alternate_set_2": [_read_equipment_item(reader, version) for _ in range(2)],
    })
    return result


def _read_character_skill(reader: _Reader, version: int) -> dict[str, Any]:
    skill = {
        "record": reader.string(), "level": reader.integer(), "enabled": reader.boolean(),
        "devotion_level": reader.integer(), "devotion_experience": reader.integer(),
        "sublevel": reader.integer(),
    }
    if version >= 8:
        skill["unknown_v8"] = reader.byte()
    skill.update({
        "active": reader.boolean(), "transition": reader.boolean(),
        "autocast_skill": reader.string(), "autocast_controller": reader.string(),
    })
    return skill


def _read_item_skill(reader: _Reader) -> dict[str, Any]:
    return {
        "record": reader.string(), "autocast_skill": reader.string(),
        "autocast_controller": reader.string(), "unknown_bytes": reader.string(fixed_length=4),
        "unknown": reader.string(),
    }


def _read_character_sub_skill(reader: _Reader) -> dict[str, Any]:
    return {
        "name": reader.string(),
        "autocast_skill": reader.string(),
        "autocast_controller": reader.string(),
        "parent_skill": reader.string(),
    }


def _read_block8(reader: _Reader) -> dict[str, Any]:
    version = reader.integer()
    if version not in range(5, 9):
        raise UnsupportedGdcVersion(f"unsupported_block_version:8:{version}")
    result = {
        "version": version,
        "skills": reader.array(lambda: _read_character_skill(reader, version)),
        "masteries_allowed": reader.integer(),
        "skill_points_reclaimed": reader.integer(),
        "devotion_points_reclaimed": reader.integer(),
        "item_skills": reader.array(lambda: _read_item_skill(reader)),
    }
    if version >= 6:
        sub_skill_count = reader.integer()
        if sub_skill_count < 0 or sub_skill_count > 1_000_000:
            raise GdcError("invalid_sub_skill_count")
        result["sub_skills"] = [_read_character_sub_skill(reader) for _ in range(sub_skill_count)]
    return result


def _verify_checksum(reader: _Reader, context: str = "save") -> None:
    actual = reader.raw_u32()
    if actual != reader.state:
        raise GdcError(f"checksum_mismatch:{context}:expected={reader.state:08x}:actual={actual:08x}")


def parse_player_gdc_bytes(data: bytes, *, source_hash: str | None = None) -> dict[str, Any]:
    if len(data) < 16:
        raise GdcError("truncated_save")
    stored_seed = struct.unpack("<I", data[:4])[0]
    seed = stored_seed ^ 0x55555555
    reader = _Reader(data, 4, seed, _encryption_table(seed))
    magic = reader.integer() & 0xFFFFFFFF
    file_version = reader.integer()
    if magic != int.from_bytes(b"GDCX", "little"):
        raise GdcError("invalid_magic")
    header = {
        "character_name": reader.string("utf-16-le"),
        "male": reader.boolean(),
        "class_record": reader.string(),
        "level": reader.integer(),
        "hardcore": reader.boolean(),
        "expansion_character": reader.byte(),
    }
    _verify_checksum(reader)
    data_version = reader.integer()
    if data_version not in (6, 7, 8):
        raise UnsupportedGdcVersion(f"unsupported_data_version:{data_version}")
    mystery = reader.encrypted_bytes(16)
    blocks: dict[int, Any] = {}
    block_versions: dict[str, int] = {}
    parsers = {2: _read_block2, 3: _read_block3, 4: _read_block4, 8: _read_block8}
    while reader.position < len(data):
        block_id = reader.integer()
        length = reader.block_length()
        end = reader.position + length
        parser = parsers.get(block_id)
        if parser:
            value = parser(reader)
            if reader.position != end:
                raise GdcError(f"block_length_mismatch:{block_id}")
            blocks[block_id] = value
            if "version" in value:
                block_versions[str(block_id)] = value["version"]
        else:
            reader.encrypted_bytes(length)
        _verify_checksum(reader, f"block_{block_id}")
    return {
        "schema_version": "0.1.0",
        "format": {"file_version": file_version, "data_version": data_version, "block_versions": block_versions},
        "header": header,
        "attributes": blocks.get(2),
        "inventory": blocks.get(3),
        "skills": blocks.get(8),
        "provenance": {"source": "player_gdc", "source_hash": source_hash or hashlib.sha256(data).hexdigest()},
        "diagnostics": {"parsed_blocks": sorted(blocks), "mystery_field_length": len(mystery)},
    }


def import_player_gdc(path: Path) -> dict[str, Any]:
    before = path.stat()
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    try:
        result = parse_player_gdc_bytes(data, source_hash=digest)
    except (UnicodeDecodeError, struct.error) as error:
        raise UnsupportedGdcVersion(
            "unsupported_or_unrecognized_save_structure: possible 1.3.0+ save format change; "
            "no partial build was emitted"
        ) from error
    after = path.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise GdcError("input_changed_during_read")
    result["provenance"].update({
        "size": before.st_size,
        "modified_at": datetime.fromtimestamp(before.st_mtime, timezone.utc).isoformat(),
        "read_only_verified": True,
    })
    return result


def discover_player_gdc() -> list[Path]:
    candidates: list[Path] = []
    user_profile = Path(os.environ.get("USERPROFILE", Path.home()))
    local_roots = [
        user_profile / "Documents" / "My Games" / "Grim Dawn" / "save" / "main",
        user_profile / "OneDrive" / "Documents" / "My Games" / "Grim Dawn" / "save" / "main",
    ]
    for root in local_roots:
        if root.is_dir():
            candidates.extend(root.glob("*/player.gdc"))
    steam = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Steam" / "userdata"
    if steam.is_dir():
        candidates.extend(steam.glob("*/219990/remote/save/main/*/player.gdc"))
    return sorted(set(path.resolve() for path in candidates), key=lambda path: str(path).casefold())
