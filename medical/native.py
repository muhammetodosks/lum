"""Native loader of the PYRM medical core.

The medical core lives in the same shared library as the rest of PYRA
(`_libpyra_core.so`). Every wrapper returns ``None`` when the backend is
unavailable or refuses the input, so the caller can fall back to the
pure-Python reference implementation, which produces identical bytes.
"""

import ctypes
import os

from .constants import (LZ_TOKEN_SYMBOLS, METHOD_LZ, METHOD_RAW, SAMPLE_FORMATS,
                        CorruptedFileError)

_LIB = None
_LOADED = False


def _load():
    """Load `_libpyra_core.so` once; returns None when it is missing."""
    global _LIB, _LOADED
    if _LOADED:
        return _LIB
    _LOADED = True
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "_libpyra_core.so")
    try:
        lib = ctypes.CDLL(path)
    except OSError:
        _LIB = None
        return None
    _i32 = ctypes.c_int32
    _u32 = ctypes.c_uint32
    _i64 = ctypes.c_int64
    _sz = ctypes.c_size_t
    _isize = ctypes.c_long if ctypes.sizeof(ctypes.c_long) == 8 else ctypes.c_int
    try:
        lib.pyra_med_encode.restype = _isize
        lib.pyra_med_encode.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), _sz, _u32, _u32, _u32, _u32,
            _u32, ctypes.POINTER(ctypes.c_ubyte), _sz, _sz, _i32, _i32,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
            ctypes.POINTER(_sz)]
        lib.pyra_med_decode.restype = _isize
        lib.pyra_med_decode.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), _sz,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
            ctypes.POINTER(_sz)]
        lib.pyra_med_info.restype = _isize
        lib.pyra_med_info.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), _sz, ctypes.POINTER(_i32)]
        lib.pyra_med_decode_region.restype = _isize
        lib.pyra_med_decode_region.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), _sz,
            _i32, _i32, _i32, _i32, _i32, _i32,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
            ctypes.POINTER(_sz)]
        lib.pyra_free_buf.restype = None
        lib.pyra_free_buf.argtypes = [ctypes.POINTER(ctypes.c_ubyte), _sz]
    except OSError:
        _LIB = None
        return None
    _LIB = lib
    return lib


def enabled() -> bool:
    """Runtime dispatch flag (read at call time)."""
    if os.environ.get("PYRA_DISABLE_NATIVE"):
        return False
    if os.environ.get("PYRA_NATIVE") == "0":
        return False
    return True


def available() -> bool:
    """True when the native medical core can be used."""
    return enabled() and _load() is not None


def encode(samples: bytes, width: int, height: int, depth: int = 1,
           channels: int = 1, sample_format: int = 1, metadata: bytes = b"",
           tile_size: int = 256, predictor: int = -1, method: int = -1) -> bytes | None:
    """Encode a whole medical image in one native call.

    `metadata` is the raw metadata block (JSON bytes) that goes into the
    container. Returns the container bytes or None.
    """
    lib = _load()
    if lib is None or not enabled():
        return None
    sb = {0: 1, 1: 2, 2: 4, 3: 2, 4: 4, 5: 4}.get(sample_format)
    if sb is None:
        return None
    need = width * height * depth * channels * sb
    if len(samples) < need:
        return None
    sbuf = ctypes.create_string_buffer(bytes(samples[:need]), need)
    src = ctypes.cast(sbuf, ctypes.POINTER(ctypes.c_ubyte))
    mbuf = ctypes.create_string_buffer(bytes(metadata), max(1, len(metadata)))
    msrc = ctypes.cast(mbuf, ctypes.POINTER(ctypes.c_ubyte))
    out_p = ctypes.POINTER(ctypes.c_ubyte)()
    out_len = ctypes.c_size_t()
    rc = lib.pyra_med_encode(
        src, need, width, height, depth, channels, sample_format,
        ctypes.cast(msrc, ctypes.POINTER(ctypes.c_ubyte)), len(metadata),
        tile_size, predictor, method, ctypes.byref(out_p), ctypes.byref(out_len))
    if rc != 0 or not out_p:
        return None
    try:
        return ctypes.string_at(out_p, out_len.value)
    finally:
        lib.pyra_free_buf(out_p, out_len.value)


def decode(blob: bytes) -> bytes | None:
    """Decode a whole container in one native call.

    Returns the raw samples or None when the backend is unavailable or the
    container is rejected (the caller then runs the reference path, which
    raises the very same exception).
    """
    lib = _load()
    if lib is None or not enabled():
        return None
    buf = ctypes.create_string_buffer(bytes(blob), len(blob))
    src = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    out_p = ctypes.POINTER(ctypes.c_ubyte)()
    out_len = ctypes.c_size_t()
    rc = lib.pyra_med_decode(src, len(blob), ctypes.byref(out_p),
                              ctypes.byref(out_len))
    if rc != 0 or not out_p:
        return None
    try:
        return ctypes.string_at(out_p, out_len.value)
    finally:
        lib.pyra_free_buf(out_p, out_len.value)


def decode_region(blob: bytes, x: int, y: int, z: int, width: int,
                  height: int, depth: int) -> bytes | None:
    """Decode only a rectangular region of a container in one native call.

    Returns the tight region (slice by slice, row by row, only the
    requested columns) or None when the backend is unavailable or the
    request is rejected (the caller then runs the reference path, which
    raises the very same exception).
    """
    lib = _load()
    if lib is None or not enabled():
        return None
    buf = ctypes.create_string_buffer(bytes(blob), len(blob))
    src = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    out_p = ctypes.POINTER(ctypes.c_ubyte)()
    out_len = ctypes.c_size_t()
    rc = lib.pyra_med_decode_region(src, len(blob), x, y, z, width, height,
                                    depth, ctypes.byref(out_p),
                                    ctypes.byref(out_len))
    if rc != 0 or not out_p:
        return None
    try:
        return ctypes.string_at(out_p, out_len.value)
    finally:
        lib.pyra_free_buf(out_p, out_len.value)


def info(blob: bytes) -> dict | None:
    """Read the header of a container without decoding the samples."""
    lib = _load()
    if lib is None or not enabled():
        return None
    buf = ctypes.create_string_buffer(bytes(blob), len(blob))
    src = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    meta = (ctypes.c_int32 * 8)()
    rc = lib.pyra_med_info(src, len(blob), meta)
    if rc != 0:
        return None
    return {
        "width": meta[0], "height": meta[1], "depth": meta[2],
        "channels": meta[3], "sample_format": meta[4], "bits": meta[5],
        "tile_size": meta[6], "num_tiles": meta[7],
    }


__all__ = ["encode", "decode", "decode_region", "info", "available", "enabled"]
