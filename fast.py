"""Loader for the optional C++ fast-path library.

The C++ core (`cpp/pyra_fast.cpp`) implements the two hottest inner loops
of the v2 codec:

  * `adaptive_residuals`  - colour transform + per-row predictor selection
    (seven candidates per row, scored by the modular L1 distance) in one
    pass over the tile.
  * `adaptive_reconstruct` - the inverse: every row is reconstructed with
    the predictor it was encoded with, descaled and inverse-transformed.

Everything here follows the same contract as the Rust backend: on any
problem the wrapper returns None and the caller falls back to the pure
Python reference implementation, which produces bit-identical output.
"""

import ctypes
import os
from array import array

_LIB = None
_TRIED = False


def _load():
    global _LIB, _TRIED
    if _TRIED:
        return _LIB
    _TRIED = True
    try:
        pkg = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(pkg, "_libpyra_fast.so")
        lib = ctypes.CDLL(path)
        lib.pyra_fast_adaptive_residuals.restype = ctypes.c_int
        lib.pyra_fast_adaptive_residuals.argtypes = [
            ctypes.POINTER(ctypes.c_int32), ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64,
            ctypes.c_int32, ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_int),
        ]
        lib.pyra_fast_adaptive_reconstruct.restype = ctypes.c_int
        lib.pyra_fast_adaptive_reconstruct.argtypes = [
            ctypes.POINTER(ctypes.c_int32), ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64,
            ctypes.c_int32, ctypes.c_int32,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_int32),
        ]
        _LIB = lib
    except (OSError, AttributeError):
        _LIB = None
    return _LIB


def available() -> bool:
    """True when the C++ fast path can be used."""
    if os.environ.get("PYRA_DISABLE_NATIVE"):
        return False
    if os.environ.get("PYRA_NATIVE") == "0":
        return False
    return _load() is not None


def _i32_buffer(pixels):
    try:
        buf = array("i", pixels)
    except (OverflowError, TypeError, ValueError):
        return None, None
    if buf.itemsize != 4:
        return None, None
    n = len(buf)
    if n == 0:
        return buf, None
    view = (ctypes.c_int32 * n).from_buffer(buf)
    return buf, view


def adaptive_residuals(pixels, width, height, channels, offset, mod,
                        color_space, transform):
    """Residuals + per-row predictor table for one tile.

    Returns (residuals, filter_bits) or None."""
    lib = _load()
    if lib is None:
        return None
    count = width * height * channels
    if count <= 0 or height <= 0:
        return None
    src_buf, src = _i32_buffer(pixels)
    if src is None:
        return None
    out_buf = array("i", bytes(4 * count))
    out_view = (ctypes.c_int32 * count).from_buffer(out_buf)
    nbytes = (height * 3 + 7) // 8
    bits_buf = bytearray(nbytes)
    bits_view = (ctypes.c_uint8 * nbytes).from_buffer(bits_buf)
    detail = ctypes.c_int(0)
    rc = lib.pyra_fast_adaptive_residuals(
        src, count, width, height, channels, offset, mod, color_space,
        transform, out_view, bits_view, ctypes.byref(detail))
    if rc != 0:
        return None
    return out_buf.tolist(), bytes(bits_buf)


def adaptive_reconstruct(residuals, width, height, channels, offset, mod,
                         color_space, transform, filter_bits):
    """Reconstruct one tile from per-row residuals. Returns pixels or None."""
    lib = _load()
    if lib is None:
        return None
    count = width * height * channels
    if count <= 0 or height <= 0:
        return None
    res_buf, res = _i32_buffer(residuals)
    if res is None:
        return None
    nbytes = (height * 3 + 7) // 8
    if len(filter_bits) < nbytes:
        return None
    bits_buf = bytearray(filter_bits[:nbytes])
    bits_view = (ctypes.c_uint8 * nbytes).from_buffer(bits_buf)
    out_buf = array("i", bytes(4 * count))
    out_view = (ctypes.c_int32 * count).from_buffer(out_buf)
    detail = ctypes.c_int(0)
    rc = lib.pyra_fast_adaptive_reconstruct(
        res, count, width, height, channels, offset, mod, color_space,
        transform, bits_view, out_view)
    if rc != 0:
        return None
    return out_buf.tolist()


def plan(pixels, width, height, channels, bit_depth, color_space,
         sample_rows):
    """Deprecated: the encoder planner lives in the Rust core.

    The plan has to be measured with the real range coder (the ideal code
    length is biased towards the wider YCoCg alphabet), and the coder lives
    in the Rust core — so there is exactly one planner, and the pure-Python
    reference mirrors it."""
    return None
