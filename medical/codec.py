"""Reference (pure Python) codec of the LUMM medical container.

This module is the *specification* of the format: every byte of a container
is produced here by straightforward code. The native Rust core
(:mod:`lum.medical.native`) implements the very same pipeline and the test
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

from ..entropy import (ContextRangeDecoder, ContextRangeEncoder, RangeDecoder,
                       RangeEncoder)
from .constants import (ESCAPE_ALPHABET, ESCAPE_CHUNK_BITS, ESCAPE_SYMBOL,
                        LZ_TOKEN_EOF, LZ_TOKEN_SYMBOLS, METHOD_LZ, METHOD_RAW,
                        PRED_COUNT, PRED_SLICE, SAMPLE_BYTES,
                        SAMPLE_FORMATS,
                        SAMPLE_SIGNED, SFMT_F32, TILE_FLAG_ADAPTIVE,
                        TILE_FLAG_CONTEXT, TILE_FLAG_ESCAPE, TILE_FLAG_SPLIT,
                        VERSION, ZIGZAG_LIMIT,
                        CorruptedFileError, UnsupportedFormatError,
                        sample_domain)
from . import container as _container
from . import dicom as _dicom
from . import native as _native
from . import signature as _signature
from .lz import lz_decode, lz_encode
from .predictors import (best_row_predictor, candidate_list, merge_planes,
                         pack_filter_bits, pack_residuals, predict_value,
                         reconstruct_row, row_residuals,
                         split_planes, unpack_filter_bits, unpack_residuals)

_ENTROPY_SYMBOLS_RAW = 256
_ENTROPY_SYMBOLS_LZ = LZ_TOKEN_SYMBOLS


# ------------------------------------------------------------------ helpers

class _Format:
    """Coding parameters of a sample format."""

    __slots__ = ("sample_format", "sample_bytes", "values_per_sample",
                 "value_bytes", "modulus", "canon_offset", "signed", "is_float",
                 "escape", "escape_chunks")

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
            # floats are shuffled into byte planes; no escape coding
            self.escape = False
            self.escape_chunks = 0
        else:
            self.values_per_sample = 1
            self.value_bytes = self.sample_bytes
            self.modulus, self.canon_offset = sample_domain(sample_format)
            self.signed = sample_format in SAMPLE_SIGNED
            # wide integer samples use one symbol per sample (zig-zag plus
            # escape) instead of one symbol per byte
            self.escape = self.sample_bytes > 1
            self.escape_chunks = ((8 * self.sample_bytes
                                   + ESCAPE_CHUNK_BITS - 1)
                                  // ESCAPE_CHUNK_BITS)


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


def _canonical_row(samples: bytes, offset: int, count: int, fmt: _Format,
                   step: int = 1) -> list[int]:
    """Read `count` samples and map them into the unsigned code domain.

    With a `step` larger than one the samples are quantized onto a grid of
    that spacing (the near-lossless mode): every sample then differs from
    the original by at most `(step - 1) // 2`. The quantization happens
    *before* the prediction, so the rest of the pipeline stays lossless
    with respect to the quantized values.
    """
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
    half = step // 2
    for i in range(count):
        chunk = samples[offset + i * sb: offset + (i + 1) * sb]
        if len(chunk) < sb:
            raise ValueError("truncated sample buffer")
        value = int.from_bytes(chunk, "little", signed=signed)
        v = (value + canon) % modulus
        if step > 1:
            v = min(((v + half) // step) * step, modulus - 1)
        out.append(v)
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


def _centre(residual: int, modulus: int) -> int:
    """Map a modular residual into the signed range [-modulus/2, modulus/2)."""
    half = modulus >> 1
    return residual if residual < half else residual - modulus


def _escape_worth_it(residuals: list[int], fmt: _Format) -> bool:
    """Decide whether the escape coding beats the byte plane coding.

    The escape coding needs one symbol per sample, but a residual that does
    not fit into +-63 costs one marker plus `escape_chunks` chunk symbols.
    It only pays off when the residuals are concentrated around zero, which
    is the normal case for real medical data (more than 99 % of the
    residuals fit). For noisy data the byte planes are cheaper, so the
    encoder measures the escape rate and picks the better of the two.

    The rule is a fixed fraction, so the encoder and the decoder always
    agree and the tile flag tells the decoder which layout was used.
    """
    if not residuals:
        return True
    limit = fmt.modulus >> 1
    escapes = 0
    for r in residuals:
        c = r if r < limit else r - fmt.modulus
        if c < -ZIGZAG_LIMIT or c > ZIGZAG_LIMIT:
            escapes += 1
            if escapes * 4 > len(residuals):
                return False
    return True


def _escape_symbols(residuals: list[int], fmt: _Format) -> list[int]:
    """Code residuals as one symbol per sample.

    A residual that fits into +-63 is zig-zag encoded into 0..127. Anything
    larger escapes: the marker 128 is followed by a fixed number of 7 bit
    chunks that carry the two's complement bit pattern of the value.
    """
    out: list[int] = []
    bits = 8 * fmt.sample_bytes
    mask = (1 << bits) - 1
    chunks = fmt.escape_chunks
    for r in residuals:
        c = _centre(r, fmt.modulus)
        if -ZIGZAG_LIMIT <= c <= ZIGZAG_LIMIT:
            out.append(2 * c if c >= 0 else -2 * c - 1)
        else:
            out.append(ESCAPE_SYMBOL)
            raw = c & mask
            for k in range(chunks):
                out.append((raw >> (ESCAPE_CHUNK_BITS * k)) & 0x7F)
    return out


def _escape_residuals(symbols, count: int, fmt: _Format) -> list[int]:
    """Inverse of :func:`_escape_symbols`; returns modular residuals.

    `symbols` is an iterable of symbol values (0..127 zig-zag values and the
    escape marker 128)."""
    out: list[int] = []
    bits = 8 * fmt.sample_bytes
    sign_bit = 1 << (bits - 1)
    modulus = 1 << bits
    chunks = fmt.escape_chunks
    it = iter(symbols)
    while len(out) < count:
        try:
            sym = next(it)
        except StopIteration:
            raise CorruptedFileError("residual stream ended too early") from None
        if sym < ESCAPE_SYMBOL:
            if sym & 1:
                c = -((sym + 1) >> 1)
            else:
                c = sym >> 1
        elif sym == ESCAPE_SYMBOL:
            raw = 0
            for k in range(chunks):
                try:
                    part = next(it)
                except StopIteration:
                    raise CorruptedFileError(
                        "escape chunk stream ended too early") from None
                if part >= ESCAPE_SYMBOL:
                    raise CorruptedFileError("invalid escape chunk symbol")
                raw |= part << (ESCAPE_CHUNK_BITS * k)
            if raw & sign_bit:
                raw -= modulus
            c = raw
        else:
            raise CorruptedFileError(f"invalid residual symbol {sym}")
        out.append(c % fmt.modulus)
    return out


def _symbol_stream(coder: RangeDecoder):
    """Infinite stream of decoded symbols."""
    while True:
        yield coder.decode_symbol()


def _entropy_encode_planes(symbols: list[int], n_symbols: int,
                           n_planes: int) -> bytes:
    """Code a byte stream with one adaptive model per byte plane.

    The high planes of a wide sample are long runs of zero bytes, and a
    separate model per plane learns that immediately, while a single shared
    model has to spend its adaptation on the noisy low plane. The decoder
    knows the plane boundaries, so the stream stays self-describing.
    """
    coder = ContextRangeEncoder(n_symbols, n_planes)
    if n_planes < 1:
        n_planes = 1
    per_plane = len(symbols) // n_planes
    for plane in range(n_planes):
        start = plane * per_plane
        for sym in symbols[start:start + per_plane]:
            coder.encode_symbol(sym, plane)
    return coder.flush()


def _entropy_decode_planes(data: bytes, n_symbols: int, n_planes: int,
                           count: int) -> list[int]:
    """Inverse of :func:`_entropy_encode_planes`."""
    coder = ContextRangeDecoder(data, n_symbols, n_planes)
    out: list[int] = []
    for plane in range(n_planes):
        for _ in range(count):
            out.append(coder.decode_symbol(plane))
    return out


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
    # The slice predictor reads the previous slice, which the decoder
    # rebuilds exactly, so it stays available in every mode.
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

    # Two candidate layouts for the residual stream:
    #
    #   * the escape coding - one symbol per sample, with a zig-zag map and
    #     a marker for the rare large residuals;
    #   * the byte planes - the residual bytes split into planes, each with
    #     its own adaptive model.
    #
    # Which one is smaller depends on the data: the escape coding wins when
    # the residuals are small and concentrated (the normal case for real
    # medical data), the planes win when they are wide or noisy. The
    # encoder measures both and keeps the smaller one, so the format never
    # has to guess. The tile flags tell the decoder which layout was used.
    escape_ok = fmt.escape and _escape_worth_it(residuals, fmt)
    if escape_ok:
        symbols = _escape_symbols(residuals, fmt)
        if method == METHOD_LZ:
            tokens = lz_encode(bytes(symbols))
            tokens.append(LZ_TOKEN_EOF)
            stream = _entropy_encode(tokens, _ENTROPY_SYMBOLS_LZ)
        else:
            stream = _entropy_encode(symbols, ESCAPE_ALPHABET)
        return table + stream, method, flags | TILE_FLAG_ESCAPE

    if fmt.value_bytes > 1:
        plane_bytes = split_planes(residuals, fmt.value_bytes)
        plane_flags = TILE_FLAG_SPLIT
    else:
        plane_bytes = pack_residuals(residuals, 1)
        plane_flags = 0

    if method == METHOD_LZ:
        # The LZ stream is not context coded, so it carries no context flag.
        tokens = lz_encode(plane_bytes)
        tokens.append(LZ_TOKEN_EOF)
        stream = _entropy_encode(tokens, _ENTROPY_SYMBOLS_LZ)
        return table + stream, method, flags | plane_flags
    # One adaptive model per byte plane.
    stream = _entropy_encode_planes(list(plane_bytes), _ENTROPY_SYMBOLS_RAW,
                                    fmt.value_bytes)
    return table + stream, method, flags | plane_flags | TILE_FLAG_CONTEXT


def encode(samples: bytes, width: int, height: int, depth: int = 1,
           channels: int = 1, sample_format: int = 1,
           metadata: dict | None = None, tile_size: int = 256,
           predictor: int = -1, method: int = -1,
           require_dicom: bool = False, sign_key: bytes | None = None,
           audit: bool = False, audit_timestamp: str | None = None,
           max_error: int = 0) -> bytes:
    """Encode a medical image into a LUMM container.

    `samples` holds the raw samples in little-endian order (exactly the
    bytes the decoder returns). `predictor` and `method` accept -1, which
    lets the encoder measure the alternatives and pick the cheapest one.

    With `max_error` the container switches to the **near-lossless** mode:
    every sample is reconstructed with an absolute error of at most
    `max_error` (the format stores the residual quantized to a step of
    `2 * max_error + 1`). The decoded image is therefore *not* identical to
    the input any more, but the error is bounded and documented in the
    container. `max_error = 0` is the ordinary lossless mode.

    The near-lossless mode does not apply to floating point samples (they
    are not predicted) and it cannot use the slice predictor, because that
    predictor reads the previous slice, which the decoder rebuilds from the
    quantized residuals.

    With `require_dicom` the metadata block is validated against the image
    geometry before anything is written: a clinical series that claims a
    different geometry, an impossible pixel representation or a missing
    patient/study identifier is refused instead of being stored silently.

    With `sign_key` the container is signed with HMAC-SHA256; the
    signature proves that the holder of the key produced the file and that
    nobody modified it afterwards. See :mod:`lum.medical.signature`.

    With `audit` a deterministic provenance block is added to the metadata
    (encoder, codec, geometry and the SHA-256 of the source samples). The
    block carries no timestamp unless `audit_timestamp` is given, because a
    timestamp would make every encoding of the same data different.
    """
    if audit:
        from .. import __version__
        metadata = dict(metadata or {})
        _fmt = _Format(sample_format)
        _n = width * height * depth * channels * _fmt.sample_bytes
        metadata["Audit"] = _signature.audit_block(
            samples[:_n], width, height, depth, channels, sample_format,
            encoder=f"lum/{__version__}", codec=f"LUMM-v{VERSION}",
            timestamp=audit_timestamp)
    if max_error < 0:
        raise ValueError("the error bound must not be negative")
    step = 2 * int(max_error) + 1
    blob = _encode_core(samples, width, height, depth, channels,
                        sample_format, metadata, tile_size, predictor, method,
                        require_dicom, step)
    if sign_key is not None:
        blob = _signature.sign(blob, sign_key)
    return blob


def _encode_core(samples: bytes, width: int, height: int, depth: int = 1,
           channels: int = 1, sample_format: int = 1,
           metadata: dict | None = None, tile_size: int = 256,
           predictor: int = -1, method: int = -1,
           require_dicom: bool = False, step: int = 1) -> bytes:
    """Encode a medical image into a LUMM container.

    `samples` holds the raw samples in little-endian order (exactly the
    bytes the decoder returns). `predictor` and `method` accept -1, which
    lets the encoder measure the alternatives and pick the cheapest one.

    With `require_dicom` the metadata block is validated against the image
    geometry before anything is written: a clinical series that claims a
    different geometry, an impossible pixel representation or a missing
    patient/study identifier is refused instead of being stored silently.
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
    if predictor < -1 or predictor >= PRED_COUNT:
        raise UnsupportedFormatError(f"unsupported predictor {predictor}")
    if fmt.is_float:
        predictor = 0  # float samples are shuffled, not predicted
    if step > 1:
        if fmt.is_float:
            raise UnsupportedFormatError(
                "the near-lossless mode does not apply to floating point "
                "samples: they are not predicted")
    if require_dicom:
        _dicom.validate_dicom_metadata(metadata, width, height, depth,
                                        channels, sample_format, strict=True)

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
        method=method, step=step)
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
            slice_rows.append(_canonical_row(samples, base, width * channels,
                                              fmt, step))
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
                                                     slice_ctx, predictor,
                                                     METHOD_LZ)
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
        predictor=predictor if predictor >= 0 else 0, sha256=digest,
        step=step)
    return (_container.pack_header(hdr, digest) + meta_blob + index_blob
            + bytes(payload))


# ------------------------------------------------------------------- decoder

def _decode_tile(body: bytes, flags: int, method: int, tw: int, th: int,
                 channels: int, fmt: _Format, slice_rows: list[list[int]] | None,
                 predictor: int, step: int = 1) -> list[list[int]]:
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

    if flags & TILE_FLAG_ESCAPE:
        # One symbol per sample: zig-zag values plus escaped residuals.
        if method == METHOD_LZ:
            coder = RangeDecoder(stream, _ENTROPY_SYMBOLS_LZ)
            tokens = []
            # one LZ token per symbol byte at worst (a match of four bytes
            # costs four tokens), and the symbol stream is at most
            # count * (1 + escape_chunks) bytes long
            limit = count * (1 + fmt.escape_chunks) + 64
            while True:
                sym = coder.decode_symbol()
                tokens.append(sym)
                if sym == LZ_TOKEN_EOF:
                    break
                if len(tokens) > limit:
                    raise CorruptedFileError("LZ token stream exceeds length limit")
            packed = lz_decode(tokens, None, count * 4 + 64)
            residuals = _escape_residuals(packed, count, fmt)
        elif flags & TILE_FLAG_CONTEXT:
            coder = ContextRangeDecoder(stream, ESCAPE_ALPHABET,
                                        CONTEXT_CLASSES)
            residuals = _escape_residuals(_symbol_stream_context(coder),
                                          count, fmt)
        else:
            coder = RangeDecoder(stream, ESCAPE_ALPHABET)
            residuals = _escape_residuals(_symbol_stream(coder), count, fmt)
    else:
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
        elif flags & TILE_FLAG_CONTEXT:
            packed = _entropy_decode_planes(stream, _ENTROPY_SYMBOLS_RAW,
                                            fmt.value_bytes, count)
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
        rows.append(reconstruct_row(row_res, prev, slice_row, channels,
                                    fmt.modulus, pred_ids[r]))
    return rows


def decode(blob: bytes, verify_key: bytes | None = None) -> tuple[dict, bytes]:
    """Decode a LUMM container.

    Returns (metadata, raw samples). Every integrity check is performed:
    header CRC, metadata CRC, per tile CRC, payload CRC and the SHA-256
    content hash of the decoded samples.

    When the container is signed, `verify_key` is used to check the
    HMAC-SHA256 signature: a key that does not match raises
    :class:`CorruptedFileError` instead of returning the data. Without a
    key a signed container still decodes (the signature is reported as
    "unverified" by :func:`verify`), because an unsigned consumer must be
    able to read a signed file.
    """
    hdr = _container.unpack_header(blob)
    _signature.status(blob, verify_key)
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
                                slice_ctx, hdr.predictor, hdr.step)
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
    if hdr.step <= 1 and hashlib.sha256(bytes(out)).digest() != hdr.sha256:
        raise CorruptedFileError("content hash mismatch")
    return metadata, bytes(out)


def _peek_pred_ids(body: bytes, flags: int, th: int, predictor: int) -> list[int]:
    """The predictor id of every row of a tile, without decoding it."""
    if flags & TILE_FLAG_ADAPTIVE:
        nbytes = (th + 1) // 2
        if len(body) < nbytes:
            raise CorruptedFileError("tile truncated before row filter table")
        return unpack_filter_bits(body[:nbytes], th)
    return [predictor] * th


def decode_region(blob: bytes, x: int, y: int, z: int, width: int,
                  height: int, depth: int = 1,
                  verify_key: bytes | None = None) -> tuple[dict, bytes]:
    """Decode only a rectangular region of a container.

    The region is given in samples: ``x``/``y`` are the first column and
    row of the first slice, ``z`` the first slice, and ``width``/``height``
    /``depth`` the size of the region. Only the tiles that touch the region
    are decoded, so a small window of a large volume costs a small window.

    The result is the tight region: slice by slice, row by row, and only
    the requested columns - exactly what a viewer needs to paint the
    window.

    Every check that does not need the complete sample set is performed:
    the magic bytes, the version, the header CRC, the metadata CRC, the
    tile index, the CRC of every tile that is touched, and the signature.
    The whole-payload CRC and the SHA-256 content hash are **not**
    checked, because they need every sample of the container - which is
    precisely what this call avoids decoding. Use :func:`decode` when the
    full verification matters.
    """
    hdr = _container.unpack_header(blob)
    fmt = _Format(hdr.sample_format)
    if hdr.bits != 8 * fmt.sample_bytes:
        raise CorruptedFileError("bits per sample does not match the format")
    # the signature covers the whole container, so a region read still
    # proves that the file was not tampered with
    _signature.status(blob, verify_key)

    meta_blob = blob[hdr.meta_offset:hdr.meta_offset + hdr.meta_size]
    metadata = _container.unpack_metadata(meta_blob, hdr.meta_crc)

    index_blob = blob[hdr.index_offset:hdr.index_offset + hdr.index_size]
    entries = _container.unpack_index(index_blob, hdr.num_tiles)

    if width <= 0 or height <= 0 or depth <= 0:
        raise ValueError("the region size must be positive")
    if x < 0 or y < 0 or z < 0:
        raise ValueError("the region origin must not be negative")
    if x + width > hdr.width or y + height > hdr.height:
        raise ValueError("the region does not fit into the slice")
    if z + depth > hdr.depth:
        raise ValueError("the region does not fit into the volume")

    # Native region path: only the tiles that touch the window are decoded.
    # The signature has already been verified above, so the native core only
    # has to do the structural checks. None means "not handled here" and the
    # reference path below then runs and raises the very same exception.
    native_region = _native.decode_region(blob, x, y, z, width, height, depth)
    if native_region is not None:
        return metadata, native_region

    grid = list(_tile_grid(hdr.width, hdr.height, hdr.tile_size))
    tiles_per_slice = len(grid)
    if hdr.num_tiles != tiles_per_slice * hdr.depth:
        raise CorruptedFileError("tile count does not match the geometry")

    cache: dict[tuple[int, int], list[list[int]]] = {}

    def tile_rows(zz: int, t: int) -> list[list[int]]:
        """The canonical rows of one tile, with the slice context if needed."""
        hit = cache.get((zz, t))
        if hit is not None:
            return hit
        tx, ty, tw, th = grid[t]
        offset, length, method, flags, _filter_bytes, crc = entries[
            zz * tiles_per_slice + t]
        if offset + length > len(blob):
            raise CorruptedFileError("tile body outside the container")
        body = blob[offset:offset + length]
        if (zlib.crc32(body) & 0xFFFFFFFF) != crc:
            raise CorruptedFileError("tile CRC mismatch")
        # the volumetric predictor reads the previous slice, so the context
        # tile is only decoded when the row table actually asks for it
        need_ctx = False
        if zz > 0:
            for pred_id in _peek_pred_ids(body, flags, th, hdr.predictor):
                if pred_id == PRED_SLICE:
                    need_ctx = True
                    break
        ctx = tile_rows(zz - 1, t) if need_ctx else None
        rows = _decode_tile(body, flags, method, tw, th, hdr.channels, fmt,
                            ctx, hdr.predictor, hdr.step)
        cache[(zz, t)] = rows
        return rows

    vps = fmt.values_per_sample
    grid_index = {geom: i for i, geom in enumerate(grid)}
    out = bytearray()
    # the region is laid out slice by slice and row by row, exactly like
    # the tight crop of the full decode
    for zz in range(z, z + depth):
        for r in range(y, y + height):
            for (tx, ty, tw, th) in grid:
                if not (ty <= r < ty + th):
                    continue
                c0 = max(x, tx)
                c1 = min(x + width, tx + tw)
                if c0 >= c1:
                    continue
                rows = tile_rows(zz, grid_index[(tx, ty, tw, th)])
                lo = (c0 - tx) * hdr.channels * vps
                hi = (c1 - tx) * hdr.channels * vps
                out += _decode_row(rows[r - ty][lo:hi], fmt)
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
        "num_tiles": hdr.num_tiles, "step": hdr.step,
    }


def verify(blob: bytes, key: bytes | None = None) -> dict:
    """Check every integrity guarantee of a container without decoding it.

    Returns a report dictionary. When the container is intact the report has
    ``"ok": True``; otherwise the matching check raises
    :class:`CorruptedFileError` (a damaged container is never reported as
    healthy).

    The checks are: the magic bytes and the version, the header CRC-32, the
    tile index size against the tile count, the metadata CRC-32, the CRC-32
    of every tile body, the CRC-32 of the whole payload and the SHA-256 of
    the decoded samples.

    With `key` the HMAC-SHA256 signature of a signed container is verified
    as well; the report carries the signature state in ``"signature"``.
    """
    hdr = _container.unpack_header(blob)
    signature_state = _signature.status(blob, key)
    fmt = _Format(hdr.sample_format)
    if hdr.bits != 8 * fmt.sample_bytes:
        raise CorruptedFileError("bits per sample does not match the format")

    meta_blob = blob[hdr.meta_offset:hdr.meta_offset + hdr.meta_size]
    metadata = _container.unpack_metadata(meta_blob, hdr.meta_crc)

    index_blob = blob[hdr.index_offset:hdr.index_offset + hdr.index_size]
    entries = _container.unpack_index(index_blob, hdr.num_tiles)

    grid = list(_tile_grid(hdr.width, hdr.height, hdr.tile_size))
    tiles_per_slice = len(grid)
    if hdr.num_tiles != tiles_per_slice * hdr.depth:
        raise CorruptedFileError("tile count does not match the geometry")

    payload = bytearray()
    for offset, length, method, flags, _filter_bytes, crc in entries:
        if offset + length > len(blob):
            raise CorruptedFileError("tile body outside the container")
        body = blob[offset:offset + length]
        if (zlib.crc32(body) & 0xFFFFFFFF) != crc:
            raise CorruptedFileError("tile CRC mismatch")
        payload += body
    if (zlib.crc32(bytes(payload)) & 0xFFFFFFFF) != hdr.payload_crc:
        raise CorruptedFileError("payload CRC mismatch")

    # the content hash needs the decoded samples
    samples = decode(blob)[1]
    if hdr.step <= 1 and hashlib.sha256(samples).digest() != hdr.sha256:
        raise CorruptedFileError("content hash mismatch")

    # The clinical metadata is checked against the geometry of the
    # container: a block that contradicts the image (wrong number of rows,
    # a pixel representation that does not fit the sample format, ...) is
    # reported with the exact problems, so a reader can see *why* a
    # container is not clinically usable.
    try:
        clinical_problems = _dicom.validate_dicom_metadata(
            metadata, hdr.width, hdr.height, hdr.depth, hdr.channels,
            hdr.sample_format, strict=False)
    except _dicom.MedicalMetadataError:
        clinical_problems = ["metadata block is missing"]

    return {
        "ok": True,
        "version": hdr.version,
        "width": hdr.width,
        "height": hdr.height,
        "depth": hdr.depth,
        "channels": hdr.channels,
        "sample_format": hdr.sample_format,
        "bits": hdr.bits,
        "tile_size": hdr.tile_size,
        "num_tiles": hdr.num_tiles,
        "payload_bytes": len(payload),
        "metadata_bytes": hdr.meta_size,
        "metadata": metadata,
        "dicom": _dicom.describe(metadata),
        "dicom_problems": clinical_problems,
        "dicom_compatible": _dicom.is_dicom_compatible(
            metadata, hdr.width, hdr.height, hdr.depth, hdr.channels,
            hdr.sample_format),
        "signature": signature_state,
    }


__all__ = ["encode", "decode", "info", "verify", "METHOD_RAW", "METHOD_LZ"]
