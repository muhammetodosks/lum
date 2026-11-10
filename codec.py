from .constants import *
from .predictors import apply_predictor_row, reconstruct_row
from .color import transform_pixels_forward, transform_pixels_inverse
from .entropy import RangeEncoder, RangeDecoder
from .tile import split_into_tiles, assemble_from_tiles, Tile
from .container import PyraContainer
from .compress import (pack_residuals, unpack_residuals, rle_encode, rle_decode,
                       lz_encode, lz_decode, TOKEN_EOF, TOKEN_MATCH, TOKEN_SYMBOLS)

# Residual-space modular alphabet. For RGB/RGBA the reversible YCoCg
# transform produces exact-integer values that fall outside [0, 2^bitdepth);
# every transformed channel (incl. passthrough alpha) is canonicalized with
# a uniform offset into [0, mod) where mod = 1 << (bit_depth + 3).
# Safety bound: |exact value| <= 2^(d+1) + 2^(d-2) - 3 < 2^(d+2) = mod/2,
# so canonical values always lie in [0, mod) and (res + pred) % mod is exact.
_GRAY_MOD_BITS = 0      # mod = 1 << (bit_depth + _GRAY_MOD_BITS)
_RGB_MOD_BITS = 3       # mod = 1 << (bit_depth + _RGB_MOD_BITS)

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
    def _residual_mod(color_space: int, bit_depth: int) -> int:
        """Alphabet width of the residual/modular domain."""
        extra = _RGB_MOD_BITS if color_space in (CS_RGB, CS_RGBA) else _GRAY_MOD_BITS
        return 1 << (bit_depth + extra)

    @staticmethod
    def _canonical_offset(color_space: int, bit_depth: int) -> int:
        """Uniform offset added to transformed values before prediction."""
        if color_space in (CS_RGB, CS_RGBA):
            return PyraCodec._residual_mod(color_space, bit_depth) >> 1
        return 0


    @staticmethod
    def _residual_stream(tile: Tile, color_space: int, pred_type: int, bit_depth: int) -> list[int]:
        """Shared encoder front half: color transform -> canonicalization ->
        predictor residuals for one tile."""
        channels = PyraCodec._channels_for_colorspace(color_space)
        mod = PyraCodec._residual_mod(color_space, bit_depth)
        offset = PyraCodec._canonical_offset(color_space, bit_depth)

        # 1. Color transform (reversible YCoCg for RGB/RGBA)
        transformed = transform_pixels_forward(tile.data, color_space)

        # 1b. Canonicalize transformed values into [0, mod) so that both
        # sides predict on identical values and (res+pred) % mod is exact.
        if offset:
            transformed = [v + offset for v in transformed]

        # 2. Predictor pass -> residuals
        residuals = []
        prev_row = None
        for y in range(tile.height):
            row_start = y * tile.width * channels
            row_end = row_start + tile.width * channels
            row = transformed[row_start:row_end]
            res_row = apply_predictor_row(row, prev_row, pred_type, channels, mod)
            residuals.extend(res_row)
            prev_row = row
        return residuals

    @staticmethod
    def _encode_raw(residuals: list[int], mod: int) -> bytes:
        """METHOD_RAW: residual symbols straight into the entropy coder."""
        coder = RangeEncoder(mod)
        for r in residuals:
            coder.encode_symbol(r)
        return coder.flush()

    @staticmethod
    def _encode_rle(rle_bytes: bytes) -> bytes:
        """METHOD_RLE: RLE byte stream as literal tokens + EOF terminator."""
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
        coder = RangeEncoder(TOKEN_SYMBOLS)
        for shift in (24, 16, 8, 0):
            coder.encode_symbol((len(rle_bytes) >> shift) & 0xFF)
        for t in lz_encode(rle_bytes):
            coder.encode_symbol(t)
        return coder.flush()

    @classmethod
    def _tile_candidates(cls, tile: Tile, color_space: int, pred_type: int, bit_depth: int) -> dict[int, bytes]:
        """Encode one tile with all three method stages; residual stream and
        RLE base are computed exactly once."""
        residuals = cls._residual_stream(tile, color_space, pred_type, bit_depth)
        mod = cls._residual_mod(color_space, bit_depth)
        packed = pack_residuals(residuals, mod)
        rle = rle_encode(packed)
        return {
            METHOD_RAW: cls._encode_raw(residuals, mod),
            METHOD_RLE: cls._encode_rle(rle),
            METHOD_LZ: cls._encode_lz(rle),
        }

    @staticmethod
    def _decode_residuals(data: bytes, method: int, count: int, mod: int) -> list[int]:
        """Residual symbols from a tile byte stream for any method, with
        strict bounded-decode guards (no unbounded memory)."""
        if method == METHOD_RAW:
            coder = RangeDecoder(data, mod)
            return [coder.decode_symbol() for _ in range(count)]

        packed_len = count * ((mod.bit_length() + 7) // 8)

        if method == METHOD_RLE:
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
            return unpack_residuals(packed, mod, count)

        if method == METHOD_LZ:
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
            return unpack_residuals(packed, mod, count)

        raise CorruptedFileError(f"unknown compression method {method}")

    @classmethod
    def encode_tile(cls, tile: Tile, color_space: int, pred_type: int, bit_depth: int, method: int = METHOD_RAW) -> bytes:
        if method not in (METHOD_RAW, METHOD_RLE, METHOD_LZ):
            raise ValueError(f"invalid compression method: {method}")
        residuals = cls._residual_stream(tile, color_space, pred_type, bit_depth)
        if method == METHOD_RAW:
            return cls._encode_raw(residuals, cls._residual_mod(color_space, bit_depth))
        packed = pack_residuals(residuals, cls._residual_mod(color_space, bit_depth))
        rle = rle_encode(packed)
        if method == METHOD_RLE:
            return cls._encode_rle(rle)
        return cls._encode_lz(rle)

    @classmethod
    def decode_tile(cls, data: bytes, width: int, height: int, color_space: int, pred_type: int, bit_depth: int, method: int = METHOD_RAW) -> list[int]:
        channels = PyraCodec._channels_for_colorspace(color_space)
        mod = PyraCodec._residual_mod(color_space, bit_depth)
        offset = PyraCodec._canonical_offset(color_space, bit_depth)

        residuals = cls._decode_residuals(data, method, width * height * channels, mod)

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
        pixels = transform_pixels_inverse(transformed, color_space)
        return pixels

    @classmethod
    def encode(cls, pixels: list[int], width: int, height: int, bit_depth: int = 8, color_space: int = CS_RGB, pred_type: int = PRED_PAETH, tile_size: int = 256, method: int = -1) -> bytes:
        channels = cls._channels_for_colorspace(color_space)
        if len(pixels) != width * height * channels:
            raise ValueError(f"Pixel count ({len(pixels)}) != {width}x{height}x{channels}")

        tiles = split_into_tiles(pixels, width, height, channels, tile_size)
        if method == METHOD_MIX:
            # Per-tile best method: each tile body starts with a 1-byte
            # method tag, decoded independently (mixed-content images win).
            compressed_tiles = []
            for t in tiles:
                cand = cls._tile_candidates(t, color_space, pred_type, bit_depth)
                m = min((len(d), mid) for mid, d in cand.items())[1]
                compressed_tiles.append(bytes([m]) + cand[m])
        elif method in (METHOD_RAW, METHOD_RLE, METHOD_LZ):
            # Forced method: single rescue-free encode per tile.
            compressed_tiles = [cls.encode_tile(t, color_space, pred_type, bit_depth, method)
                                for t in tiles]
        elif method == -1:
            # Auto: per-tile candidates for every method; pick the single
            # method that minimizes the total compressed size (deterministic
            # tie-break: lower method id wins).
            candidates = [cls._tile_candidates(t, color_space, pred_type, bit_depth)
                          for t in tiles]
            totals = [0] * METHOD_COUNT
            for cand in candidates:
                for m, data in cand.items():
                    totals[m] += len(data)
            method = min(range(METHOD_COUNT), key=lambda m: (totals[m], m))
            compressed_tiles = [cand[method] for cand in candidates]
        else:
            raise ValueError(f"invalid method: {method} (use -1 for auto)")
        return PyraContainer.pack_file(width, height, bit_depth, color_space, pred_type, method, tile_size, compressed_tiles)

    @classmethod
    def decode(cls, file_bytes: bytes) -> tuple[dict, list[int]]:
        meta, compressed_tiles = PyraContainer.unpack_file(file_bytes)
        width = meta["width"]
        height = meta["height"]
        bit_depth = meta["bit_depth"]
        color_space = meta["color_space"]
        pred_type = meta["pred_type"]
        method = meta["method"]
        tile_size = meta["tile_size"]

        channels = cls._channels_for_colorspace(color_space)
        tiles = []

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
                tile_pixels = cls.decode_tile(tile_bytes, tw, th, color_space, pred_type, bit_depth, tile_method)
                tiles.append(Tile(tx, ty, tw, th, tile_pixels))
                tile_idx += 1

        pixels = assemble_from_tiles(tiles, width, height, channels)
        return meta, pixels
