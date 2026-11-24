# LUMEN native core loader — optional Rust-backed hot paths (stdlib only).
#
# `scripts/build_native.sh` (rustc, std-only cdylib) produces
# `_liblum_core.so` next to this module. Every wrapper here returns None
# when the backend is unavailable OR disabled, and the caller in compress.py
# falls back to the byte-identical pure-Python reference — so results are
# bit-exact either way and the codec works without Rust installed.
#
# Runtime dispatch flags (checked at call time, not import time):
#   LUMEN_DISABLE_NATIVE=1   force the pure-Python path
#   LUMEN_NATIVE=0           force the pure-Python path
#
# Rust error convention: every fn returns isize; < 0 is an error code that
# is mapped here to the same exception/message the Python reference raises.

import ctypes
import os
from array import array as _array

from .constants import (CorruptedFileError, LumenError, METHOD_RAW, METHOD_RLE,
                       METHOD_LZ)

# LZ/RLE token alphabet size (0..255 literal, 256 MATCH, 257 EOF).
# Duplicated here to avoid a circular import with compress.py.
_TOKEN_SYMBOLS = 258


def _pack_width(mod: int, legacy: bool = False) -> int:
    """Bytes per packed residual symbol (see compress.pack_width)."""
    if mod < 1:
        return 1
    if legacy:
        return (mod.bit_length() + 7) // 8
    return (((mod - 1).bit_length()) + 7) // 8 or 1


def _u32_ptr(values) -> tuple:
    """Fast u32 array: array.array("I") beats a starred ctypes tuple build."""
    a = _array("I", values)
    ptr = ctypes.cast(a.buffer_info()[0], ctypes.POINTER(ctypes.c_uint32))
    return a, ptr


def _i32_ptr(values) -> tuple:
    """Signed i32 array for row/pixel kernels (residuals and pixel values)."""
    a = _array("i", values)
    ptr = ctypes.cast(a.buffer_info()[0], ctypes.POINTER(ctypes.c_int32))
    return a, ptr


def _view(buf) -> ctypes.Array:
    """ctypes view over an array('i') buffer, sharing its memory.

    No copy is made: the native core writes straight into the caller's
    buffer, which removes the per-tile list<->array conversions that used
    to dominate the container codec. The returned view keeps the buffer
    alive (and locked against resizing) for as long as it exists."""
    n = len(buf)
    if n == 0 or buf.itemsize != 4:
        return None
    return (ctypes.c_int32 * n).from_buffer(buf)


def _u32_view(buf) -> ctypes.Array:
    """Unsigned view over an array('i') buffer (residuals are < 2^31)."""
    n = len(buf)
    if n == 0 or buf.itemsize != 4:
        return None
    return (ctypes.c_uint32 * n).from_buffer(buf)

# --- Rust error codes (must match rust/lum_core.rs) ---
_E_RLE_TRUNC = -1
_E_RLE_ESCAPE_TRUNC = -2
_E_RLE_OVERSHOOT = -3
_E_RLE_LEN0 = -4
_E_RLE_MISMATCH = -5
_E_PACK_RANGE = -100
_E_UNPACK_TRUNC = -110
_E_UNPACK_RANGE = -111
_E_LZ_NO_EOF = -20
_E_LZ_SYM_CAP = -21
_E_LZ_LIT_OVER = -22
_E_LZ_BAD_SYM = -23
_E_LZ_MATCH_TRUNC = -24
_E_LZ_BAD_EXTRA = -25
_E_LZ_DIST = -26
_E_LZ_MATCH_OVER = -27
_E_LZ_MISMATCH = -28

_RLE_MESSAGES = {
    _E_RLE_TRUNC: "truncated RLE stream",
    _E_RLE_ESCAPE_TRUNC: "truncated RLE escape",
    _E_RLE_OVERSHOOT: "RLE run overshoots target length",
    _E_RLE_LEN0: "RLE run length 0",
    _E_RLE_MISMATCH: "RLE output length mismatch",
}

_LZ_MESSAGES = {
    _E_LZ_NO_EOF: "truncated LZ token stream",
    _E_LZ_SYM_CAP: "LZ token stream exceeds target length",
    _E_LZ_LIT_OVER: "LZ literal overshoots target length",
    _E_LZ_MATCH_TRUNC: "truncated LZ match tokens",
    _E_LZ_BAD_EXTRA: "malformed LZ match extra symbol",
    _E_LZ_MATCH_OVER: "LZ match overshoots target length",
    _E_LZ_MISMATCH: "LZ output length mismatch",
}

_lib = None
_load_attempted = False


def _load():
    """One-shot load of the cdylib; caches result (None = not built)."""
    global _lib, _load_attempted
    if not _load_attempted:
        _load_attempted = True
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_liblum_core.so")
        try:
            lib = ctypes.CDLL(path)
            _isize = ctypes.c_ssize_t
            _sz = ctypes.c_size_t
            _u32 = ctypes.c_uint32
            _u64 = ctypes.c_uint64
            _i64 = ctypes.c_int64
            lib.lum_pack_residuals.restype = _isize
            lib.lum_pack_residuals.argtypes = [
                ctypes.POINTER(_u32), _sz, _sz, _u64,
                ctypes.POINTER(ctypes.c_ubyte)]
            lib.lum_unpack_residuals.restype = _isize
            lib.lum_unpack_residuals.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz, _sz, _sz, _u64,
                ctypes.POINTER(_u32)]
            lib.lum_rle_encode.restype = _isize
            lib.lum_rle_encode.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz,
                ctypes.POINTER(ctypes.c_ubyte)]
            lib.lum_rle_decode.restype = _isize
            lib.lum_rle_decode.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz, _sz,
                ctypes.POINTER(ctypes.c_ubyte)]
            lib.lum_lz_encode.restype = _isize
            lib.lum_lz_encode.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz, ctypes.POINTER(_u32)]
            lib.lum_lz_decode_tokens.restype = _isize
            lib.lum_lz_decode_tokens.argtypes = [
                ctypes.POINTER(_u32), _sz, _sz,
                ctypes.POINTER(ctypes.c_ubyte)]
            _i32 = ctypes.c_int32
            lib.lum_apply_predictor_row.restype = _isize
            lib.lum_apply_predictor_row.argtypes = [
                ctypes.POINTER(_i32), _sz, ctypes.POINTER(_i32), _sz,
                _i32, _sz, _i64, ctypes.POINTER(_i32)]
            lib.lum_reconstruct_row.restype = _isize
            lib.lum_reconstruct_row.argtypes = [
                ctypes.POINTER(_i32), _sz, ctypes.POINTER(_i32), _sz,
                _i32, _sz, _i64, ctypes.POINTER(_i32)]
            lib.lum_transform_forward.restype = _isize
            lib.lum_transform_forward.argtypes = [
                ctypes.POINTER(_i32), _sz, _i32, ctypes.POINTER(_i32)]
            lib.lum_transform_inverse.restype = _isize
            lib.lum_transform_inverse.argtypes = [
                ctypes.POINTER(_i32), _sz, _i32, ctypes.POINTER(_i32)]
            lib.lum_pipe_forward.restype = _isize
            lib.lum_pipe_forward.argtypes = [
                ctypes.POINTER(_i32), _sz, _sz, _i32, _i64, _i64,
                _i32, _sz, ctypes.POINTER(_i32), _i32]
            lib.lum_pipe_reconstruct.restype = _isize
            lib.lum_pipe_reconstruct.argtypes = [
                ctypes.POINTER(_i32), _sz, _sz, _i32, _i64, _i64,
                _i32, _sz, ctypes.POINTER(_i32), _i32]
            lib.lum_range_encode.restype = _isize
            lib.lum_range_encode.argtypes = [
                ctypes.POINTER(_u32), _sz, _sz,
                ctypes.POINTER(ctypes.c_ubyte), _sz]
            lib.lum_range_decode.restype = _isize
            lib.lum_range_decode.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz, _sz, _sz,
                ctypes.POINTER(_u32)]
            lib.lum_decode_rle_method.restype = _isize
            lib.lum_decode_rle_method.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz, _sz, _sz, _sz, _u64,
                _sz, ctypes.POINTER(_u32), ctypes.POINTER(_i32)]
            lib.lum_decode_lz_method.restype = _isize
            lib.lum_decode_lz_method.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz, _sz, _sz, _sz, _sz, _u64,
                _sz, ctypes.POINTER(_u32), ctypes.POINTER(_u32),
                ctypes.POINTER(_i32)]
            lib.lum_extract_tile.restype = _isize
            lib.lum_extract_tile.argtypes = [
                ctypes.POINTER(_i32), _sz, _sz, _sz, _sz, _sz, _sz,
                ctypes.POINTER(_i32)]
            lib.lum_pipe_reconstruct_into.restype = _isize
            lib.lum_pipe_reconstruct_into.argtypes = [
                ctypes.POINTER(_i32), _sz, _sz, _i32, _i64, _i64,
                _i32, _sz, ctypes.POINTER(_i32), _sz, _i32]
            lib.lum_encode_raw_method.restype = _isize
            lib.lum_encode_raw_method.argtypes = [
                ctypes.POINTER(_i32), _sz, _u64,
                ctypes.POINTER(ctypes.c_ubyte), _sz]
            lib.lum_encode_rle_method.restype = _isize
            lib.lum_encode_rle_method.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz,
                ctypes.POINTER(ctypes.c_ubyte), _sz]
            lib.lum_encode_lz_method.restype = _isize
            lib.lum_encode_lz_method.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz,
                ctypes.POINTER(ctypes.c_ubyte), _sz]
            lib.lum_plan.restype = _isize
            lib.lum_plan.argtypes = [
                ctypes.POINTER(_i32), _sz, _sz, _sz, _sz, ctypes.c_uint32,
                _i32, _sz, _i32, _i32, _i32, ctypes.POINTER(_i32)]
            lib.lum_pipe_forward_adaptive_into.restype = _isize
            lib.lum_pipe_forward_adaptive_into.argtypes = [
                ctypes.POINTER(_i32), _sz, _sz, _i32, _i64, _i64,
                _sz, _i32, ctypes.POINTER(_i32), ctypes.POINTER(ctypes.c_ubyte)]
            lib.lum_pipe_reconstruct_adaptive_into.restype = _isize
            lib.lum_pipe_reconstruct_adaptive_into.argtypes = [
                ctypes.POINTER(_i32), _sz, _sz, _i32, _i64, _i64,
                _sz, _i32, ctypes.POINTER(ctypes.c_ubyte), _sz,
                ctypes.POINTER(_i32), _sz]
            lib.lum_encode_image.restype = _isize
            lib.lum_encode_image.argtypes = [
                ctypes.POINTER(_i32), _sz, _sz, _sz, _sz, ctypes.c_uint32,
                _i32, _i32, _i32, _sz, _i32, _i32, _sz,
                ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
                ctypes.POINTER(_sz)]
            lib.lum_decode_image.restype = _isize
            lib.lum_decode_image.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz,
                ctypes.POINTER(ctypes.POINTER(_i32)), ctypes.POINTER(_sz),
                ctypes.POINTER(_i32)]
            lib.lum_encode_image_bytes.restype = _isize
            lib.lum_encode_image_bytes.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz, _sz, _sz, _sz,
                ctypes.c_uint32, _i32, _i32, _i32, _sz, _i32, _i32, _sz,
                ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
                ctypes.POINTER(_sz)]
            lib.lum_free_buf.restype = None
            lib.lum_free_buf.argtypes = [ctypes.POINTER(ctypes.c_ubyte), _sz]
            lib.lum_free_buf_i32.restype = None
            lib.lum_free_buf_i32.argtypes = [ctypes.POINTER(_i32), _sz]
            _lib = lib
        except OSError:
            _lib = None
    return _lib


def enabled() -> bool:
    """Runtime dispatch flag (read at call time so tests can toggle it)."""
    if os.environ.get("LUMEN_DISABLE_NATIVE"):
        return False
    if os.environ.get("LUMEN_NATIVE") == "0":
        return False
    return True


def available() -> bool:
    """True when the native backend is built AND enabled."""
    return _load() is not None and enabled()


def build_path() -> str | None:
    return (os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "_liblum_core.so")
            if _load() is not None else None)


# ------------------------------------------------------------ wrapped calls
# Each returns None when the backend must not be used; otherwise raises the
# reference exception on any Rust error code, or returns the result.

def pack_residuals(residuals: list[int], mod: int,
                   legacy: bool = False) -> bytes | None:
    lib = _load()
    if lib is None or not enabled():
        return None
    width = _pack_width(mod, legacy)
    n = len(residuals)
    a, syms = _u32_ptr(residuals)
    out = (ctypes.c_ubyte * (n * width))()
    rc = lib.lum_pack_residuals(syms, n, width, mod, out)
    if rc < 0:
        # -100 - idx: first out-of-range symbol
        raise ValueError(f"residual symbol out of range at index {-rc - _E_PACK_RANGE}")
    return bytes(memoryview(out))


def unpack_residuals(data: bytes, mod: int, count: int,
                     legacy: bool = False) -> list[int] | None:
    lib = _load()
    if lib is None or not enabled():
        return None
    width = _pack_width(mod, legacy)
    if len(data) < count * width:
        raise CorruptedFileError("truncated packed residual stream")
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    out = (ctypes.c_uint32 * count)()
    rc = lib.lum_unpack_residuals(buf, len(data), count, width, mod, out)
    if rc < 0:
        if rc == _E_UNPACK_TRUNC:
            raise CorruptedFileError("truncated packed residual stream")
        # -111 - idx
        raise CorruptedFileError(
            f"residual symbol out of range at index {-rc - _E_UNPACK_RANGE}")
    return list(out)


def rle_encode(data: bytes) -> bytes | None:
    lib = _load()
    if lib is None or not enabled():
        return None
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data or b"\x00")
    out = (ctypes.c_ubyte * (2 * len(data) + 16))()
    rc = lib.lum_rle_encode(buf, len(data), out)
    if rc < 0:
        raise CorruptedFileError("native RLE encoder failure")
    return bytes(memoryview(out)[:rc])


def rle_decode(data: bytes, target_len: int) -> bytes | None:
    lib = _load()
    if lib is None or not enabled():
        return None
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data or b"\x00")
    out = (ctypes.c_ubyte * target_len)()
    rc = lib.lum_rle_decode(buf, len(data), target_len, out)
    if rc < 0:
        raise CorruptedFileError(_RLE_MESSAGES.get(rc, "corrupted RLE stream"))
    return bytes(memoryview(out))


def lz_encode(data: bytes) -> list[int] | None:
    lib = _load()
    if lib is None or not enabled():
        return None
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data or b"\x00")
    out = (ctypes.c_uint32 * (len(data) + 2))()
    rc = lib.lum_lz_encode(buf, len(data), out)
    if rc < 0:
        raise CorruptedFileError("native LZ encoder failure")
    return list(out[:rc])


def lz_decode_tokens(tokens: list[int], target_len: int) -> bytes | None:
    """Batch-mode counterpart of compress.lz_decode (the streaming one stays
    pure-Python because it pulls symbols lazily from the range decoder).
    Exposed for tests + direct users of the token representation."""
    lib = _load()
    if lib is None or not enabled():
        return None
    n = len(tokens)
    a, buf = _u32_ptr(tokens)
    out = (ctypes.c_ubyte * target_len)()
    rc = lib.lum_lz_decode_tokens(buf, n, target_len, out)
    if rc < 0:
        if rc in _LZ_MESSAGES:
            raise CorruptedFileError(_LZ_MESSAGES[rc])
        if rc == _E_LZ_BAD_SYM:
            raise CorruptedFileError("invalid LZ token symbol")
        if rc == _E_LZ_DIST:
            raise CorruptedFileError("invalid LZ back-reference distance")
        raise CorruptedFileError("corrupted LZ token stream")
    return bytes(memoryview(out))


def apply_predictor_row(row: list[int], prev_row: list[int] | None,
                        pred_type: int, channels: int, mod: int) -> list[int] | None:
    """Native counterpart of predictors.apply_predictor_row."""
    lib = _load()
    if lib is None or not enabled():
        return None
    n = len(row)
    a, buf = _i32_ptr(row)
    if prev_row is not None and len(prev_row) >= n:
        pa, prev = _i32_ptr(prev_row[:n]) if len(prev_row) != n else _i32_ptr(prev_row)
        prow, plen = prev, n
    else:
        pa, prow, plen = None, None, 0
    out = (ctypes.c_int32 * n)()
    rc = lib.lum_apply_predictor_row(buf, n, prow, plen, pred_type, channels, mod, out)
    if rc < 0:
        raise ValueError("native predictor failure (invalid channels)")
    return list(out)


def reconstruct_row(residuals: list[int], prev_row: list[int] | None,
                    pred_type: int, channels: int, mod: int) -> list[int] | None:
    """Native counterpart of predictors.reconstruct_row."""
    lib = _load()
    if lib is None or not enabled():
        return None
    n = len(residuals)
    a, buf = _i32_ptr(residuals)
    if prev_row is not None and len(prev_row) >= n:
        pa, prev = _i32_ptr(prev_row[:n]) if len(prev_row) != n else _i32_ptr(prev_row)
        prow, plen = prev, n
    else:
        pa, prow, plen = None, None, 0
    out = (ctypes.c_int32 * n)()
    rc = lib.lum_reconstruct_row(buf, n, prow, plen, pred_type, channels, mod, out)
    if rc < 0:
        raise ValueError("native predictor failure (invalid channels)")
    return list(out)


def transform_forward(pixels: list[int], color_space: int) -> list[int] | None:
    """Native counterpart of color.transform_pixels_forward."""
    lib = _load()
    if lib is None or not enabled():
        return None
    n = len(pixels)
    a, buf = _i32_ptr(pixels)
    out = (ctypes.c_int32 * n)()
    rc = lib.lum_transform_forward(buf, n, color_space, out)
    if rc < 0:
        raise ValueError("native YCoCg transform failure")
    return list(out)


def transform_inverse(pixels: list[int], color_space: int) -> list[int] | None:
    """Native counterpart of color.transform_pixels_inverse."""
    lib = _load()
    if lib is None or not enabled():
        return None
    n = len(pixels)
    a, buf = _i32_ptr(pixels)
    out = (ctypes.c_int32 * n)()
    rc = lib.lum_transform_inverse(buf, n, color_space, out)
    if rc < 0:
        raise ValueError("native YCoCg transform failure")
    return list(out)


# ------------------------------------------------------- fused pipelines
# Single-conversion counterparts of LumenCodec._residual_stream (color
# transform + canonicalization + row prediction) and the decode-side tail
# (row reconstruction + descaling + inverse transform). Returns None on
# shape mismatch so the caller falls back to the reference composition.

def pipe_residuals(data: list[int], stride: int, offset: int, mod: int,
                   pred_type: int, channels: int,
                   color_space: int, transform: int = 0) -> list[int] | None:
    lib = _load()
    if lib is None or not enabled():
        return None
    n = len(data)
    if n % stride:
        return None
    try:
        a, buf = _i32_ptr(data)
    except (OverflowError, TypeError):
        return None
    out = (ctypes.c_int32 * n)()
    rc = lib.lum_pipe_forward(buf, n, stride, color_space, offset, mod,
                               pred_type, channels, out, transform)
    if rc < 0:
        if rc == _E_PIPE_SHAPE:
            return None
        raise ValueError(f"native pipeline failure ({rc})")
    return list(out)


def pipe_reconstruct(residuals: list[int], stride: int, offset: int, mod: int,
                     pred_type: int, channels: int,
                     color_space: int, transform: int = 0) -> list[int] | None:
    lib = _load()
    if lib is None or not enabled():
        return None
    n = len(residuals)
    if n % stride:
        return None
    try:
        a, buf = _i32_ptr(residuals)
    except (OverflowError, TypeError):
        return None
    out = (ctypes.c_int32 * n)()
    rc = lib.lum_pipe_reconstruct(buf, n, stride, color_space, offset, mod,
                                   pred_type, channels, out, transform)
    if rc < 0:
        if rc == _E_PIPE_SHAPE:
            return None
        raise ValueError(f"native pipeline failure ({rc})")
    return list(out)


# ----------------------------------------------------------- range coder
# Adaptive arithmetic coder (Fenwick model + WNC renormalization).
# The Rust side is a line-for-line port of entropy.py, so the encoded bit
# stream is byte-identical to RangeEncoder.flush().

def range_encode(symbols: list[int], n_symbols: int) -> bytes | None:
    """Encode a symbol list with the adaptive range coder.

    Returns None when the backend is unavailable/disabled (caller falls back
    to the pure-Python RangeEncoder) or when the native side refuses the
    input (defensive: symbol outside the alphabet)."""
    lib = _load()
    if lib is None or not enabled():
        return None
    if n_symbols < 1:
        raise LumenError("model needs at least 1 symbol")
    n = len(symbols)
    a, buf = _u32_ptr(symbols)
    # Worst-case output size: the range coder emits at most
    # ceil(log2(model_total)) + 1 bits per symbol, and the adaptive model
    # total is capped at max(2^15, 2 * n_symbols).
    bits_per = max(15, n_symbols.bit_length() + 1) + 1
    cap = (n * bits_per) // 8 + 64
    out = (ctypes.c_ubyte * cap)()
    rc = lib.lum_range_encode(buf, n, n_symbols, out, cap)
    if rc < 0:
        return None
    return bytes(memoryview(out)[:rc])


def range_decode(data: bytes, n_symbols: int, count: int) -> list[int] | None:
    """Decode exactly `count` symbols. Returns None when unavailable."""
    lib = _load()
    if lib is None or not enabled():
        return None
    if n_symbols < 1:
        raise LumenError("model needs at least 1 symbol")
    buf = (ctypes.c_ubyte * max(len(data), 1)).from_buffer_copy(data or b"\x00")
    out = (ctypes.c_uint32 * count)()
    rc = lib.lum_range_decode(buf, len(data), n_symbols, count, out)
    if rc < 0:
        return None
    return list(out)


# ------------------------------------------------- fused method decoders
# One native call per tile stage: range decode -> RLE/LZ -> residual
# unpacking. On ANY error the wrapper returns None so the caller re-runs
# the pure-Python reference and raises the exact reference exception
# (bit-exact error semantics in both modes).

def decode_rle_method(data: bytes, count: int, mod: int,
                      legacy: bool = False) -> list[int] | None:
    """METHOD_RLE residuals in one native call (None => use the reference)."""
    lib = _load()
    if lib is None or not enabled():
        return None
    width = _pack_width(mod, legacy)
    packed_len = count * width
    buf = (ctypes.c_ubyte * max(len(data), 1)).from_buffer_copy(data or b"\x00")
    out = (ctypes.c_uint32 * count)()
    detail = ctypes.c_int32(0)
    rc = lib.lum_decode_rle_method(buf, len(data), _TOKEN_SYMBOLS,
                                    packed_len, width, ctypes.c_uint64(mod),
                                    count, out, ctypes.byref(detail))
    if rc < 0:
        return None
    return list(out)


def decode_lz_method(data: bytes, count: int, mod: int,
                     legacy: bool = False) -> list[int] | None:
    """METHOD_LZ residuals in one native call (None => use the reference)."""
    lib = _load()
    if lib is None or not enabled():
        return None
    width = _pack_width(mod, legacy)
    packed_len = count * width
    max_rle_len = 2 * packed_len + 64
    buf = (ctypes.c_ubyte * max(len(data), 1)).from_buffer_copy(data or b"\x00")
    out = (ctypes.c_uint32 * count)()
    rle_len = ctypes.c_uint32(0)
    detail = ctypes.c_int32(0)
    rc = lib.lum_decode_lz_method(buf, len(data), _TOKEN_SYMBOLS,
                                   max_rle_len, packed_len, width,
                                   ctypes.c_uint64(mod), count, out,
                                   ctypes.byref(rle_len), ctypes.byref(detail))
    if rc < 0:
        return None
    return list(out)


# ------------------------------------------------- zero-copy buffer API
# The container codec keeps its pixel/residual data in array('i') buffers
# and lets the native core read and write them in place. The wrappers below
# mirror the per-tile pipeline exactly, so the produced bytes and pixels are
# identical to the list-based path; they only avoid the intermediate Python
# lists (converting 200k residuals to a list cost ~30 ms per tile).
#
# Every wrapper returns None (or False) when the backend is unavailable or
# refuses the input, and the caller then runs the pure-Python reference.

def extract_tile(src_buf, src_stride: int, x: int, y: int, tw: int, th: int,
                 channels: int, dst_buf) -> bool:
    """Copy the tile at (x, y) out of the canvas buffer into dst_buf."""
    lib = _load()
    if lib is None or not enabled():
        return False
    src_view = _view(src_buf)
    dst_view = _view(dst_buf)
    if src_view is None or dst_view is None:
        return False
    rc = lib.lum_extract_tile(src_view, src_stride, x, y, tw, th, channels,
                                dst_view)
    return rc >= 0


def pipe_reconstruct_into(res_buf, stride: int, offset: int, mod: int,
                          pred_type: int, channels: int, color_space: int,
                          dst_buf, dst_offset: int, dst_stride: int,
                          transform: int = 0) -> bool:
    """Reconstruct a tile straight into the canvas buffer at dst_offset."""
    lib = _load()
    if lib is None or not enabled():
        return False
    res_view = _view(res_buf)
    dst_view = _view(dst_buf)
    if res_view is None or dst_view is None:
        return False
    n = len(res_buf)
    if n % stride:
        return False
    dst_ptr = ctypes.cast(ctypes.addressof(dst_view) + dst_offset * 4,
                          ctypes.POINTER(ctypes.c_int32))
    rc = lib.lum_pipe_reconstruct_into(
        res_view, n, stride, color_space, offset, mod, pred_type, channels,
        dst_ptr, dst_stride, transform)
    return rc >= 0


def decode_rle_method_into(data: bytes, count: int, mod: int,
                           out_buf, legacy: bool = False) -> bool:
    """METHOD_RLE residuals written directly into out_buf."""
    lib = _load()
    if lib is None or not enabled():
        return False
    width = _pack_width(mod, legacy)
    packed_len = count * width
    buf = (ctypes.c_ubyte * max(len(data), 1)).from_buffer_copy(data or b"\x00")
    out = _u32_view(out_buf)
    if out is None:
        return False
    detail = ctypes.c_int32(0)
    rc = lib.lum_decode_rle_method(buf, len(data), _TOKEN_SYMBOLS,
                                    packed_len, width, ctypes.c_uint64(mod),
                                    count, out, ctypes.byref(detail))
    return rc >= 0


def decode_lz_method_into(data: bytes, count: int, mod: int,
                          out_buf, legacy: bool = False) -> bool:
    """METHOD_LZ residuals written directly into out_buf."""
    lib = _load()
    if lib is None or not enabled():
        return False
    width = _pack_width(mod, legacy)
    packed_len = count * width
    max_rle_len = 2 * packed_len + 64
    buf = (ctypes.c_ubyte * max(len(data), 1)).from_buffer_copy(data or b"\x00")
    out = _u32_view(out_buf)
    if out is None:
        return False
    rle_len = ctypes.c_uint32(0)
    detail = ctypes.c_int32(0)
    rc = lib.lum_decode_lz_method(buf, len(data), _TOKEN_SYMBOLS,
                                   max_rle_len, packed_len, width,
                                   ctypes.c_uint64(mod), count, out,
                                   ctypes.byref(rle_len), ctypes.byref(detail))
    return rc >= 0


def pack_residuals_buf(res_buf, mod: int) -> bytes | None:
    """Pack residual symbols held in an array('i') buffer."""
    lib = _load()
    if lib is None or not enabled():
        return None
    count = len(res_buf)
    width = _pack_width(mod)
    syms = _u32_view(res_buf)
    if syms is None:
        return b""
    out = (ctypes.c_ubyte * max(count * width, 1))()
    rc = lib.lum_pack_residuals(syms, count, width, ctypes.c_uint64(mod), out)
    if rc < 0:
        return None
    return bytes(memoryview(out)[:count * width])


def encode_raw_method(res_buf, mod: int) -> bytes | None:
    """METHOD_RAW: residual symbols from a buffer, range coded natively."""
    lib = _load()
    if lib is None or not enabled():
        return None
    count = len(res_buf)
    syms = _view(res_buf)
    if syms is None:
        return b""
    cap = _enc_cap(count, mod)
    out = (ctypes.c_ubyte * cap)()
    rc = lib.lum_encode_raw_method(syms, count, ctypes.c_uint64(mod), out, cap)
    if rc < 0:
        return None
    return bytes(memoryview(out)[:rc])


def encode_rle_method(rle_bytes: bytes) -> bytes | None:
    """METHOD_RLE: RLE byte stream as literal tokens + EOF, range coded."""
    lib = _load()
    if lib is None or not enabled():
        return None
    n = len(rle_bytes)
    src = (ctypes.c_ubyte * max(n, 1)).from_buffer_copy(rle_bytes or b"\x00")
    cap = _enc_cap(n + 1, _TOKEN_SYMBOLS)
    out = (ctypes.c_ubyte * cap)()
    rc = lib.lum_encode_rle_method(src, n, out, cap)
    if rc < 0:
        return None
    return bytes(memoryview(out)[:rc])


def encode_lz_method(rle_bytes: bytes) -> bytes | None:
    """METHOD_LZ: length header + LZ77 tokens, range coded in one pass."""
    lib = _load()
    if lib is None or not enabled():
        return None
    n = len(rle_bytes)
    src = (ctypes.c_ubyte * max(n, 1)).from_buffer_copy(rle_bytes or b"\x00")
    # worst case: every byte is a literal (n tokens) plus 4 header bytes
    cap = _enc_cap(n + 5, _TOKEN_SYMBOLS)
    out = (ctypes.c_ubyte * cap)()
    rc = lib.lum_encode_lz_method(src, n, out, cap)
    if rc < 0:
        return None
    return bytes(memoryview(out)[:rc])


def _enc_cap(count: int, n_symbols: int) -> int:
    """Worst-case encoded size: ceil(log2(model_total)) + 1 bits/symbol.

    The adaptive model total is capped at max(2^15, 2 * n_symbols)."""
    bits_per = max(15, n_symbols.bit_length() + 1) + 1
    return (count * bits_per) // 8 + 64


def pipe_forward_into(src_buf, stride: int, offset: int, mod: int,
                      pred_type: int, channels: int, color_space: int,
                      out_buf, transform: int = 0) -> bool:
    """Color transform + canonicalization + prediction, buffer in / out."""
    lib = _load()
    if lib is None or not enabled():
        return False
    src_view = _view(src_buf)
    out_view = _view(out_buf)
    if src_view is None or out_view is None:
        return False
    n = len(src_buf)
    if n != len(out_buf) or n % stride:
        return False
    rc = lib.lum_pipe_forward(src_view, n, stride, color_space, offset, mod,
                               pred_type, channels, out_view, transform)
    return rc >= 0


def pipe_forward_adaptive_into(src_buf, stride: int, offset: int, mod: int,
                                channels: int, color_space: int, res_buf,
                                bits_buf, transform: int = 0) -> bool:
    """Adaptive front half: transform + per-row predictor selection."""
    lib = _load()
    if lib is None or not enabled():
        return False
    src_view = _view(src_buf)
    out_view = _view(res_buf)
    if src_view is None or out_view is None:
        return False
    n = len(src_buf)
    if n != len(res_buf) or n % stride:
        return False
    bits = (ctypes.c_ubyte * max(len(bits_buf), 1)).from_buffer(bits_buf)
    rc = lib.lum_pipe_forward_adaptive_into(
        src_view, n, stride, color_space, offset, mod, channels, transform,
        out_view, bits)
    return rc >= 0


def pipe_reconstruct_adaptive_into(res_buf, stride: int, offset: int,
                                   mod: int, channels: int, color_space: int,
                                   filter_bits: bytes, dst_buf, dst_offset: int,
                                   dst_stride: int, transform: int = 0) -> bool:
    """Adaptive back half: rebuild rows into the canvas."""
    lib = _load()
    if lib is None or not enabled():
        return False
    res_view = _view(res_buf)
    dst_view = _view(dst_buf)
    if res_view is None or dst_view is None:
        return False
    n = len(res_buf)
    if n % stride:
        return False
    bits = (ctypes.c_ubyte * max(len(filter_bits), 1)).from_buffer_copy(
        filter_bits or b"\x00")
    dst_ptr = ctypes.cast(ctypes.addressof(dst_view) + dst_offset * 4,
                          ctypes.POINTER(ctypes.c_int32))
    rc = lib.lum_pipe_reconstruct_adaptive_into(
        res_view, n, stride, color_space, offset, mod, channels, transform,
        bits, len(filter_bits), dst_ptr, dst_stride)
    return rc >= 0


def range_decode_into(data: bytes, n_symbols: int, count: int,
                      out_buf) -> bool:
    """Decode exactly `count` symbols into out_buf (METHOD_RAW)."""
    lib = _load()
    if lib is None or not enabled():
        return False
    buf = (ctypes.c_ubyte * max(len(data), 1)).from_buffer_copy(data or b"\x00")
    out = _u32_view(out_buf)
    if out is None:
        return False
    rc = lib.lum_range_decode(buf, len(data), n_symbols, count, out)
    return rc >= 0


def rle_encode_bytes(data: bytes) -> bytes | None:
    """Byte-level RLE encoding (native), used by the buffer pipeline."""
    lib = _load()
    if lib is None or not enabled():
        return None
    n = len(data)
    src = (ctypes.c_ubyte * max(n, 1)).from_buffer_copy(data or b"\x00")
    out = (ctypes.c_ubyte * (2 * n + 16))()
    rc = lib.lum_rle_encode(src, n, out)
    if rc < 0:
        return None
    return bytes(memoryview(out)[:rc])


def plan(pixels: list[int], width: int, height: int, channels: int,
         bit_depth: int, color_space: int, sample_rows: int,
         fixed_transform: int = -1, fixed_filter: int = -1,
         fixed_pred: int = -1) -> tuple[int, int, int] | None:
    """Pick (transform, filter_mode, pred_type) for an image.

    The native planner measures every candidate with the real range coder on
    a spread sample of rows, exactly like the pure-Python reference planner,
    so both backends produce the same plan (and therefore the same bytes).
    Returns None when the backend is unavailable."""
    lib = _load()
    if lib is None or not enabled():
        return None
    count = width * height * channels
    if count <= 0 or len(pixels) < count:
        return None
    try:
        px_buf = _array("i", pixels[:count])
        out_buf = _array("i", bytes(12))
    except (OverflowError, TypeError, MemoryError):
        return None
    px_ptr = ctypes.cast(px_buf.buffer_info()[0],
                         ctypes.POINTER(ctypes.c_int32))
    out_ptr = ctypes.cast(out_buf.buffer_info()[0],
                          ctypes.POINTER(ctypes.c_int32))
    try:
        rc = lib.lum_plan(px_ptr, count, width, height, channels,
                           bit_depth, color_space, sample_rows,
                           fixed_transform, fixed_filter, fixed_pred,
                           out_ptr)
    except (ctypes.ArgumentError, ValueError, OverflowError):
        return None
    if rc != 3:
        return None
    return (out_buf[0], out_buf[1], out_buf[2])


def tile_candidates_adaptive(canvas, src_stride: int, x: int, y: int, tw: int,
                             th: int, channels: int, offset: int, mod: int,
                             color_space: int,
                             methods=(METHOD_RAW, METHOD_RLE, METHOD_LZ),
                             transform: int = 0):
    """Method candidates for one tile in adaptive row-filter mode.

    Every candidate carries the leading per-row predictor table, exactly like
    the pure-Python reference does, so the method selection compares the same
    sizes. Returns (candidates, filter_bits) or None."""
    lib = _load()
    if lib is None or not enabled():
        return None
    count = tw * th * channels
    nbits = (th * 3 + 7) // 8
    try:
        tile_buf = _array("i", bytes(4 * count))
        res_buf = _array("i", bytes(4 * count))
        bits_buf = _array("B", bytes(nbits + 1))
    except (OverflowError, TypeError, MemoryError):
        return None
    if not extract_tile(canvas, src_stride, x, y, tw, th, channels, tile_buf):
        return None
    if not pipe_forward_adaptive_into(tile_buf, tw * channels, offset, mod,
                                      channels, color_space, res_buf, bits_buf,
                                      transform):
        return None
    prefix = bytes(memoryview(bits_buf)[:nbits])
    out = {}
    if METHOD_RAW in methods:
        raw = encode_raw_method(res_buf, mod)
        if raw is None:
            return None
        out[METHOD_RAW] = prefix + raw
    if METHOD_RLE in methods or METHOD_LZ in methods:
        packed = pack_residuals_buf(res_buf, mod)
        if packed is None:
            return None
        rle = rle_encode_bytes(packed)
        if rle is None:
            return None
        if METHOD_RLE in methods:
            rle_out = encode_rle_method(rle)
            if rle_out is None:
                return None
            out[METHOD_RLE] = prefix + rle_out
        if METHOD_LZ in methods:
            lz_out = encode_lz_method(rle)
            if lz_out is None:
                return None
            out[METHOD_LZ] = prefix + lz_out
    return out, prefix


def tile_candidates(canvas, src_stride: int, x: int, y: int, tw: int,
                     th: int, channels: int, offset: int, mod: int,
                     pred_type: int, color_space: int,
                     methods=(METHOD_RAW, METHOD_RLE, METHOD_LZ),
                     transform: int = 0) -> dict | None:
    """Method candidates for one tile, computed in native code.

    Only the requested methods are encoded (a forced method must not pay
    for the other two). Returns None when the backend is unavailable or
    refuses the input; the caller then falls back to the pure-Python
    reference path."""
    lib = _load()
    if lib is None or not enabled():
        return None
    count = tw * th * channels
    try:
        tile_buf = _array("i", bytes(4 * count))
        res_buf = _array("i", bytes(4 * count))
    except (OverflowError, TypeError, MemoryError):
        return None
    if not extract_tile(canvas, src_stride, x, y, tw, th, channels, tile_buf):
        return None
    if not pipe_forward_into(tile_buf, tw * channels, offset, mod, pred_type,
                             channels, color_space, res_buf, transform):
        return None
    out = {}
    if METHOD_RAW in methods:
        raw = encode_raw_method(res_buf, mod)
        if raw is None:
            return None
        out[METHOD_RAW] = raw
    if METHOD_RLE in methods or METHOD_LZ in methods:
        packed = pack_residuals_buf(res_buf, mod)
        if packed is None:
            return None
        rle = rle_encode_bytes(packed)
        if rle is None:
            return None
        if METHOD_RLE in methods:
            rle_out = encode_rle_method(rle)
            if rle_out is None:
                return None
            out[METHOD_RLE] = rle_out
        if METHOD_LZ in methods:
            lz_out = encode_lz_method(rle)
            if lz_out is None:
                return None
            out[METHOD_LZ] = lz_out
    return out


# ------------------------------------------------------- whole-image codec

def encode_image(pixels, width: int, height: int, channels: int,
                 bit_depth: int, color_space: int, pred_type: int = -1,
                 method: int = -1, tile_size: int = 256, transform: int = -1,
                 filter_mode: int = -1, sample_rows: int = 8) -> bytes | None:
    """Encode a whole image in one native call.

    Returns the container bytes, or None when the native backend is
    unavailable or refuses the input (the caller then runs the pure-Python
    reference path, which produces the same bytes).
    """
    lib = _load()
    if lib is None or not enabled():
        return None
    try:
        buf = _array("i", pixels)
    except (OverflowError, TypeError, ValueError):
        return None
    n = len(buf)
    if n != width * height * channels:
        return None
    src = ctypes.cast(buf.buffer_info()[0], ctypes.POINTER(ctypes.c_int32))
    out_p = ctypes.POINTER(ctypes.c_ubyte)()
    out_len = ctypes.c_size_t()
    rc = lib.lum_encode_image(
        src, n, width, height, channels, bit_depth, color_space, pred_type,
        method, tile_size, transform, filter_mode, sample_rows,
        ctypes.byref(out_p), ctypes.byref(out_len))
    if rc != 0 or not out_p:
        return None
    try:
        return ctypes.string_at(out_p, out_len.value)
    finally:
        lib.lum_free_buf(out_p, out_len.value)


def encode_image_bytes(raw: bytes, width: int, height: int, channels: int,
                      bit_depth: int, color_space: int, pred_type: int = -1,
                      method: int = -1, tile_size: int = 256,
                      transform: int = -1, filter_mode: int = -1,
                      sample_rows: int = 8) -> bytes | None:
    """Encode a whole image from raw sample bytes (8-bit or big-endian u16).

    This is the zero-conversion variant of `encode_image`: the samples are
    widened inside the native core, so the caller never has to build a
    Python list of ints. Returns None when the native backend is unavailable
    or refuses the input.
    """
    lib = _load()
    if lib is None or not enabled():
        return None
    samples = width * height * channels
    need = samples * (2 if bit_depth == 16 else 1)
    if bit_depth not in (8, 16) or len(raw) < need:
        return None
    buf = ctypes.create_string_buffer(bytes(raw[:need]), need)
    src = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    out_p = ctypes.POINTER(ctypes.c_ubyte)()
    out_len = ctypes.c_size_t()
    rc = lib.lum_encode_image_bytes(
        src, need, width, height, channels, bit_depth, color_space, pred_type,
        method, tile_size, transform, filter_mode, sample_rows,
        ctypes.byref(out_p), ctypes.byref(out_len))
    if rc != 0 or not out_p:
        return None
    try:
        return ctypes.string_at(out_p, out_len.value)
    finally:
        lib.lum_free_buf(out_p, out_len.value)


def decode_image(data: bytes):
    """Decode a whole container in one native call.

    Returns (meta_dict, pixels) or None when the native backend is
    unavailable or the file is malformed (the caller then runs the
    pure-Python reference path, which raises the same exception).
    """
    lib = _load()
    if lib is None or not enabled():
        return None
    raw = ctypes.create_string_buffer(data, len(data))
    src = ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte))
    out_p = ctypes.POINTER(ctypes.c_int32)()
    out_len = ctypes.c_size_t()
    meta = (ctypes.c_int32 * 12)()
    rc = lib.lum_decode_image(src, len(data), ctypes.byref(out_p),
                                ctypes.byref(out_len), meta)
    if rc != 0 or not out_p:
        return None
    try:
        pixels = _array("i")
        pixels.frombytes(ctypes.string_at(out_p, out_len.value * 4))
    finally:
        lib.lum_free_buf_i32(out_p, out_len.value)
    m = list(meta)
    meta_dict = {
        "version": m[0],
        "width": m[1],
        "height": m[2],
        "bit_depth": m[3],
        "color_space": m[4],
        "pred_type": m[5],
        "method": m[6],
        "tile_size": m[7],
        "num_tiles": m[8],
        "transform": m[9],
        "filter_mode": m[10],
    }
    return meta_dict, pixels.tolist()
