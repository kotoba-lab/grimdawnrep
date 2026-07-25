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


def _fixture(data_version: int = 8) -> bytes:
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
        writer.integer(6)
        writer.integer(1)
        writer.string("records/skills/player/test.dbr")
        writer.integer(12)
        writer.encrypted_bytes(b"\x01")
        writer.integer(3)
        writer.integer(99)
        writer.integer(0)
        writer.encrypted_bytes(b"\x01\x00")
        writer.string("")
        writer.string("")
        for value in (2, 1, 1):
            writer.integer(value)
        writer.integer(0)  # item skill count
        writer.integer(0)  # v6 field

    writer.block(2, block2)
    writer.block(8, block8)
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
