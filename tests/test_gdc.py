from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from grim_dawn_lab.gdc import (
    GdcError,
    UnsupportedGdcVersion,
    _encryption_table,
    discover_player_gdc,
    import_player_gdc,
    parse_player_gdc_bytes,
)


class _Writer:
    def __init__(self, seed: int = 0x12345678):
        self.seed = seed
        self.state = seed
        self.table = _encryption_table(seed)
        self.data = bytearray(struct.pack("<I", seed ^ 0x55555555))

    def raw_u32(self, value: int) -> None:
        self.data.extend(struct.pack("<I", value & 0xFFFFFFFF))

    def encrypted_bytes(self, plain: bytes) -> None:
        for value in plain:
            encrypted = value ^ (self.state & 0xFF)
            self.data.append(encrypted)
            self.state = (self.state ^ self.table[encrypted]) & 0xFFFFFFFF

    def integer(self, value: int) -> None:
        encrypted = (value & 0xFFFFFFFF) ^ self.state
        raw = struct.pack("<I", encrypted)
        self.data.extend(raw)
        for byte in raw:
            self.state = (self.state ^ self.table[byte]) & 0xFFFFFFFF

    def floating(self, value: float) -> None:
        self.integer(struct.unpack("<I", struct.pack("<f", value))[0])

    def string(self, value: str, encoding: str = "ascii", *, fixed: bool = False) -> None:
        raw = value.encode(encoding)
        if not fixed:
            self.integer(len(value) if encoding == "utf-16-le" else len(raw))
        self.encrypted_bytes(raw)

    def block(self, block_id: int, content) -> None:
        self.integer(block_id)
        length_position = len(self.data)
        length_state = self.state
        self.raw_u32(0)
        start = len(self.data)
        content()
        length = len(self.data) - start
        self.data[length_position : length_position + 4] = struct.pack("<I", length ^ length_state)
        self.raw_u32(self.state)


def _fixture(data_version: int = 8, block8_version: int = 6, sub_skill_count: int = 0) -> bytes:
    writer = _Writer()
    writer.integer(int.from_bytes(b"GDCX", "little"))
    writer.integer(2)
    writer.string("Fixture", "utf-16-le")
    writer.encrypted_bytes(b"\x01")
    writer.string("tagClass01")
    writer.integer(42)
    writer.encrypted_bytes(b"\x00\x02")
    writer.raw_u32(writer.state)
    writer.integer(data_version)
    writer.encrypted_bytes(bytes(16))

    def block2() -> None:
        for value in (8, 42, 12345, 4, 5, 6, 7):
            writer.integer(value)
        for value in (100.5, 200.5, 300.5, 4000.0, 500.0):
            writer.floating(value)

    def block8() -> None:
        writer.integer(block8_version)
        writer.integer(1)
        writer.string("records/skills/player/test.dbr")
        writer.integer(12)
        writer.encrypted_bytes(b"\x01")
        writer.integer(3)
        writer.integer(99)
        writer.integer(0)
        if block8_version >= 8:
            writer.encrypted_bytes(b"\xfe")
        writer.encrypted_bytes(b"\x01\x00")
        writer.string("")
        writer.string("")
        for value in (2, 1, 1):
            writer.integer(value)
        writer.integer(0)  # item skill count
        if block8_version in (6, 7):
            writer.integer(sub_skill_count)
        elif block8_version == 8:
            writer.integer(sub_skill_count)
        if block8_version >= 6 and 0 < sub_skill_count <= 2:
            for index in range(sub_skill_count):
                for value in (
                    f"records/sub/{index}.dbr",
                    f"records/autocast/{index}.dbr",
                    f"records/controller/{index}.dbr",
                    f"records/parent/{index}.dbr",
                ):
                    writer.string(value)

    writer.block(2, block2)
    writer.block(8, block8)
    return bytes(writer.data)


def _write_item(writer: _Writer, version: int, base: bytes = b"") -> None:
    for value in (base, b"", b"", b"", b""):
        writer.integer(len(value))
        writer.encrypted_bytes(value)
    for value in (0,):
        writer.integer(value)
    for value in (b"", b""):
        writer.integer(len(value))
        writer.encrypted_bytes(value)
    for value in (0,):
        writer.integer(value)
    writer.integer(0)  # augment string length
    if version >= 8:
        writer.integer(len(b"records/ascendant/fixture.dbr"))
        writer.encrypted_bytes(b"records/ascendant/fixture.dbr")
        writer.integer(17)
    for value in (11, 12, 13, 14):
        writer.integer(value)
    if version >= 8:
        writer.integer(18)
    if version >= 11:
        writer.integer(19)


def _fixture_with_block3(version: int = 4, sack_count: int = 1) -> bytes:
    writer = _Writer()
    writer.integer(int.from_bytes(b"GDCX", "little"))
    writer.integer(2)
    writer.string("Fixture", "utf-16-le")
    writer.encrypted_bytes(b"\x01")
    writer.string("tagClass01")
    writer.integer(42)
    writer.encrypted_bytes(b"\x00\x02")
    writer.raw_u32(writer.state)
    writer.integer(8)
    writer.encrypted_bytes(bytes(16))

    def block3() -> None:
        writer.integer(version)
        if version not in range(4, 12):
            return
        writer.encrypted_bytes(b"\x01")
        writer.integer(sack_count)
        if sack_count > 100:
            return
        writer.integer(0)
        writer.integer(0)

        def sack() -> None:
            writer.encrypted_bytes(b"\xfe")
            writer.integer(1)
            _write_item(writer, version, b"records/items/\xfffixture.dbr")
            writer.integer(4)
            writer.integer(5)

        writer.block(0, sack)
        writer.encrypted_bytes(b"\x02")
        for _ in range(12):
            _write_item(writer, version)
            writer.encrypted_bytes(b"\x80")
        writer.encrypted_bytes(b"\x03")
        for _ in range(2):
            _write_item(writer, version)
            writer.encrypted_bytes(b"\x80")
        writer.encrypted_bytes(b"\xff")
        for _ in range(2):
            _write_item(writer, version)
            writer.encrypted_bytes(b"\x80")

    writer.block(3, block3)
    return bytes(writer.data)


def _fixture_with_block4(version: int, stash_count: int = 1) -> bytes:
    writer = _Writer()
    writer.integer(int.from_bytes(b"GDCX", "little"))
    writer.integer(2)
    writer.string("Fixture", "utf-16-le")
    writer.encrypted_bytes(b"\x01")
    writer.string("tagClass01")
    writer.integer(42)
    writer.encrypted_bytes(b"\x00\x02")
    writer.raw_u32(writer.state)
    writer.integer(8)
    writer.encrypted_bytes(bytes(16))

    def block4() -> None:
        writer.integer(version)
        writer.integer(stash_count)
        if stash_count > 100:
            return

        def stash() -> None:
            writer.integer(10)
            writer.integer(20)
            writer.integer(0)
            if version >= 9:
                for value in (1, 2, 3, 4):
                    writer.integer(value)
                writer.string("Synthetic tab", "utf-16-le")

        writer.block(0, stash)

    writer.block(4, block4)
    return bytes(writer.data)


class GdcTests(unittest.TestCase):
    def test_parses_supported_synthetic_save(self) -> None:
        result = parse_player_gdc_bytes(_fixture())
        self.assertEqual(result["header"]["character_name"], "Fixture")
        self.assertEqual(result["header"]["level"], 42)
        self.assertEqual(result["attributes"]["health"], 4000.0)
        self.assertEqual(result["skills"]["skills"][0]["level"], 12)
        self.assertEqual(result["provenance"]["source_hash"], hashlib.sha256(_fixture()).hexdigest())

    def test_rejects_unsupported_version(self) -> None:
        with self.assertRaisesRegex(UnsupportedGdcVersion, "unsupported_data_version:9"):
            parse_player_gdc_bytes(_fixture(9))

    def test_parses_v8_block8_skill_extension_without_weakening_booleans(self) -> None:
        parsed = parse_player_gdc_bytes(_fixture(block8_version=8))
        skill = parsed["skills"]["skills"][0]
        self.assertEqual(skill["unknown_v8"], 254)
        self.assertTrue(skill["active"])
        self.assertFalse(skill["transition"])
        self.assertEqual(parsed["skills"]["sub_skills"], [])

    def test_parses_v6_block8_empty_sub_skills(self) -> None:
        self.assertEqual(parse_player_gdc_bytes(_fixture(block8_version=6))["skills"]["sub_skills"], [])

    def test_parses_v8_block8_sub_skills_and_preserves_boundary(self) -> None:
        result = parse_player_gdc_bytes(_fixture(block8_version=8, sub_skill_count=1))
        self.assertEqual(
            result["skills"]["sub_skills"],
            [{
                "name": "records/sub/0.dbr",
                "autocast_skill": "records/autocast/0.dbr",
                "autocast_controller": "records/controller/0.dbr",
                "parent_skill": "records/parent/0.dbr",
            }],
        )

    def test_parses_v8_block8_multiple_sub_skills(self) -> None:
        sub_skills = parse_player_gdc_bytes(_fixture(block8_version=8, sub_skill_count=2))["skills"]["sub_skills"]
        self.assertEqual(len(sub_skills), 2)
        self.assertEqual(sub_skills[1]["parent_skill"], "records/parent/1.dbr")

    def test_rejects_invalid_block8_sub_skill_count(self) -> None:
        for count in (-1, 1_000_001):
            with self.subTest(count=count), self.assertRaisesRegex(GdcError, "^invalid_sub_skill_count$"):
                parse_player_gdc_bytes(_fixture(block8_version=8, sub_skill_count=count))

    def test_rejects_corrupt_v8_block8_tail_checksum(self) -> None:
        data = bytearray(_fixture(block8_version=8, sub_skill_count=1))
        data[-1] ^= 1
        with self.assertRaisesRegex(GdcError, "checksum_mismatch:block_8"):
            parse_player_gdc_bytes(bytes(data))

    def test_parses_v5_block8_old_skill_layout(self) -> None:
        skill = parse_player_gdc_bytes(_fixture(block8_version=5))["skills"]["skills"][0]
        self.assertNotIn("unknown_v8", skill)
        self.assertTrue(skill["active"])
        self.assertFalse(skill["transition"])

    def test_rejects_unknown_block8_version_before_guessing_layout(self) -> None:
        with self.assertRaisesRegex(UnsupportedGdcVersion, "^unsupported_block_version:8:9$"):
            parse_player_gdc_bytes(_fixture(block8_version=9))

    def test_parses_v4_block3_raw_selectors_and_lossless_byte_strings(self) -> None:
        result = parse_player_gdc_bytes(_fixture_with_block3(4))
        inventory = result["inventory"]
        self.assertEqual(inventory["version"], 4)
        self.assertEqual(inventory["use_alternate_weapon_set"], 2)
        self.assertEqual(inventory["alternate_set_1_enabled"], 3)
        self.assertEqual(inventory["alternate_set_2_enabled"], 255)
        self.assertEqual(inventory["inventory_sacks"][0]["unused"], 254)
        self.assertEqual(inventory["equipment"][0]["attached"], 128)
        self.assertEqual(
            inventory["inventory_sacks"][0]["items"][0]["base"],
            "records/items/\\xfffixture.dbr",
        )

    def test_parses_v8_block3_extended_item_fields(self) -> None:
        item = parse_player_gdc_bytes(_fixture_with_block3(8))["inventory"]["equipment"][0]
        self.assertEqual(item["ascendant_record"], "records/ascendant/fixture.dbr")
        self.assertEqual(item["ascendant_rerolls"], 17)
        self.assertEqual(item["seed_rerolls"], 18)
        self.assertNotIn("affix_rerolls", item)

    def test_parses_v11_block3_affix_rerolls(self) -> None:
        item = parse_player_gdc_bytes(_fixture_with_block3(11))["inventory"]["equipment"][0]
        self.assertEqual(item["seed_rerolls"], 18)
        self.assertEqual(item["affix_rerolls"], 19)

    def test_rejects_unknown_block3_version_before_guessing_layout(self) -> None:
        with self.assertRaisesRegex(UnsupportedGdcVersion, "^unsupported_block_version:3:12$"):
            parse_player_gdc_bytes(_fixture_with_block3(12))

    def test_rejects_corrupt_block3_checksum(self) -> None:
        data = bytearray(_fixture_with_block3(11))
        data[-1] ^= 1
        with self.assertRaisesRegex(GdcError, "checksum_mismatch:block_3"):
            parse_player_gdc_bytes(bytes(data))

    def test_rejects_block3_sack_count_out_of_bounds(self) -> None:
        with self.assertRaisesRegex(GdcError, "^invalid_sack_count$"):
            parse_player_gdc_bytes(_fixture_with_block3(4, sack_count=101))

    def test_parses_block4_v8_stash_without_v9_tail(self) -> None:
        stash = parse_player_gdc_bytes(_fixture_with_block4(8))["format"]["block_versions"]
        self.assertEqual(stash["4"], 8)

    def test_parses_block4_v9_and_v11_stash_tail(self) -> None:
        for version in (9, 11):
            with self.subTest(version=version):
                result = parse_player_gdc_bytes(_fixture_with_block4(version))
                self.assertEqual(result["format"]["block_versions"]["4"], version)

    def test_rejects_unknown_block4_version_before_guessing_layout(self) -> None:
        for version in (5, 12):
            with self.subTest(version=version), self.assertRaisesRegex(
                UnsupportedGdcVersion,
                rf"^unsupported_block_version:4:{version}$",
            ):
                parse_player_gdc_bytes(_fixture_with_block4(version))

    def test_rejects_corrupt_block4_checksum(self) -> None:
        data = bytearray(_fixture_with_block4(9))
        data[-1] ^= 1
        with self.assertRaisesRegex(GdcError, "checksum_mismatch:block_4"):
            parse_player_gdc_bytes(bytes(data))

    def test_rejects_block4_stash_count_out_of_bounds(self) -> None:
        with self.assertRaisesRegex(GdcError, "^invalid_stash_count$"):
            parse_player_gdc_bytes(_fixture_with_block4(9, stash_count=101))

    def test_reports_unrecognized_save_structure_without_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary) / "player.gdc"
            save.write_bytes(_fixture())
            with patch("grim_dawn_lab.gdc.parse_player_gdc_bytes", side_effect=UnicodeDecodeError("ascii", b"\\xb7", 0, 1, "invalid")):
                with self.assertRaisesRegex(UnsupportedGdcVersion, "possible 1.3.0\\+ save format change"):
                    import_player_gdc(save)

    def test_rejects_corruption_at_checksum(self) -> None:
        data = bytearray(_fixture())
        data[-1] ^= 1
        with self.assertRaisesRegex(GdcError, "checksum_mismatch:block_8"):
            parse_player_gdc_bytes(bytes(data))

    def test_import_is_read_only_and_discovery_finds_local_save(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save = root / "Documents/My Games/Grim Dawn/save/main/_fixture/player.gdc"
            save.parent.mkdir(parents=True)
            save.write_bytes(_fixture())
            before = (save.stat().st_mtime_ns, save.read_bytes())
            with patch.dict("os.environ", {"USERPROFILE": str(root), "PROGRAMFILES(X86)": str(root / "none")}):
                self.assertEqual(discover_player_gdc(), [save.resolve()])
            result = import_player_gdc(save)
            self.assertTrue(result["provenance"]["read_only_verified"])
            self.assertEqual(before, (save.stat().st_mtime_ns, save.read_bytes()))


if __name__ == "__main__":
    unittest.main()
