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

from .constants import CorruptedFileError


def _u32_ptr(values) -> tuple:
    """Fast u32 array: array.array("I") beats a starred ctypes tuple build."""
    a = _array("I", values)
    ptr = ctypes.cast(a.buffer_info()[0], ctypes.POINTER(ctypes.c_uint32))
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
