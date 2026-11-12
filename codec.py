from .constants import *
from .predictors import (apply_predictor_row, reconstruct_row,
                         apply_predictor_row_adaptive, pack_filter_bits,
                         unpack_filter_bits, PRED_CANDIDATES)
from .color import (transform_pixels_forward, transform_pixels_inverse,
                    transform_pixels_forward_ex, transform_pixels_inverse_ex)
from .entropy import RangeEncoder, RangeDecoder
from .entropycost import histogram_cost
from .tile import split_into_tiles, assemble_from_tiles, Tile
from .container import PyraContainer
from .compress import (pack_residuals, unpack_residuals, rle_encode, rle_decode,
                       lz_encode, lz_decode, pack_width as _pack_width,
                       TOKEN_EOF, TOKEN_MATCH, TOKEN_SYMBOLS)
from . import native as _native
from . import fast as _fast
from array import array as _array

# Residual-space modular alphabet. For RGB/RGBA the reversible YCoCg
# transform produces exact-integer values that fall outside [0, 2^bitdepth);
# every transformed channel (incl. passthrough alpha) is canonicalized with
# a uniform offset into [0, mod) where mod = 1 << (bit_depth + 3).
# Safety bound: |exact value| <= 2^(d+1) + 2^(d-2) - 3 < 2^(d+2) = mod/2,
# so canonical values always lie in [0, mod) and (res + pred) % mod is exact.
_GRAY_MOD_BITS = 0      # mod = 1 << (bit_depth + _GRAY_MOD_BITS)
_RGB_MOD_BITS = 3       # mod = 1 << (bit_depth + _RGB_MOD_BITS)

# Number of rows used to plan the encoding (transform + row filter choice).
# Planning on a spread sample of rows keeps the encoder fast while staying
# deterministic: the native and the pure-Python path always see the same
# rows and measure them with the same range coder.
_PLAN_SAMPLE_ROWS = 8

class PyraCodec:
    @staticmethod
    def _channels_for_colorspace(color_space: int) -> int:
        if color_space == CS_GRAY:
            return 1
        elif color_space == CS_RGB:
            return 3
        elif color_space == CS_RGBA:
            return 4
        return 3

    @staticmethod
    def _residual_mod(color_space: int, bit_depth: int,
                       transform: int = TRANSFORM_YCOCG) -> int:
        """Alphabet width of the residual/modular domain.

        The reversible YCoCg transform produces values outside
        [0, 2^bit_depth), so RGB/RGBA need a 3-bit wider alphabet. Without
        the transform the channels stay in [0, 2^bit_depth) and the much
        smaller alphabet codes better."""
        if color_space in (CS_RGB, CS_RGBA) and transform == TRANSFORM_YCOCG:
            return 1 << (bit_depth + _RGB_MOD_BITS)
        return 1 << bit_depth

    @staticmethod
    def _canonical_offset(color_space: int, bit_depth: int,
                          transform: int = TRANSFORM_YCOCG) -> int:
        """Uniform offset added to transformed values before prediction."""
        if color_space in (CS_RGB, CS_RGBA) and transform == TRANSFORM_YCOCG:
            return PyraCodec._residual_mod(color_space, bit_depth, transform) >> 1
        return 0

    @staticmethod
    def _transform_candidates(color_space: int) -> tuple[int, ...]:
        """Colour transforms worth trying for this colour space."""
        if color_space in (CS_RGB, CS_RGBA):
            return (TRANSFORM_YCOCG, TRANSFORM_NONE)
        return (TRANSFORM_NONE,)

    @staticmethod
    def _predict_rows(transformed: list[int], width: int, rows: int,
                      channels: int, mod: int, pred_type: int) -> list[int]:
        """Residuals of the first `rows` rows with one fixed predictor."""
        residuals = []
        prev_row = None
        for y in range(rows):
            start = y * width * channels
            row = transformed[start:start + width * channels]
            residuals.extend(apply_predictor_row(row, prev_row, pred_type,
                                                 channels, mod))
            prev_row = row
        return residuals

    @staticmethod
    def _predict_rows_adaptive(transformed: list[int], width: int, rows: int,
                               channels: int, mod: int) -> tuple[list[int], list[int]]:
        """Residuals of the first `rows` rows, every row with its own
        predictor (chosen by modular L1 distance)."""
        residuals = []
        preds = []
        prev_row = None
        for y in range(rows):
            start = y * width * channels
            row = transformed[start:start + width * channels]
            res_row, pred_type = apply_predictor_row_adaptive(
                row, prev_row, channels, mod)
            residuals.extend(res_row)
            preds.append(pred_type)
            prev_row = row
        return residuals, preds

    @staticmethod
    def _plan_cost(residuals: list[int], mod: int) -> int:
        """Size (in bytes) the real range coder needs for these residuals.

        The planner measures candidates with the actual coder, not with an
        entropy estimate: the ideal code length would be biased towards the
        wider YCoCg alphabet and would pick the wrong transform."""
        coder = RangeEncoder(mod)
        for r in residuals:
            coder.encode_symbol(r)
        return len(coder.flush())

    @classmethod
    def _plan_encoding(cls, pixels: list[int], width: int, height: int,
                        bit_depth: int, color_space: int,
                        fixed_transform: int = -1, fixed_filter: int = -1,
                        fixed_pred: int = -1) -> tuple[int, int, int]:
        """Pick (transform, filter_mode, pred_type) by measured coder cost.

        The decision is taken on a spread sample of rows (so every part of
        the image is represented) and every candidate is measured with the
        real range coder. The sample is fixed and the measurement is exact,
        so the native and the pure-Python path always agree on the result."""
        channels = cls._channels_for_colorspace(color_space)

        native = _native.plan(pixels, width, height, channels, bit_depth,
                              color_space, _PLAN_SAMPLE_ROWS,
                              fixed_transform, fixed_filter, fixed_pred)
        if native is not None:
            return native

        if fixed_transform >= 0:
            transforms = (fixed_transform,)
        else:
            transforms = cls._transform_candidates(color_space)

        row_len = width * channels
        rows = min(height, _PLAN_SAMPLE_ROWS)
        sample_idx = [min(height - 1, (i * height) // rows) for i in range(rows)]

        best = None
        for transform in transforms:
            mod = cls._residual_mod(color_space, bit_depth, transform)
            offset = cls._canonical_offset(color_space, bit_depth, transform)

            trows = []
            prevs = []
            for y in sample_idx:
                tr = transform_pixels_forward_ex(
                    pixels[y * row_len:(y + 1) * row_len], color_space, transform)
                if offset:
                    tr = [v + offset for v in tr]
                trows.append(tr)
                if y > 0:
                    pr = transform_pixels_forward_ex(
                        pixels[(y - 1) * row_len:y * row_len], color_space,
                        transform)
                    if offset:
                        pr = [v + offset for v in pr]
                    prevs.append(pr)
                else:
                    prevs.append(None)

            if fixed_filter != FILTER_ADAPTIVE:
                if fixed_pred >= 0:
                    pred_list = (fixed_pred,)
                else:
                    pred_list = PRED_CANDIDATES
                for pred_type in pred_list:
                    res = []
                    for tr, pr in zip(trows, prevs):
                        res.extend(apply_predictor_row(tr, pr, pred_type,
                                                      channels, mod))
                    cost = cls._plan_cost(res, mod)
                    cand = (cost, transform, FILTER_GLOBAL, pred_type)
                    if best is None or cand < best:
                        best = cand
            if fixed_filter != FILTER_GLOBAL:
                res = []
                for tr, pr in zip(trows, prevs):
                    row_res, _pred = apply_predictor_row_adaptive(
                        tr, pr, channels, mod)
                    res.extend(row_res)
                cost = cls._plan_cost(res, mod)
                cand = (cost, transform, FILTER_ADAPTIVE, PRED_NONE)
                if best is None or cand < best:
                    best = cand
        return best[1], best[2], best[3]

    @classmethod
    def _residual_stream(cls, tile: Tile, color_space: int, pred_type: int,
                         bit_depth: int, transform: int = TRANSFORM_YCOCG,
                         filter_mode: int = FILTER_GLOBAL
                         ) -> tuple[list[int], bytes | None]:
        """Shared encoder front half: color transform -> canonicalization ->
        predictor residuals for one tile.

        Returns the residuals and, in adaptive mode, the packed per-row
        predictor selection (3 bits per row)."""
        channels = cls._channels_for_colorspace(color_space)
        mod = cls._residual_mod(color_space, bit_depth, transform)
        offset = cls._canonical_offset(color_space, bit_depth, transform)

        # 0. native fused front half (single buffer conversion for the
        # whole transform + canonicalize + predict pipeline)
        if filter_mode == FILTER_GLOBAL:
            native_residuals = _native.pipe_residuals(
                tile.data, tile.width * channels, offset, mod, pred_type,
                channels, color_space, transform)
            if native_residuals is not None:
                return native_residuals, None

        # 0b. C++ fast path: transform + per-row adaptive prediction in one
        # pass over the tile (SIMD inside).
        if filter_mode == FILTER_ADAPTIVE:
            fast = _fast.adaptive_residuals(
                tile.data, tile.width, tile.height, channels, offset, mod,
                color_space, transform)
            if fast is not None:
                residuals, filter_bits = fast
                return residuals, filter_bits

        # 1. Color transform (reversible YCoCg for RGB/RGBA)
        transformed = transform_pixels_forward_ex(tile.data, color_space,
                                                  transform)

        # 1b. Canonicalize transformed values into [0, mod) so that both
        # sides predict on identical values and (res+pred) % mod is exact.
        if offset:
            transformed = [v + offset for v in transformed]

        # 2. Predictor pass -> residuals
        residuals = []
        filter_preds = []
        prev_row = None
        for y in range(tile.height):
            row_start = y * tile.width * channels
            row_end = row_start + tile.width * channels
            row = transformed[row_start:row_end]
            if filter_mode == FILTER_ADAPTIVE:
                res_row, row_pred = apply_predictor_row_adaptive(
                    row, prev_row, channels, mod)
                filter_preds.append(row_pred)
            else:
                res_row = apply_predictor_row(row, prev_row, pred_type,
                                              channels, mod)
            residuals.extend(res_row)
            prev_row = row
        if filter_mode == FILTER_ADAPTIVE:
            return residuals, pack_filter_bits(filter_preds)
        return residuals, None

    @staticmethod
    def _encode_raw(residuals: list[int], mod: int) -> bytes:
        """METHOD_RAW: residual symbols straight into the entropy coder."""
        native = _native.range_encode(residuals, mod)
        if native is not None:
            return native
        coder = RangeEncoder(mod)
        for r in residuals:
            coder.encode_symbol(r)
        return coder.flush()

    @staticmethod
    def _encode_rle(rle_bytes: bytes) -> bytes:
        """METHOD_RLE: RLE byte stream as literal tokens + EOF terminator."""
        native = _native.range_encode(list(rle_bytes) + [TOKEN_EOF], TOKEN_SYMBOLS)
        if native is not None:
            return native
        coder = RangeEncoder(TOKEN_SYMBOLS)
        for b in rle_bytes:
            coder.encode_symbol(b)
        coder.encode_symbol(TOKEN_EOF)
        return coder.flush()

    @staticmethod
    def _encode_lz(rle_bytes: bytes) -> bytes:
        """METHOD_LZ: 4-byte big-endian RLE-stream-length header (so the
        decoder knows the LZ output target) + LZ77 token stream (already
        EOF-terminated)."""
        tokens = lz_encode(rle_bytes)
        header = [(len(rle_bytes) >> shift) & 0xFF for shift in (24, 16, 8, 0)]
        native = _native.range_encode(header + tokens, TOKEN_SYMBOLS)
        if native is not None:
            return native
        coder = RangeEncoder(TOKEN_SYMBOLS)
        for shift in (24, 16, 8, 0):
            coder.encode_symbol((len(rle_bytes) >> shift) & 0xFF)
        for t in tokens:
            coder.encode_symbol(t)
        return coder.flush()

    @classmethod
    def _tile_candidates(cls, tile: Tile, color_space: int, pred_type: int,
                         bit_depth: int, transform: int = TRANSFORM_YCOCG,
                         filter_mode: int = FILTER_GLOBAL) -> dict[int, bytes]:
        """Encode one tile with all three method stages; residual stream and
        RLE base are computed exactly once."""
        residuals, filter_bits = cls._residual_stream(
            tile, color_space, pred_type, bit_depth, transform, filter_mode)
        mod = cls._residual_mod(color_space, bit_depth, transform)
        packed = pack_residuals(residuals, mod)
        rle = rle_encode(packed)
        prefix = filter_bits or b""
        return {
            METHOD_RAW: prefix + cls._encode_raw(residuals, mod),
            METHOD_RLE: prefix + cls._encode_rle(rle),
            METHOD_LZ: prefix + cls._encode_lz(rle),
        }

    @staticmethod
    def _decode_residuals(data: bytes, method: int, count: int, mod: int,
                          legacy: bool = False) -> list[int]:
        """Residual symbols from a tile byte stream for any method, with
        strict bounded-decode guards (no unbounded memory)."""
        if method == METHOD_RAW:
            native = _native.range_decode(data, mod, count)
            if native is not None:
                return native
            coder = RangeDecoder(data, mod)
            return [coder.decode_symbol() for _ in range(count)]

        width = _pack_width(mod, legacy)
        packed_len = count * width

        if method == METHOD_RLE:
            # Fused native path: range decode + RLE decode + residual unpack
            # in one call. On any error it returns None and the reference
            # path below re-derives the exact reference exception.
            native = _native.decode_rle_method(data, count, mod, legacy)
            if native is not None:
                return native
            coder = RangeDecoder(data, TOKEN_SYMBOLS)
            # RLE cannot exceed ~2x packed size (worst-case 2-byte escapes);
            # + EOF. Each symbol read beyond that is corruption.
            limit = 2 * packed_len + 8
            raw = bytearray()
            reads = 0
            while True:
                if reads > limit:
                    raise CorruptedFileError("RLE token stream exceeds length limit")
                sym = coder.decode_symbol()
                reads += 1
                if sym == TOKEN_EOF:
                    break
                if sym > 255:
                    raise CorruptedFileError(f"invalid RLE byte symbol {sym}")
                raw.append(sym)
            packed = rle_decode(bytes(raw), packed_len)
            return unpack_residuals(packed, mod, count, legacy)

        if method == METHOD_LZ:
            native = _native.decode_lz_method(data, count, mod, legacy)
            if native is not None:
                return native
            coder = RangeDecoder(data, TOKEN_SYMBOLS)
            # 4-byte big-endian length header of the RLE byte stream
            rle_len = 0
            for _ in range(4):
                s = coder.decode_symbol()
                if s > 255:
                    raise CorruptedFileError("invalid RLE length header symbol")
                rle_len = (rle_len << 8) | s
            if rle_len > 2 * packed_len + 64:
                raise CorruptedFileError(f"implausible RLE stream length {rle_len}")
            rle = lz_decode(coder.decode_symbol, rle_len)
            packed = rle_decode(rle, packed_len)
            return unpack_residuals(packed, mod, count, legacy)

        raise CorruptedFileError(f"unknown compression method {method}")

    @classmethod
    def encode_tile(cls, tile: Tile, color_space: int, pred_type: int,
                    bit_depth: int, method: int = METHOD_RAW,
                    transform: int = TRANSFORM_YCOCG,
                    filter_mode: int = FILTER_GLOBAL,
                    legacy: bool = False) -> bytes:
        if method not in (METHOD_RAW, METHOD_RLE, METHOD_LZ):
            raise ValueError(f"invalid compression method: {method}")
        residuals, filter_bits = cls._residual_stream(
            tile, color_space, pred_type, bit_depth, transform, filter_mode)
        mod = cls._residual_mod(color_space, bit_depth, transform)
        prefix = filter_bits or b""
        if method == METHOD_RAW:
            return prefix + cls._encode_raw(residuals, mod)
        packed = pack_residuals(residuals, mod, legacy)
        rle = rle_encode(packed)
        if method == METHOD_RLE:
            return prefix + cls._encode_rle(rle)
        return prefix + cls._encode_lz(rle)

    @classmethod
    def decode_tile(cls, data: bytes, width: int, height: int, color_space: int,
                    pred_type: int, bit_depth: int, method: int = METHOD_RAW,
                    transform: int = TRANSFORM_YCOCG,
                    filter_mode: int = FILTER_GLOBAL,
                    filter_bits: bytes | None = None,
                    legacy: bool = False) -> list[int]:
        channels = PyraCodec._channels_for_colorspace(color_space)
        mod = PyraCodec._residual_mod(color_space, bit_depth, transform)
        offset = PyraCodec._canonical_offset(color_space, bit_depth, transform)

        if filter_mode == FILTER_ADAPTIVE:
            if filter_bits is None:
                nbytes = (height * 3 + 7) // 8
                if len(data) < nbytes:
                    raise CorruptedFileError("tile truncated before row filter table")
                filter_bits = data[:nbytes]
                data = data[nbytes:]

        residuals = cls._decode_residuals(data, method, width * height * channels,
                                         mod, legacy)

        if filter_mode == FILTER_ADAPTIVE:
            fast = _fast.adaptive_reconstruct(
                residuals, width, height, channels, offset, mod,
                color_space, transform, filter_bits)
            if fast is not None:
                return fast
            row_preds = unpack_filter_bits(filter_bits, height)
            transformed = []
            prev_row = None
            for y in range(height):
                row_start = y * width * channels
                row_end = row_start + width * channels
                res_row = residuals[row_start:row_end]
                row = reconstruct_row(res_row, prev_row, row_preds[y],
                                      channels, mod)
                transformed.extend(row)
                prev_row = row
            if offset:
                transformed = [v - offset for v in transformed]
            return transform_pixels_inverse_ex(transformed, color_space,
                                               transform)

        # native fused back half (residuals -> rows -> descale -> inverse
        # color transform in a single buffer pass)
        native_pixels = _native.pipe_reconstruct(
            residuals, width * channels, offset, mod, pred_type,
            channels, color_space, transform)
        if native_pixels is not None:
            return native_pixels

        transformed = []
        prev_row = None
        for y in range(height):
            row_start = y * width * channels
            row_end = row_start + width * channels
            res_row = residuals[row_start:row_end]
            row = reconstruct_row(res_row, prev_row, pred_type, channels, mod)
            transformed.extend(row)
            prev_row = row

        # Undo canonicalization, then inverse color transform
        if offset:
            transformed = [v - offset for v in transformed]
        pixels = transform_pixels_inverse_ex(transformed, color_space, transform)
        return pixels

    @classmethod
    def _encode_native(cls, pixels, width: int, height: int, bit_depth: int,
                       color_space: int, pred_type: int, tile_size: int,
                       method: int, transform: int = TRANSFORM_YCOCG,
                       filter_mode: int = FILTER_GLOBAL) -> bytes | None:
        """Zero-copy native encode of the whole image.

        The pixel data lives in one array('i') canvas; the native core
        extracts every tile, predicts it and encodes all three method
        candidates in place. Returns None when the native backend is
        unavailable or refuses the input, in which case the caller runs the
        pure-Python reference path."""
        if method not in (METHOD_MIX, METHOD_RAW, METHOD_RLE, METHOD_LZ, -1):
            return None
        channels = cls._channels_for_colorspace(color_space)
        mod = cls._residual_mod(color_space, bit_depth, transform)
        offset = cls._canonical_offset(color_space, bit_depth, transform)
        try:
            canvas = _array("i", pixels)
        except (OverflowError, TypeError, ValueError):
            return None
        src_stride = width * channels
        if method == METHOD_MIX or method == -1:
            wanted = (METHOD_RAW, METHOD_RLE, METHOD_LZ)
        else:
            wanted = (method,)
        candidates = []
        for ty in range(0, height, tile_size):
            th = min(tile_size, height - ty)
            for tx in range(0, width, tile_size):
                tw = min(tile_size, width - tx)
                if filter_mode == FILTER_ADAPTIVE:
                    got = _native.tile_candidates_adaptive(
                        canvas, src_stride, tx, ty, tw, th, channels, offset,
                        mod, color_space, wanted, transform)
                    if got is None:
                        return None
                    cand = got[0]
                else:
                    cand = _native.tile_candidates(
                        canvas, src_stride, tx, ty, tw, th, channels, offset,
                        mod, pred_type, color_space, wanted, transform)
                    if cand is None:
                        return None
                candidates.append(cand)
        compressed_tiles, method = cls._select_candidates(candidates, method)
        return PyraContainer.pack_file(width, height, bit_depth, color_space,
                                       pred_type, method, tile_size,
                                       compressed_tiles, transform, filter_mode)

    @staticmethod
    def _select_candidates(candidates: list[dict], method: int) -> tuple:
        """Pick the per-tile method streams exactly like the reference."""
        compressed_tiles = []
        if method == METHOD_MIX:
            for cand in candidates:
                m = min((len(d), mid) for mid, d in cand.items())[1]
                compressed_tiles.append(bytes([m]) + cand[m])
        elif method in (METHOD_RAW, METHOD_RLE, METHOD_LZ):
            compressed_tiles = [cand[method] for cand in candidates]
        else:  # method == -1: one method for the whole image
            totals = [0] * METHOD_COUNT
            for cand in candidates:
                for m, data in cand.items():
                    totals[m] += len(data)
            method = min(range(METHOD_COUNT), key=lambda m: (totals[m], m))
            compressed_tiles = [cand[method] for cand in candidates]
        return compressed_tiles, method

    @classmethod
    def encode_samples(cls, raw: bytes, width: int, height: int,
                       bit_depth: int = 8, color_space: int = CS_RGB,
                       pred_type: int = -1, tile_size: int = 256,
                       method: int = -1, transform: int = -1,
                       filter_mode: int = -1) -> bytes:
        """Encode an image given as raw sample bytes.

        8-bit input holds one sample per byte, 16-bit input holds big-endian
        `u16` samples. The native core widens the samples itself, so no
        intermediate Python list is built; the fallback path converts and
        calls `encode`, which produces the very same bytes.
        """
        channels = cls._channels_for_colorspace(color_space)
        samples = width * height * channels
        need = samples * (2 if bit_depth == 16 else 1)
        if bit_depth not in (8, 16) or len(raw) < need:
            raise ValueError("raw sample buffer too small for the given geometry")
        native_blob = _native.encode_image_bytes(
            raw[:need], width, height, channels, bit_depth, color_space,
            pred_type, method, tile_size, transform, filter_mode,
            _PLAN_SAMPLE_ROWS)
        if native_blob is not None:
            return native_blob
        if bit_depth == 8:
            pixels = list(raw[:need])
        else:
            import struct
            pixels = list(struct.unpack(f">{samples}H", raw[:need]))
        return cls.encode(pixels, width, height, bit_depth, color_space,
                          pred_type, tile_size, method, transform, filter_mode)

    @classmethod
    def encode(cls, pixels: list[int], width: int, height: int,
               bit_depth: int = 8, color_space: int = CS_RGB,
               pred_type: int = -1, tile_size: int = 256, method: int = -1,
               transform: int = -1, filter_mode: int = -1) -> bytes:
        """Encode an image.

        `pred_type`, `transform` and `filter_mode` accept -1 ("auto"), which
        lets the encoder measure the alternatives and pick the cheapest one.
        The choice is deterministic: it is made on a fixed sample of rows.
        """
        channels = cls._channels_for_colorspace(color_space)
        if len(pixels) != width * height * channels:
            raise ValueError(f"Pixel count ({len(pixels)}) != {width}x{height}x{channels}")

        # Native whole-image path: tiles, method candidates and the container
        # are built in one call, with the tiles spread over the available
        # cores. It returns None when the backend is unavailable, in which
        # case the pure-Python reference below runs and produces the very
        # same bytes.
        native_blob = _native.encode_image(
            pixels, width, height, channels, bit_depth, color_space,
            pred_type, method, tile_size, transform, filter_mode,
            _PLAN_SAMPLE_ROWS)
        if native_blob is not None:
            return native_blob

        if pred_type < 0 or transform < 0 or filter_mode < 0:
            p_transform, p_filter, p_pred = cls._plan_encoding(
                pixels, width, height, bit_depth, color_space,
                fixed_transform=transform, fixed_filter=filter_mode,
                fixed_pred=pred_type)
            if transform < 0:
                transform = p_transform
            if filter_mode < 0:
                filter_mode = p_filter
            if pred_type < 0:
                pred_type = p_pred
        if filter_mode == FILTER_ADAPTIVE:
            # The per-row table replaces the global predictor.
            pred_type = PRED_NONE
        if transform not in (TRANSFORM_YCOCG, TRANSFORM_NONE):
            raise ValueError(f"invalid color transform: {transform}")
        if filter_mode not in (FILTER_GLOBAL, FILTER_ADAPTIVE):
            raise ValueError(f"invalid filter mode: {filter_mode}")

        native = cls._encode_native(pixels, width, height, bit_depth,
                                    color_space, pred_type, tile_size, method,
                                    transform, filter_mode)
        if native is not None:
            return native

        tiles = split_into_tiles(pixels, width, height, channels, tile_size)
        if method == METHOD_MIX:
            # Per-tile best method: each tile body starts with a 1-byte
            # method tag, decoded independently (mixed-content images win).
            compressed_tiles = []
            for t in tiles:
                cand = cls._tile_candidates(t, color_space, pred_type,
                                            bit_depth, transform, filter_mode)
                m = min((len(d), mid) for mid, d in cand.items())[1]
                compressed_tiles.append(bytes([m]) + cand[m])
        elif method in (METHOD_RAW, METHOD_RLE, METHOD_LZ):
            # Forced method: single rescue-free encode per tile.
            compressed_tiles = [cls.encode_tile(t, color_space, pred_type,
                                                bit_depth, method, transform,
                                                filter_mode)
                                for t in tiles]
        elif method == -1:
            # Auto: per-tile candidates for every method; pick the single
            # method that minimizes the total compressed size (deterministic
            # tie-break: lower method id wins).
            candidates = [cls._tile_candidates(t, color_space, pred_type,
                                               bit_depth, transform, filter_mode)
                          for t in tiles]
            totals = [0] * METHOD_COUNT
            for cand in candidates:
                for m, data in cand.items():
                    totals[m] += len(data)
            method = min(range(METHOD_COUNT), key=lambda m: (totals[m], m))
            compressed_tiles = [cand[method] for cand in candidates]
        else:
            raise ValueError(f"invalid method: {method} (use -1 for auto)")
        return PyraContainer.pack_file(width, height, bit_depth, color_space,
                                       pred_type, method, tile_size,
                                       compressed_tiles, transform, filter_mode)

    @classmethod
    def _decode_native(cls, meta: dict, compressed_tiles: list[bytes]) -> list[int] | None:
        """Zero-copy native decode of the whole image.

        Every tile is decoded into a residual buffer and reconstructed
        straight into the pixel canvas, so no per-tile Python lists are
        created. Returns None when the native backend is unavailable or
        refuses the stream, in which case the caller runs the pure-Python
        reference path (which raises the exact reference exception)."""
        width = meta["width"]
        height = meta["height"]
        bit_depth = meta["bit_depth"]
        color_space = meta["color_space"]
        pred_type = meta["pred_type"]
        method = meta["method"]
        tile_size = meta["tile_size"]
        transform = meta.get("transform", TRANSFORM_YCOCG)
        filter_mode = meta.get("filter_mode", FILTER_GLOBAL)
        legacy = meta.get("version", VERSION) < VERSION

        channels = cls._channels_for_colorspace(color_space)
        mod = cls._residual_mod(color_space, bit_depth, transform)
        offset = cls._canonical_offset(color_space, bit_depth, transform)
        dst_stride = width * channels
        try:
            canvas = _array("i", bytes(4 * width * height * channels))
        except (OverflowError, TypeError, ValueError, MemoryError):
            return None

        tile_idx = 0
        for ty in range(0, height, tile_size):
            th = min(tile_size, height - ty)
            for tx in range(0, width, tile_size):
                tw = min(tile_size, width - tx)
                tile_bytes = compressed_tiles[tile_idx]
                tile_method = method
                if method == METHOD_MIX:
                    if not tile_bytes:
                        raise CorruptedFileError("empty MIX tile (missing method byte)")
                    tile_method, tile_body = tile_bytes[0], tile_bytes[1:]
                    if tile_method not in (METHOD_RAW, METHOD_RLE, METHOD_LZ):
                        raise CorruptedFileError(f"invalid per-tile method {tile_method}")
                    tile_bytes = tile_body
                filter_bits = None
                if filter_mode == FILTER_ADAPTIVE:
                    nbytes = (th * 3 + 7) // 8
                    if len(tile_bytes) < nbytes:
                        raise CorruptedFileError(
                            "tile truncated before row filter table")
                    filter_bits = tile_bytes[:nbytes]
                    tile_bytes = tile_bytes[nbytes:]
                count = tw * th * channels
                try:
                    res_buf = _array("i", bytes(4 * count))
                except (OverflowError, TypeError, ValueError, MemoryError):
                    return None
                if tile_method == METHOD_RAW:
                    ok = _native.range_decode_into(tile_bytes, mod, count, res_buf)
                elif tile_method == METHOD_RLE:
                    ok = _native.decode_rle_method_into(tile_bytes, count, mod,
                                                        res_buf, legacy)
                else:
                    ok = _native.decode_lz_method_into(tile_bytes, count, mod,
                                                       res_buf, legacy)
                if not ok:
                    return None
                dst_offset = (ty * width + tx) * channels
                if filter_mode == FILTER_ADAPTIVE:
                    ok = _native.pipe_reconstruct_adaptive_into(
                        res_buf, tw * channels, offset, mod, channels,
                        color_space, filter_bits, canvas, dst_offset,
                        dst_stride, transform)
                else:
                    ok = _native.pipe_reconstruct_into(
                        res_buf, tw * channels, offset, mod, pred_type, channels,
                        color_space, canvas, dst_offset, dst_stride, transform)
                if not ok:
                    return None
                tile_idx += 1
        return canvas.tolist()

    @staticmethod
    def _tile_body(meta: dict, tile_bytes: bytes, tile_height: int):
        """Split a stored tile blob into (method, body, filter_bits).

        Handles the per-tile method byte of METHOD_MIX and the leading row
        filter table of the adaptive filter mode."""
        method = meta["method"]
        filter_mode = meta.get("filter_mode", FILTER_GLOBAL)
        tile_method = method
        filter_bits = None
        if method == METHOD_MIX:
            if not tile_bytes:
                raise CorruptedFileError("empty MIX tile (missing method byte)")
            tile_method, tile_bytes = tile_bytes[0], tile_bytes[1:]
            if tile_method not in (METHOD_RAW, METHOD_RLE, METHOD_LZ):
                raise CorruptedFileError(f"invalid per-tile method {tile_method}")
        if filter_mode == FILTER_ADAPTIVE:
            nbytes = (tile_height * 3 + 7) // 8
            if len(tile_bytes) < nbytes:
                raise CorruptedFileError("tile truncated before row filter table")
            filter_bits = tile_bytes[:nbytes]
            tile_bytes = tile_bytes[nbytes:]
        return tile_method, tile_bytes, filter_bits

    @classmethod
    def decode(cls, file_bytes: bytes) -> tuple[dict, list[int]]:
        # Native whole-image path: container parsing, tile decoding and the
        # canvas assembly happen in one call (tiles in parallel). None means
        # "unavailable or malformed" -> the reference path below raises the
        # proper exception.
        native = _native.decode_image(file_bytes)
        if native is not None:
            return native

        meta, compressed_tiles = PyraContainer.unpack_file(file_bytes)
        width = meta["width"]
        height = meta["height"]
        bit_depth = meta["bit_depth"]
        color_space = meta["color_space"]
        pred_type = meta["pred_type"]
        method = meta["method"]
        tile_size = meta["tile_size"]
        transform = meta.get("transform", TRANSFORM_YCOCG)
        filter_mode = meta.get("filter_mode", FILTER_GLOBAL)
        legacy = meta.get("version", VERSION) < VERSION

        native = cls._decode_native(meta, compressed_tiles)
        if native is not None:
            return meta, native

        channels = cls._channels_for_colorspace(color_space)
        tiles = []

        tile_idx = 0
        for ty in range(0, height, tile_size):
            th = min(tile_size, height - ty)
            for tx in range(0, width, tile_size):
                tw = min(tile_size, width - tx)
                tile_method, tile_bytes, filter_bits = cls._tile_body(
                    meta, compressed_tiles[tile_idx], th)
                tile_pixels = cls.decode_tile(
                    tile_bytes, tw, th, color_space, pred_type, bit_depth,
                    tile_method, transform, filter_mode, filter_bits, legacy)
                tiles.append(Tile(tx, ty, tw, th, tile_pixels))
                tile_idx += 1

        pixels = assemble_from_tiles(tiles, width, height, channels)
        return meta, pixels
