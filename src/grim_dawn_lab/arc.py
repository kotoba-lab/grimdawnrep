"""Read-only reader for Grim Dawn ARC v3 archives."""

from __future__ import annotations

import struct
from pathlib import Path


class ArcFormatError(ValueError):
    pass


def decompress_lz4_block(data: bytes, expected_size: int) -> bytes:
    """Decode the raw LZ4 block variant used by ARC v3 parts."""
    output = bytearray()
    cursor = 0
    while cursor < len(data):
        token = data[cursor]
        cursor += 1
        literal_length = token >> 4
        if literal_length == 15:
            while True:
                if cursor >= len(data):
                    raise ArcFormatError("truncated LZ4 literal length")
                extension = data[cursor]
                cursor += 1
                literal_length += extension
                if extension != 255:
                    break
        if cursor + literal_length > len(data):
            raise ArcFormatError("truncated LZ4 literals")
        output.extend(data[cursor : cursor + literal_length])
        cursor += literal_length
        if cursor == len(data):
            break
        if cursor + 2 > len(data):
            raise ArcFormatError("truncated LZ4 match offset")
        offset = int.from_bytes(data[cursor : cursor + 2], "little")
        cursor += 2
        if offset == 0 or offset > len(output):
            raise ArcFormatError("invalid LZ4 match offset")
        match_length = token & 0x0F
        if match_length == 15:
            while True:
                if cursor >= len(data):
                    raise ArcFormatError("truncated LZ4 match length")
                extension = data[cursor]
                cursor += 1
                match_length += extension
                if extension != 255:
                    break
        match_length += 4
        start = len(output) - offset
        for index in range(match_length):
            output.append(output[start + index])
    if len(output) != expected_size:
        raise ArcFormatError(f"LZ4 size mismatch: expected {expected_size}, got {len(output)}")
    return bytes(output)


def read_arc(path: Path) -> dict[str, bytes]:
    data = path.read_bytes()
    if len(data) < 28:
        raise ArcFormatError("ARC header is truncated")
    magic, version, file_count, part_count, parts_size, names_size, table_offset = struct.unpack_from(
        "<4s6I", data, 0
    )
    if magic != b"ARC\0" or version != 3:
        raise ArcFormatError(f"unsupported ARC magic/version: {magic!r}/{version}")
    if parts_size != part_count * 12:
        raise ArcFormatError("ARC part table size is inconsistent")
    parts_offset = table_offset
    names_offset = parts_offset + parts_size
    files_offset = names_offset + names_size
    expected_end = files_offset + file_count * 44
    if expected_end > len(data):
        raise ArcFormatError("ARC tables extend beyond end of file")
    parts = [struct.unpack_from("<3I", data, parts_offset + index * 12) for index in range(part_count)]
    names = data[names_offset:files_offset]
    result: dict[str, bytes] = {}
    for index in range(file_count):
        entry = struct.unpack_from("<11I", data, files_offset + index * 44)
        storage_type, _, _, expected_size, _, _, _, file_parts, first_part, name_length, name_start = entry
        if first_part + file_parts > len(parts) or name_start + name_length > len(names):
            raise ArcFormatError("ARC file entry references an invalid table range")
        name = names[name_start : name_start + name_length].decode("utf-8")
        chunks: list[bytes] = []
        for part_offset, compressed_size, uncompressed_size in parts[first_part : first_part + file_parts]:
            compressed = data[part_offset : part_offset + compressed_size]
            if len(compressed) != compressed_size:
                raise ArcFormatError("ARC part is truncated")
            if compressed_size == uncompressed_size:
                chunk = compressed
            elif storage_type == 3:
                chunk = decompress_lz4_block(compressed, uncompressed_size)
            else:
                raise ArcFormatError(f"unsupported ARC storage type: {storage_type}")
            chunks.append(chunk)
        content = b"".join(chunks)
        if len(content) != expected_size:
            raise ArcFormatError(f"ARC file size mismatch for {name}")
        result[name] = content
    return result


def _parse_tag_text(payload: bytes) -> dict[str, str]:
    tags: dict[str, str] = {}
    for line in payload.decode("utf-8-sig").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        tag, value = line.split("=", 1)
        tags[tag.strip()] = value.strip()
    return tags


def parse_localization_arc(path: Path) -> dict[str, str]:
    """Read tag=value localization files from ARC or plain synthetic fixtures."""
    data = path.read_bytes()
    if data[:4] != b"ARC\0":
        return _parse_tag_text(data)
    tags: dict[str, str] = {}
    for name, content in read_arc(path).items():
        if name.lower().endswith(".txt"):
            tags.update(_parse_tag_text(content))
    return tags
