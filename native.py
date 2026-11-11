# PYRA native core loader — optional Rust-backed hot paths (stdlib only).
#
# `scripts/build_native.sh` (rustc, std-only cdylib) produces
# `_libpyra_core.so` next to this module. Every wrapper here returns None
# when the backend is unavailable OR disabled, and the caller in compress.py
# falls back to the byte-identical pure-Python reference — so results are
# bit-exact either way and the codec works without Rust installed.
#
# Runtime dispatch flags (checked at call time, not import time):
#   PYRA_DISABLE_NATIVE=1   force the pure-Python path
#   PYRA_NATIVE=0           force the pure-Python path
#
# Rust error convention: every fn returns isize; < 0 is an error code that
# is mapped here to the same exception/message the Python reference raises.

import ctypes
import os
from array import array as _array

from .constants import CorruptedFileError, PyraError

# LZ/RLE token alphabet size (0..255 literal, 256 MATCH, 257 EOF).
# Duplicated here to avoid a circular import with compress.py.
_TOKEN_SYMBOLS = 258


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

# --- Rust error codes (must match rust/pyra_core.rs) ---
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
                            "_libpyra_core.so")
        try:
            lib = ctypes.CDLL(path)
            _isize = ctypes.c_ssize_t
            _sz = ctypes.c_size_t
            _u32 = ctypes.c_uint32
            _u64 = ctypes.c_uint64
            _i64 = ctypes.c_int64
            lib.pyra_pack_residuals.restype = _isize
            lib.pyra_pack_residuals.argtypes = [
                ctypes.POINTER(_u32), _sz, _sz, _u64,
                ctypes.POINTER(ctypes.c_ubyte)]
            lib.pyra_unpack_residuals.restype = _isize
            lib.pyra_unpack_residuals.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz, _sz, _sz, _u64,
                ctypes.POINTER(_u32)]
            lib.pyra_rle_encode.restype = _isize
            lib.pyra_rle_encode.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz,
                ctypes.POINTER(ctypes.c_ubyte)]
            lib.pyra_rle_decode.restype = _isize
            lib.pyra_rle_decode.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz, _sz,
                ctypes.POINTER(ctypes.c_ubyte)]
            lib.pyra_lz_encode.restype = _isize
            lib.pyra_lz_encode.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz, ctypes.POINTER(_u32)]
            lib.pyra_lz_decode_tokens.restype = _isize
            lib.pyra_lz_decode_tokens.argtypes = [
                ctypes.POINTER(_u32), _sz, _sz,
                ctypes.POINTER(ctypes.c_ubyte)]
            _i32 = ctypes.c_int32
            lib.pyra_apply_predictor_row.restype = _isize
            lib.pyra_apply_predictor_row.argtypes = [
                ctypes.POINTER(_i32), _sz, ctypes.POINTER(_i32), _sz,
                _i32, _sz, _i64, ctypes.POINTER(_i32)]
            lib.pyra_reconstruct_row.restype = _isize
            lib.pyra_reconstruct_row.argtypes = [
                ctypes.POINTER(_i32), _sz, ctypes.POINTER(_i32), _sz,
                _i32, _sz, _i64, ctypes.POINTER(_i32)]
            lib.pyra_transform_forward.restype = _isize
            lib.pyra_transform_forward.argtypes = [
                ctypes.POINTER(_i32), _sz, _i32, ctypes.POINTER(_i32)]
            lib.pyra_transform_inverse.restype = _isize
            lib.pyra_transform_inverse.argtypes = [
                ctypes.POINTER(_i32), _sz, _i32, ctypes.POINTER(_i32)]
            lib.pyra_pipe_forward.restype = _isize
            lib.pyra_pipe_forward.argtypes = [
                ctypes.POINTER(_i32), _sz, _sz, _i32, _i64, _i64,
                _i32, _sz, ctypes.POINTER(_i32)]
            lib.pyra_pipe_reconstruct.restype = _isize
            lib.pyra_pipe_reconstruct.argtypes = [
                ctypes.POINTER(_i32), _sz, _sz, _i32, _i64, _i64,
                _i32, _sz, ctypes.POINTER(_i32)]
            lib.pyra_range_encode.restype = _isize
            lib.pyra_range_encode.argtypes = [
                ctypes.POINTER(_u32), _sz, _sz,
                ctypes.POINTER(ctypes.c_ubyte), _sz]
            lib.pyra_range_decode.restype = _isize
            lib.pyra_range_decode.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz, _sz, _sz,
                ctypes.POINTER(_u32)]
            lib.pyra_decode_rle_method.restype = _isize
            lib.pyra_decode_rle_method.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz, _sz, _sz, _sz, _u64,
                _sz, ctypes.POINTER(_u32), ctypes.POINTER(_i32)]
            lib.pyra_decode_lz_method.restype = _isize
            lib.pyra_decode_lz_method.argtypes = [
                ctypes.POINTER(ctypes.c_ubyte), _sz, _sz, _sz, _sz, _sz, _u64,
                _sz, ctypes.POINTER(_u32), ctypes.POINTER(_u32),
                ctypes.POINTER(_i32)]
            _lib = lib
        except OSError:
            _lib = None
    return _lib


def enabled() -> bool:
    """Runtime dispatch flag (read at call time so tests can toggle it)."""
    if os.environ.get("PYRA_DISABLE_NATIVE"):
        return False
    if os.environ.get("PYRA_NATIVE") == "0":
        return False
    return True


def available() -> bool:
    """True when the native backend is built AND enabled."""
    return _load() is not None and enabled()


def build_path() -> str | None:
    return (os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "_libpyra_core.so")
            if _load() is not None else None)


# ------------------------------------------------------------ wrapped calls
# Each returns None when the backend must not be used; otherwise raises the
# reference exception on any Rust error code, or returns the result.

def pack_residuals(residuals: list[int], mod: int) -> bytes | None:
    lib = _load()
    if lib is None or not enabled():
        return None
    width = (mod.bit_length() + 7) // 8
    n = len(residuals)
    a, syms = _u32_ptr(residuals)
    out = (ctypes.c_ubyte * (n * width))()
    rc = lib.pyra_pack_residuals(syms, n, width, mod, out)
    if rc < 0:
        # -100 - idx: first out-of-range symbol
        raise ValueError(f"residual symbol out of range at index {-rc - _E_PACK_RANGE}")
    return bytes(memoryview(out))


def unpack_residuals(data: bytes, mod: int, count: int) -> list[int] | None:
    lib = _load()
    if lib is None or not enabled():
        return None
    width = (mod.bit_length() + 7) // 8
    if len(data) < count * width:
        raise CorruptedFileError("truncated packed residual stream")
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    out = (ctypes.c_uint32 * count)()
    rc = lib.pyra_unpack_residuals(buf, len(data), count, width, mod, out)
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
    rc = lib.pyra_rle_encode(buf, len(data), out)
    if rc < 0:
        raise CorruptedFileError("native RLE encoder failure")
    return bytes(memoryview(out)[:rc])


def rle_decode(data: bytes, target_len: int) -> bytes | None:
    lib = _load()
    if lib is None or not enabled():
        return None
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data or b"\x00")
    out = (ctypes.c_ubyte * target_len)()
    rc = lib.pyra_rle_decode(buf, len(data), target_len, out)
    if rc < 0:
        raise CorruptedFileError(_RLE_MESSAGES.get(rc, "corrupted RLE stream"))
    return bytes(memoryview(out))


def lz_encode(data: bytes) -> list[int] | None:
    lib = _load()
    if lib is None or not enabled():
        return None
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data or b"\x00")
    out = (ctypes.c_uint32 * (len(data) + 2))()
    rc = lib.pyra_lz_encode(buf, len(data), out)
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
    rc = lib.pyra_lz_decode_tokens(buf, n, target_len, out)
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
    rc = lib.pyra_apply_predictor_row(buf, n, prow, plen, pred_type, channels, mod, out)
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
    rc = lib.pyra_reconstruct_row(buf, n, prow, plen, pred_type, channels, mod, out)
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
    rc = lib.pyra_transform_forward(buf, n, color_space, out)
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
    rc = lib.pyra_transform_inverse(buf, n, color_space, out)
    if rc < 0:
        raise ValueError("native YCoCg transform failure")
    return list(out)


# ------------------------------------------------------- fused pipelines
# Single-conversion counterparts of PyraCodec._residual_stream (color
# transform + canonicalization + row prediction) and the decode-side tail
# (row reconstruction + descaling + inverse transform). Returns None on
# shape mismatch so the caller falls back to the reference composition.

def pipe_residuals(data: list[int], stride: int, offset: int, mod: int,
                   pred_type: int, channels: int,
                   color_space: int) -> list[int] | None:
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
    rc = lib.pyra_pipe_forward(buf, n, stride, color_space, offset, mod,
                               pred_type, channels, out)
    if rc < 0:
        if rc == _E_PIPE_SHAPE:
            return None
        raise ValueError(f"native pipeline failure ({rc})")
    return list(out)


def pipe_reconstruct(residuals: list[int], stride: int, offset: int, mod: int,
                     pred_type: int, channels: int,
                     color_space: int) -> list[int] | None:
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
    rc = lib.pyra_pipe_reconstruct(buf, n, stride, color_space, offset, mod,
                                   pred_type, channels, out)
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
        raise PyraError("model needs at least 1 symbol")
    n = len(symbols)
    a, buf = _u32_ptr(symbols)
    # Worst-case output size: the range coder emits at most
    # ceil(log2(model_total)) + 1 bits per symbol, and the adaptive model
    # total is capped at max(2^15, 2 * n_symbols).
    bits_per = max(15, n_symbols.bit_length() + 1) + 1
    cap = (n * bits_per) // 8 + 64
    out = (ctypes.c_ubyte * cap)()
    rc = lib.pyra_range_encode(buf, n, n_symbols, out, cap)
    if rc < 0:
        return None
    return bytes(memoryview(out)[:rc])


def range_decode(data: bytes, n_symbols: int, count: int) -> list[int] | None:
    """Decode exactly `count` symbols. Returns None when unavailable."""
    lib = _load()
    if lib is None or not enabled():
        return None
    if n_symbols < 1:
        raise PyraError("model needs at least 1 symbol")
    buf = (ctypes.c_ubyte * max(len(data), 1)).from_buffer_copy(data or b"\x00")
    out = (ctypes.c_uint32 * count)()
    rc = lib.pyra_range_decode(buf, len(data), n_symbols, count, out)
    if rc < 0:
        return None
    return list(out)


# ------------------------------------------------- fused method decoders
# One native call per tile stage: range decode -> RLE/LZ -> residual
# unpacking. On ANY error the wrapper returns None so the caller re-runs
# the pure-Python reference and raises the exact reference exception
# (bit-exact error semantics in both modes).

def decode_rle_method(data: bytes, count: int, mod: int) -> list[int] | None:
    """METHOD_RLE residuals in one native call (None => use the reference)."""
    lib = _load()
    if lib is None or not enabled():
        return None
    width = (mod.bit_length() + 7) // 8
    packed_len = count * width
    buf = (ctypes.c_ubyte * max(len(data), 1)).from_buffer_copy(data or b"\x00")
    out = (ctypes.c_uint32 * count)()
    detail = ctypes.c_int32(0)
    rc = lib.pyra_decode_rle_method(buf, len(data), _TOKEN_SYMBOLS,
                                    packed_len, width, ctypes.c_uint64(mod),
                                    count, out, ctypes.byref(detail))
    if rc < 0:
        return None
    return list(out)


def decode_lz_method(data: bytes, count: int, mod: int) -> list[int] | None:
    """METHOD_LZ residuals in one native call (None => use the reference)."""
    lib = _load()
    if lib is None or not enabled():
        return None
    width = (mod.bit_length() + 7) // 8
    packed_len = count * width
    max_rle_len = 2 * packed_len + 64
    buf = (ctypes.c_ubyte * max(len(data), 1)).from_buffer_copy(data or b"\x00")
    out = (ctypes.c_uint32 * count)()
    rle_len = ctypes.c_uint32(0)
    detail = ctypes.c_int32(0)
    rc = lib.pyra_decode_lz_method(buf, len(data), _TOKEN_SYMBOLS,
                                   max_rle_len, packed_len, width,
                                   ctypes.c_uint64(mod), count, out,
                                   ctypes.byref(rle_len), ctypes.byref(detail))
    if rc < 0:
        return None
    return list(out)
