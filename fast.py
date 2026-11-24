"""Loader for the optional C++ fast-path library.

The C++ core (`cpp/lum_fast.cpp`) implements the two hottest inner loops
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
        path = os.path.join(pkg, "_liblum_fast.so")
        lib = ctypes.CDLL(path)
        lib.lum_fast_adaptive_residuals.restype = ctypes.c_int
        lib.lum_fast_adaptive_residuals.argtypes = [
            ctypes.POINTER(ctypes.c_int32), ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64,
            ctypes.c_int32, ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_int),
        ]
        lib.lum_fast_adaptive_reconstruct.restype = ctypes.c_int
        lib.lum_fast_adaptive_reconstruct.argtypes = [
            ctypes.POINTER(ctypes.c_int32), ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64,
            ctypes.c_int32, ctypes.c_int32,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_int32),
        ]
        lib.lum_med_preview.restype = ctypes.c_int
        lib.lum_med_preview.argtypes = [
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_double, ctypes.c_double,
            ctypes.c_double, ctypes.c_double, ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint8),
        ]
        lib.lum_pnm_read.restype = ctypes.c_int
        lib.lum_pnm_read.argtypes = [
            ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.lum_pnm_write.restype = ctypes.c_int
        lib.lum_pnm_write.argtypes = [
            ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        _LIB = lib
    except (OSError, AttributeError):
        _LIB = None
    return _LIB


def available() -> bool:
    """True when the C++ fast path can be used."""
    if os.environ.get("LUMEN_DISABLE_NATIVE"):
        return False
    if os.environ.get("LUMEN_NATIVE") == "0":
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
    rc = lib.lum_fast_adaptive_residuals(
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
    rc = lib.lum_fast_adaptive_reconstruct(
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


# --------------------------------------------------------------- medical


def medical_preview(samples: bytes, width: int, height: int, depth: int,
                    channels: int, sample_format: int, slice: int,
                    window: float, level: float, slope: float = 1.0,
                    intercept: float = 0.0, invert: bool = False):
    """Window/level 8 bit preview of one slice of a medical volume.

    The samples are little endian, exactly like in the LUMM container.
    `slope`/`intercept` apply the calibration of the modality before the
    window; `invert` selects the MONOCHROME1 convention. Returns one byte
    per pixel (the channels are averaged) or None."""
    lib = _load()
    if lib is None:
        return None
    if width <= 0 or height <= 0 or depth <= 0 or channels <= 0:
        return None
    if not samples:
        return None
    src = ctypes.create_string_buffer(bytes(samples), len(samples))
    src_view = ctypes.cast(src, ctypes.POINTER(ctypes.c_uint8))
    count = width * height
    out_buf = bytearray(count)
    out_view = (ctypes.c_uint8 * count).from_buffer(out_buf)
    rc = lib.lum_med_preview(src_view, len(samples), width, height, depth,
                              channels, sample_format, slice, window, level,
                              float(slope), float(intercept),
                              1 if invert else 0, out_view)
    if rc != 0:
        return None
    return bytes(out_buf)


def pnm_read(path: str):
    """Read a binary PGM/PPM file.

    Returns (raw_bytes, channels, width, height, bit_depth, max_val) or
    None. The raw bytes hold the samples exactly as stored in the file."""
    lib = _load()
    if lib is None:
        return None
    cap = 1 << 28
    buf = bytearray(cap)
    view = (ctypes.c_uint8 * cap).from_buffer(buf)
    info = (ctypes.c_int * 6)()
    rc = lib.lum_pnm_read(path.encode("utf-8"), view, cap, info)
    if rc <= 0:
        return None
    return (bytes(buf[:rc]), info[0], info[1], info[2], info[3], info[4])


def pnm_write(path: str, data: bytes, width: int, height: int, channels: int,
              bit_depth: int) -> bool:
    """Write a binary PGM/PPM file. Returns True on success."""
    lib = _load()
    if lib is None:
        return False
    if not data:
        return False
    src = ctypes.create_string_buffer(bytes(data), len(data))
    src_view = ctypes.cast(src, ctypes.POINTER(ctypes.c_uint8))
    rc = lib.lum_pnm_write(path.encode("utf-8"), src_view, width, height,
                            channels, bit_depth)
    return rc == 0
