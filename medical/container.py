"""Container layout of the PYRM (PYRA Medical) format.

The header is 144 bytes, everything is little-endian:

===========  ======  =========================================================
offset       size   field
===========  ======  =========================================================
0            8      magic ``PYRM\\r\\n\\x1a\\n``
8            4      version (1)
12           4      flags
16           4      width
20           4      height
24           4      depth (number of slices; 1 for a 2D image)
28           4      channels (samples per pixel)
32           4      sample format
36           4      bits per sample
40           4      tile size
44           4      number of tiles
48           4      tile index offset
52           4      tile index size
56           4      metadata offset
60           4      metadata size
64           4      metadata CRC-32
68           4      payload CRC-32 (all tile bodies)
72           4      header CRC-32 (bytes 0..72)
76           4      fixed predictor id (used when a tile has no row table)
80           32     SHA-256 of the raw sample bytes
112          32      reserved
===========  ======  =========================================================

The metadata block is UTF-8 JSON. The tile index holds one 16 byte record
per tile: offset, length, method, flags, filter table size and the CRC-32 of
the tile body.
"""

import json
import struct
import zlib
from .constants import (MAGIC, VERSION, VERSION_V1, VERSION_V2,
                        CorruptedFileError,
                        TILE_FLAG_ADAPTIVE, TILE_FLAG_SPLIT)

HEADER_SIZE = 144
HEADER_CRC_OFFSET = 72
INDEX_ENTRY_SIZE = 16

_HDR = struct.Struct("<8s" + "I" * 18 + "32s32s")
assert _HDR.size == HEADER_SIZE, _HDR.size

_IDX = struct.Struct("<IIBBHI")


class Header:
    """Parsed container header."""

    __slots__ = ("version", "flags", "width", "height", "depth", "channels",
                 "sample_format", "bits", "tile_size", "num_tiles",
                 "index_offset", "index_size", "meta_offset", "meta_size",
                 "meta_crc", "payload_crc", "header_crc", "predictor",
                 "sha256", "signature")

    def __init__(self, **kw):
        for name in self.__slots__:
            setattr(self, name, kw.get(name))

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__slots__}


def pack_header(hdr: Header, sha256: bytes) -> bytes:
    """Serialise the fixed header (CRC fields included).

    `hdr.signature` holds the optional HMAC-SHA256 signature (32 bytes);
    it is written into the reserved area at offset 112 and is *not* part
    of the header CRC (which covers bytes 0..72).
    """
    if len(sha256) != 32:
        raise ValueError("sha256 must be 32 bytes")
    signature = hdr.signature if hdr.signature else b"\x00" * 32
    if len(signature) != 32:
        raise ValueError("signature must be 32 bytes")
    body = _HDR.pack(
        MAGIC, hdr.version, hdr.flags, hdr.width, hdr.height, hdr.depth,
        hdr.channels, hdr.sample_format, hdr.bits, hdr.tile_size,
        hdr.num_tiles, hdr.index_offset, hdr.index_size, hdr.meta_offset,
        hdr.meta_size, hdr.meta_crc, hdr.payload_crc, 0, hdr.predictor, sha256,
        signature)
    crc = zlib.crc32(body[:HEADER_CRC_OFFSET]) & 0xFFFFFFFF
    return body[:HEADER_CRC_OFFSET] + struct.pack("<I", crc) + body[76:]


def unpack_header(data: bytes) -> Header:
    """Parse and validate the fixed header."""
    if len(data) < HEADER_SIZE:
        raise CorruptedFileError("truncated PYRM header")
    (magic, version, flags, width, height, depth, channels, sample_format,
     bits, tile_size, num_tiles, index_offset, index_size, meta_offset,
     meta_size, meta_crc, payload_crc, header_crc, predictor, sha256,
     signature) = _HDR.unpack(data[:HEADER_SIZE])
    if magic != MAGIC:
        raise CorruptedFileError("not a PYRM container")
    if version not in (VERSION_V1, VERSION_V2, VERSION):
        raise CorruptedFileError(f"unsupported PYRM version {version}")
    if width == 0 or height == 0 or depth == 0 or channels == 0:
        raise CorruptedFileError("degenerate image geometry")
    if tile_size == 0:
        raise CorruptedFileError("invalid tile size")
    expected_crc = zlib.crc32(data[:HEADER_CRC_OFFSET]) & 0xFFFFFFFF
    if expected_crc != header_crc:
        raise CorruptedFileError("header CRC mismatch")
    if index_size != num_tiles * INDEX_ENTRY_SIZE:
        raise CorruptedFileError("tile index size mismatch")
    return Header(version=version, flags=flags, width=width, height=height,
                  depth=depth, channels=channels, sample_format=sample_format,
                  bits=bits, tile_size=tile_size, num_tiles=num_tiles,
                  index_offset=index_offset, index_size=index_size,
                  meta_offset=meta_offset, meta_size=meta_size,
                  meta_crc=meta_crc, payload_crc=payload_crc,
                  header_crc=header_crc, predictor=predictor, sha256=sha256,
                  signature=signature)


def pack_metadata(meta: dict | None) -> tuple[bytes, int]:
    """Serialise the metadata block; returns (bytes, crc32)."""
    if not meta:
        return b"", 0
    blob = json.dumps(meta, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return blob, zlib.crc32(blob) & 0xFFFFFFFF


def unpack_metadata(blob: bytes, crc: int) -> dict:
    """Parse the metadata block and verify its CRC."""
    if not blob:
        return {}
    if (zlib.crc32(blob) & 0xFFFFFFFF) != crc:
        raise CorruptedFileError("metadata CRC mismatch")
    try:
        obj = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CorruptedFileError(f"damaged metadata block: {exc}") from exc
    if not isinstance(obj, dict):
        raise CorruptedFileError("metadata block is not an object")
    return obj


def pack_index(entries: list[tuple]) -> bytes:
    """Serialise the tile index.

    Every entry is (offset, length, method, flags, filter_bytes, crc32)."""
    out = bytearray()
    for offset, length, method, flags, filter_bytes, crc in entries:
        out += _IDX.pack(offset, length, method, flags, filter_bytes, crc)
    return bytes(out)


def unpack_index(blob: bytes, num_tiles: int) -> list[tuple]:
    """Parse the tile index."""
    if len(blob) != num_tiles * INDEX_ENTRY_SIZE:
        raise CorruptedFileError("truncated tile index")
    out = []
    for i in range(num_tiles):
        out.append(_IDX.unpack_from(blob, i * INDEX_ENTRY_SIZE))
    return out


def tile_flag_has_split(flags: int) -> bool:
    return bool(flags & TILE_FLAG_SPLIT)


def tile_flag_is_adaptive(flags: int) -> bool:
    return bool(flags & TILE_FLAG_ADAPTIVE)
