from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import unittest

from grim_dawn_lab.arc import ArcFormatError, decompress_lz4_block, parse_localization_arc, read_arc


def literal_lz4(content: bytes) -> bytes:
    if len(content) < 15:
        return bytes([len(content) << 4]) + content
    remaining = len(content) - 15
    extensions = bytearray()
    while remaining >= 255:
        extensions.append(255)
        remaining -= 255
    extensions.append(remaining)
    return b"\xf0" + bytes(extensions) + content


def make_arc(name: str, content: bytes) -> bytes:
    compressed = literal_lz4(content)
    data_offset = 2048
    table_offset = data_offset + len(compressed)
    encoded_name = name.encode("utf-8")
    header = struct.pack("<4s6I", b"ARC\0", 3, 1, 1, 12, len(encoded_name) + 1, table_offset)
    padding = bytes(data_offset - len(header))
    part = struct.pack("<3I", data_offset, len(compressed), len(content))
    entry = struct.pack(
        "<11I",
        3,
        data_offset,
        len(compressed),
        len(content),
        0,
        0,
        0,
        1,
        0,
        len(encoded_name),
        0,
    )
    return header + padding + compressed + part + encoded_name + b"\0" + entry


class ArcTests(unittest.TestCase):
    def test_raw_lz4_literals_and_overlap_match(self) -> None:
        self.assertEqual(b"hello", decompress_lz4_block(b"\x50hello", 5))
        self.assertEqual(b"abcabcabc", decompress_lz4_block(b"\x32abc\x03\x00", 9))

    def test_arc_v3_and_localization(self) -> None:
        archive = make_arc("tags_creatures.txt", b"tagEnemyName=Enemy\n")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "text.arc"
            path.write_bytes(archive)
            self.assertEqual(b"tagEnemyName=Enemy\n", read_arc(path)["tags_creatures.txt"])
            self.assertEqual("Enemy", parse_localization_arc(path)["tagEnemyName"])

    def test_plain_localization_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "text.arc"
            path.write_text("tagEnemyName=Enemy\n", encoding="utf-8")
            self.assertEqual("Enemy", parse_localization_arc(path)["tagEnemyName"])

    def test_rejects_unknown_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.arc"
            path.write_bytes(struct.pack("<4s6I", b"ARC\0", 2, 0, 0, 0, 0, 28))
            with self.assertRaises(ArcFormatError):
                read_arc(path)


if __name__ == "__main__":
    unittest.main()
