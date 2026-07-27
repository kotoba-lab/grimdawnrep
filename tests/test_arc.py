from __future__ import annotations

import struct


def make_arc(name: str, payload: bytes) -> bytes:
    """Create a minimal uncompressed ARC v3 fixture containing one text file."""
    name_bytes = name.encode("utf-8")
    part_offset = 28
    part_table_offset = part_offset + len(payload)
    header = struct.pack("<7I", 0x00435241, 3, 1, 1, 12, len(name_bytes), part_table_offset)
    part = struct.pack("<3I", part_offset, len(payload), len(payload))
    toc = struct.pack("<5IQ4I", 1, part_offset, len(payload), len(payload), 0, 0, 1, 0, len(name_bytes), 0)
    return header + payload + part + name_bytes + toc
