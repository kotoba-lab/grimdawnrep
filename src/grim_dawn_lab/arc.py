"""Reader for Grim Dawn ARC v3 localization archives."""

from __future__ import annotations

import struct
from pathlib import Path


def _lz4_block(data: bytes, expected_size: int) -> bytes:
    """Decode the raw LZ4 blocks used by ARC v3 file parts."""
    output = bytearray()
    position = 0
    while position < len(data):
        token = data[position]
        position += 1
        literal_length = token >> 4
        if literal_length == 15:
            while True:
                if position >= len(data):
                    raise ValueError("truncated LZ4 literal length")
                extra = data[position]
                position += 1
                literal_length += extra
                if extra != 255:
                    break
        if position + literal_length > len(data):
            raise ValueError("truncated LZ4 literals")
        output.extend(data[position : position + literal_length])
        position += literal_length
        if position == len(data):
            break
        if position + 2 > len(data):
            raise ValueError("truncated LZ4 match offset")
        offset = data[position] | (data[position + 1] << 8)
        position += 2
        if offset == 0 or offset > len(output):
            raise ValueError("invalid LZ4 match offset")
        match_length = token & 0x0F
        if match_length == 15:
            while True:
                if position >= len(data):
                    raise ValueError("truncated LZ4 match length")
                extra = data[position]
                position += 1
                match_length += extra
                if extra != 255:
                    break
        match_length += 4
        start = len(output) - offset
        for index in range(match_length):
            output.append(output[start + index])
    if len(output) != expected_size:
        raise ValueError(f"LZ4 size mismatch: expected {expected_size}, got {len(output)}")
    return bytes(output)


def _parse_tag_text(payload: bytes) -> dict[str, str]:
    text = payload.decode("utf-8-sig")
    result: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.startswith("tag"):
            result[key] = value
    return result


def parse_localization_arc(path: Path) -> dict[str, str]:
    """Read tag=value localization files from a Grim Dawn ARC v3 archive.

    Plain UTF-8 tag files are also accepted for small synthetic fixtures.
    """
    data = path.read_bytes()
    if data[:4] != b"ARC\0":
        return _parse_tag_text(data)
    if len(data) < 28:
        raise ValueError(f"truncated ARC header: {path}")
    _, version, file_count, part_count, part_table_size, string_table_size, part_table_offset = struct.unpack_from("<7I", data)
    if version != 3 or part_table_size != part_count * 12:
        raise ValueError(f"unsupported ARC layout: {path}")
    string_table_offset = part_table_offset + part_table_size
    toc_offset = string_table_offset + string_table_size
    toc_size = file_count * 44
    if toc_offset + toc_size != len(data):
        raise ValueError(f"invalid ARC table bounds: {path}")
    strings = data[string_table_offset:toc_offset]
    parts = [struct.unpack_from("<3I", data, part_table_offset + index * 12) for index in range(part_count)]
    tags: dict[str, str] = {}
    for index in range(file_count):
        entry = struct.unpack_from("<5IQ4I", data, toc_offset + index * 44)
        entry_type, file_offset, compressed_size, decompressed_size, _, _, file_parts, first_part, name_length, name_offset = entry
        if name_offset + name_length > len(strings):
            raise ValueError(f"invalid ARC string table entry: {path}")
        name = strings[name_offset : name_offset + name_length].decode("utf-8")
        if not name.lower().endswith(".txt"):
            continue
        if first_part + file_parts > len(parts):
            raise ValueError(f"invalid ARC part range: {path}")
        chunks: list[bytes] = []
        for part_offset, compressed_length, decompressed_length in parts[first_part : first_part + file_parts]:
            if part_offset + compressed_length > len(data):
                raise ValueError(f"invalid ARC part bounds: {path}")
            chunk = data[part_offset : part_offset + compressed_length]
            chunks.append(chunk if compressed_length == decompressed_length else _lz4_block(chunk, decompressed_length))
        payload = b"".join(chunks)
        if len(payload) != decompressed_size:
            raise ValueError(f"invalid ARC file entry: {path}:{name}")
        tags.update(_parse_tag_text(payload))
    return tags
