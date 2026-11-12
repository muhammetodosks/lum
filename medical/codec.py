"""Reference (pure Python) codec of the PYRM medical container.

This module is the *specification* of the format: every byte of a container
is produced here by straightforward code. The native Rust core
(:mod:`pyra.medical.native`) implements the very same pipeline and the test
suite asserts that both produce identical bytes.

Layout of a tile body::

    [row filter table]   (only when the tile uses the adaptive filter)
    [entropy stream]     (range coded symbols)

The entropy stream carries the *packed residual bytes* of the tile, either
directly (``METHOD_RAW``) or as an LZ77 token stream (``METHOD_LZ``).

Code units
----------
Every sample is turned into one or more *code values*:

===============  ==================  ==========  =====================
sample format    code values         value bytes modulus
===============  ==================  ==========  =====================
U8 / I16 / U16   1 per sample        1 / 2 / 2   2**bits
U32 / I32        1 per sample        4          2**bits
F32              4 per sample        1          256 (byte planes)
===============  ==================  ==========  =====================

Float samples are shuffled into their four byte planes before coding, so
the entropy coder sees smooth bytes instead of random float bit patterns.
"""

import hashlib
import zlib

from ..entropy import RangeDecoder, RangeEncoder
from .constants import (LZ_TOKEN_EOF, LZ_TOKEN_SYMBOLS, METHOD_LZ, METHOD_RAW,
                        SAMPLE_BYTES, SAMPLE_FORMATS, SAMPLE_SIGNED, SFMT_F32,
                        TILE_FLAG_ADAPTIVE, TILE_FLAG_SPLIT, VERSION,
                        CorruptedFileError, UnsupportedFormatError,
                        sample_domain)
from . import container as _container
from . import native as _native
from .lz import lz_decode, lz_encode
from .predictors import (best_row_predictor, candidate_list, merge_planes,
                         pack_filter_bits, pack_residuals, predict_value,
                         row_residuals, split_planes, unpack_filter_bits,
                         unpack_residuals)

_ENTROPY_SYMBOLS_RAW = 256
_ENTROPY_SYMBOLS_LZ = LZ_TOKEN_SYMBOLS


# ------------------------------------------------------------------ helpers

class _Format:
    """Coding parameters of a sample format."""

    __slots__ = ("sample_format", "sample_bytes", "values_per_sample",
                 "value_bytes", "modulus", "canon_offset", "signed", "is_float")

    def __init__(self, sample_format: int):
        if sample_format not in SAMPLE_FORMATS:
            raise UnsupportedFormatError(f"unknown sample format {sample_format}")
        self.sample_format = sample_format
        self.sample_bytes = SAMPLE_BYTES[sample_format]
        self.is_float = sample_format == SFMT_F32
        if self.is_float:
            self.values_per_sample = 4
            self.value_bytes = 1
            self.modulus = 256
            self.canon_offset = 0
            self.signed = False
        else:
            self.values_per_sample = 1
            self.value_bytes = self.sample_bytes
            self.modulus, self.canon_offset = sample_domain(sample_format)
            self.signed = sample_format in SAMPLE_SIGNED


def _tile_grid(width: int, height: int, tile_size: int):
    """Yield (tx, ty, tw, th) for every tile of a slice."""
    ty = 0
    while ty < height:
        th = tile_size if tile_size < height - ty else height - ty
        tx = 0
        while tx < width:
            tw = tile_size if tile_size < width - tx else width - tx
            yield tx, ty, tw, th
            tx += tile_size
        ty += tile_size


def _canonical_row(samples: bytes, offset: int, count: int, fmt: _Format) -> list[int]:
    """Read `count` samples and map them into the unsigned code domain."""
    out = []
    if fmt.is_float:
        for i in range(count):
            base = offset + i * 4
            for plane in range(4):
                out.append(samples[base + plane])
        return out
    sb = fmt.sample_bytes
    modulus = fmt.modulus
    canon = fmt.canon_offset
    signed = fmt.signed
    for i in range(count):
        chunk = samples[offset + i * sb: offset + (i + 1) * sb]
        if len(chunk) < sb:
            raise ValueError("truncated sample buffer")
        value = int.from_bytes(chunk, "little", signed=signed)
        out.append((value + canon) % modulus)
    return out


def _decode_row(values: list[int], fmt: _Format) -> bytes:
    """Inverse of :func:`_canonical_row`."""
    out = bytearray()
    if fmt.is_float:
        for i in range(0, len(values), 4):
            for plane in range(4):
                out.append(values[i + plane] & 0xFF)
        return bytes(out)
    sb = fmt.sample_bytes
    modulus = fmt.modulus
    canon = fmt.canon_offset
    signed = fmt.signed
    for v in values:
        # `v` lives in [0, modulus); for signed formats `v - canon` is the
        # two's complement value and fits the signed sample width.
        out += (v - canon).to_bytes(sb, "little", signed=signed)
    return bytes(out)


def _entropy_encode(symbols: list[int], n_symbols: int) -> bytes:
    coder = RangeEncoder(n_symbols)
    for sym in symbols:
        coder.encode_symbol(sym)
    return coder.flush()


def _entropy_decode(data: bytes, n_symbols: int, count: int) -> list[int]:
    coder = RangeDecoder(data, n_symbols)
    return [coder.decode_symbol() for _ in range(count)]


# ------------------------------------------------------------------- encoder

def _encode_tile(rows: list[list[int]], channels: int, fmt: _Format,
                 slice_rows: list[list[int]] | None, predictor: int,
                 method: int) -> tuple[bytes, int, int]:
    """Encode one tile.

    Returns (body bytes, method, flags).
    """
    flags = 0
    candidates = candidate_list(2 if slice_rows is not None else 1)
    table = b""
    pred_ids: list[int] = []
    residuals: list[int] = []
    for r, row in enumerate(rows):
        prev = rows[r - 1] if r > 0 else None
        slice_row = slice_rows[r] if slice_rows is not None else None
        if predictor < 0:
            pred_id, res = best_row_predictor(row, prev, slice_row, channels,
                                              fmt.modulus, candidates)
            pred_ids.append(pred_id)
        else:
            pred_id = predictor
            res = row_residuals(row, prev, slice_row, channels, fmt.modulus, pred_id)
        residuals.extend(res)
    if predictor < 0:
        table = pack_filter_bits(pred_ids)
        flags |= TILE_FLAG_ADAPTIVE

    if fmt.value_bytes > 1:
        packed = split_planes(residuals, fmt.value_bytes)
        flags |= TILE_FLAG_SPLIT
    else:
        packed = pack_residuals(residuals, 1)

    if method == METHOD_LZ:
        tokens = lz_encode(packed)
        tokens.append(LZ_TOKEN_EOF)
        stream = _entropy_encode(tokens, _ENTROPY_SYMBOLS_LZ)
    else:
        stream = _entropy_encode(list(packed), _ENTROPY_SYMBOLS_RAW)
    return table + stream, method, flags


def encode(samples: bytes, width: int, height: int, depth: int = 1,
           channels: int = 1, sample_format: int = 1,
           metadata: dict | None = None, tile_size: int = 256,
           predictor: int = -1, method: int = -1) -> bytes:
    """Encode a medical image into a PYRM container.

    `samples` holds the raw samples in little-endian order (exactly the
    bytes the decoder returns). `predictor` and `method` accept -1, which
    lets the encoder measure the alternatives and pick the cheapest one.
    """
    fmt = _Format(sample_format)
    if width <= 0 or height <= 0 or depth <= 0 or channels <= 0:
        raise ValueError("degenerate image geometry")
    if tile_size <= 0:
        raise ValueError("invalid tile size")
    total_samples = width * height * depth * channels
    if len(samples) < total_samples * fmt.sample_bytes:
        raise ValueError("sample buffer too small for the given geometry")
    if method not in (-1, METHOD_RAW, METHOD_LZ):
        raise UnsupportedFormatError(f"unsupported method {method}")
    if predictor < -1 or predictor > 7:
        raise UnsupportedFormatError(f"unsupported predictor {predictor}")
    if fmt.is_float:
        predictor = 0  # float samples are shuffled, not predicted

    grid = list(_tile_grid(width, height, tile_size))
    tiles_per_slice = len(grid)

    # Native whole-image path: tiles are coded in parallel and the container
    # is assembled in one call. The metadata block is produced here (the
    # native core only stores it verbatim) and the result is byte-identical
    # to the reference path below.
    meta_blob, _meta_crc = _container.pack_metadata(metadata)
    native_blob = _native.encode(
        samples, width, height, depth, channels, sample_format,
        metadata=meta_blob, tile_size=tile_size, predictor=predictor,
        method=method)
    if native_blob is not None:
        return native_blob

    bodies: list[bytes] = []
    tile_methods: list[int] = []
    tile_flags: list[int] = []

    prev_slice: list[list[int]] | None = None
    for z in range(depth):
        slice_offset = z * width * height * channels * fmt.sample_bytes
        slice_rows = []
        for y in range(height):
            base = slice_offset + y * width * channels * fmt.sample_bytes
            slice_rows.append(_canonical_row(samples, base, width * channels, fmt))
        vps = fmt.values_per_sample
        for (tx, ty, tw, th) in grid:
            lo = tx * channels * vps
            hi = lo + tw * channels * vps
            rows = [slice_rows[ty + r][lo:hi] for r in range(th)]
            slice_ctx = None
            if prev_slice is not None:
                slice_ctx = [prev_slice[ty + r][lo:hi] for r in range(th)]
            raw_body, raw_flags = _encode_tile(rows, channels, fmt, slice_ctx,
                                               predictor, METHOD_RAW)[0:3:2]
            if method == -1:
                lz_body, _, lz_flags = _encode_tile(rows, channels, fmt,
                                                     slice_ctx, predictor, METHOD_LZ)
                if len(lz_body) < len(raw_body):
                    body, m, flags = lz_body, METHOD_LZ, lz_flags
                else:
                    body, m, flags = raw_body, METHOD_RAW, raw_flags
            elif method == METHOD_LZ:
                body, m, flags = _encode_tile(rows, channels, fmt, slice_ctx,
                                              predictor, METHOD_LZ)
            else:
                body, m, flags = raw_body, METHOD_RAW, raw_flags
            bodies.append(body)
            tile_methods.append(m)
            tile_flags.append(flags)
        prev_slice = slice_rows

    # ---- assemble
    meta_blob, meta_crc = _container.pack_metadata(metadata)
    header_size = _container.HEADER_SIZE
    index_size = len(bodies) * _container.INDEX_ENTRY_SIZE
    meta_offset = header_size
    index_offset = meta_offset + len(meta_blob)
    payload_offset = index_offset + index_size

    entries = []
    offset = payload_offset
    payload = bytearray()
    for i, body in enumerate(bodies):
        entries.append((offset, len(body), tile_methods[i], tile_flags[i],
                        0, zlib.crc32(body) & 0xFFFFFFFF))
        payload += body
        offset += len(body)

    index_blob = _container.pack_index(entries)
    payload_crc = zlib.crc32(bytes(payload)) & 0xFFFFFFFF
    digest = hashlib.sha256(samples[:total_samples * fmt.sample_bytes]).digest()

    hdr = _container.Header(
        version=VERSION, flags=0, width=width, height=height, depth=depth,
        channels=channels, sample_format=sample_format,
        bits=8 * fmt.sample_bytes, tile_size=tile_size, num_tiles=len(bodies),
        index_offset=index_offset, index_size=index_size,
        meta_offset=meta_offset, meta_size=len(meta_blob), meta_crc=meta_crc,
        payload_crc=payload_crc, header_crc=0,
        predictor=predictor if predictor >= 0 else 0, sha256=digest)
    return (_container.pack_header(hdr, digest) + meta_blob + index_blob
            + bytes(payload))


# ------------------------------------------------------------------- decoder

def _decode_tile(body: bytes, flags: int, method: int, tw: int, th: int,
                 channels: int, fmt: _Format, slice_rows: list[list[int]] | None,
                 predictor: int) -> list[list[int]]:
    """Decode one tile into canonical rows."""
    if flags & TILE_FLAG_ADAPTIVE:
        nbytes = (th + 1) // 2
        if len(body) < nbytes:
            raise CorruptedFileError("tile truncated before row filter table")
        table = body[:nbytes]
        stream = body[nbytes:]
        pred_ids = unpack_filter_bits(table, th)
    else:
        stream = body
        pred_ids = [predictor] * th
    if method not in (METHOD_RAW, METHOD_LZ):
        raise CorruptedFileError(f"unsupported tile method {method}")

    per_row = tw * channels * fmt.values_per_sample
    count = per_row * th
    if method == METHOD_LZ:
        coder = RangeDecoder(stream, _ENTROPY_SYMBOLS_LZ)
        tokens = []
        limit = count * fmt.value_bytes * 2 + 64
        while True:
            sym = coder.decode_symbol()
            tokens.append(sym)
            if sym == LZ_TOKEN_EOF:
                break
            if len(tokens) > limit:
                raise CorruptedFileError("LZ token stream exceeds length limit")
        packed = lz_decode(tokens, count * fmt.value_bytes)
    else:
        packed = _entropy_decode(stream, _ENTROPY_SYMBOLS_RAW,
                                 count * fmt.value_bytes)

    if flags & TILE_FLAG_SPLIT:
        residuals = merge_planes(packed, count, fmt.value_bytes)
    else:
        residuals = unpack_residuals(packed, count, fmt.value_bytes)

    rows: list[list[int]] = []
    for r in range(th):
        row_res = residuals[r * per_row:(r + 1) * per_row]
        prev = rows[r - 1] if r > 0 else None
        slice_row = slice_rows[r] if slice_rows is not None else None
        row = [0] * per_row
        for i in range(len(row_res)):
            x = i // channels
            left = row[i - channels] if x > 0 else 0
            up = prev[i] if prev is not None else 0
            upper_left = prev[i - channels] if (prev is not None and x > 0) else 0
            slice_val = slice_row[i] if slice_row is not None else 0
            pred = predict_value(pred_ids[r], left, up, upper_left, slice_val)
            row[i] = (row_res[i] + pred) % fmt.modulus
        rows.append(row)
    return rows


def decode(blob: bytes) -> tuple[dict, bytes]:
    """Decode a PYRM container.

    Returns (metadata, raw samples). Every integrity check is performed:
    header CRC, metadata CRC, per tile CRC, payload CRC and the SHA-256
    content hash of the decoded samples.
    """
    hdr = _container.unpack_header(blob)
    fmt = _Format(hdr.sample_format)
    if hdr.bits != 8 * fmt.sample_bytes:
        raise CorruptedFileError("bits per sample does not match the format")

    meta_blob = blob[hdr.meta_offset:hdr.meta_offset + hdr.meta_size]
    metadata = _container.unpack_metadata(meta_blob, hdr.meta_crc)

    # Native whole-image path: the native core performs every integrity check
    # (header, metadata, per tile, payload and the SHA-256 content hash) and
    # returns the raw samples. None means "not handled here" (unavailable
    # backend or damaged container) and the reference path below then runs
    # and raises the very same exception.
    native_samples = _native.decode(blob)
    if native_samples is not None:
        return metadata, native_samples

    index_blob = blob[hdr.index_offset:hdr.index_offset + hdr.index_size]
    entries = _container.unpack_index(index_blob, hdr.num_tiles)

    width, height = hdr.width, hdr.height
    channels = hdr.channels

    grid = list(_tile_grid(width, height, hdr.tile_size))
    tiles_per_slice = len(grid)
    if hdr.num_tiles != tiles_per_slice * hdr.depth:
        raise CorruptedFileError("tile count does not match the geometry")

    row_bytes = width * channels * fmt.sample_bytes
    out = bytearray(row_bytes * height * hdr.depth)
    payload = bytearray()

    prev_slice: list[list[int]] | None = None
    for z in range(hdr.depth):
        vps = fmt.values_per_sample
        slice_rows: list[list[int]] = [[0] * (width * channels * vps)
                                       for _ in range(height)]
        for t, (tx, ty, tw, th) in enumerate(grid):
            offset, length, method, flags, _filter_bytes, crc = entries[z * tiles_per_slice + t]
            if offset + length > len(blob):
                raise CorruptedFileError("tile body outside the container")
            body = blob[offset:offset + length]
            if (zlib.crc32(body) & 0xFFFFFFFF) != crc:
                raise CorruptedFileError("tile CRC mismatch")
            payload += body
            lo = tx * channels * vps
            hi = lo + tw * channels * vps
            slice_ctx = None
            if prev_slice is not None:
                slice_ctx = [prev_slice[ty + r][lo:hi] for r in range(th)]
            rows = _decode_tile(body, flags, method, tw, th, channels, fmt,
                                slice_ctx, hdr.predictor)
            for r, row in enumerate(rows):
                slice_rows[ty + r][lo:lo + len(row)] = row
                # byte offset of the tile column inside the slice
                byte_off = ((z * height + ty + r) * row_bytes
                            + tx * channels * fmt.sample_bytes)
                out[byte_off:byte_off + len(row) * fmt.value_bytes] = \
                    _decode_row(row, fmt)
        prev_slice = slice_rows

    if (zlib.crc32(bytes(payload)) & 0xFFFFFFFF) != hdr.payload_crc:
        raise CorruptedFileError("payload CRC mismatch")
    if hashlib.sha256(bytes(out)).digest() != hdr.sha256:
        raise CorruptedFileError("content hash mismatch")
    return metadata, bytes(out)


def info(blob: bytes) -> dict:
    """Read the header of a container without decoding the samples."""
    native_info = _native.info(blob)
    if native_info is not None:
        return native_info
    hdr = _container.unpack_header(blob)
    return {
        "width": hdr.width, "height": hdr.height, "depth": hdr.depth,
        "channels": hdr.channels, "sample_format": hdr.sample_format,
        "bits": hdr.bits, "tile_size": hdr.tile_size,
        "num_tiles": hdr.num_tiles,
    }


__all__ = ["encode", "decode", "info", "METHOD_RAW", "METHOD_LZ"]
