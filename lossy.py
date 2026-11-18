# PYRA v6 — lossy codec binding (Rust core).
#
# The codec itself lives in `rust/pyra_lossy.rs`: YCbCr (optional 4:2:0),
# 8x8 DCT, perceptual quantisation and an adaptive binary range coder. This
# module is only the ctypes glue, mirroring the style of `native.py`:
#
#   encode(raw, width, height, color_space, quality, subsampling) -> bytes
#   decode(blob) -> (meta_dict, raw_rgb_bytes)
#   info(blob)   -> header dict without decoding the planes
#
# Every function returns None when the shared library is missing/disabled or
# refuses the input, so callers can fall back to an error with the same
# wording as the native core.

import ctypes
import os

from .constants import CS_GRAY, CS_RGB, CS_RGBA, CorruptedFileError

_lib = None
_load_attempted = False

_ERR = {
    -500: "invalid PYRA v6 parameter",
    -501: "malformed PYRA v6 header",
    -502: "PYRA v6 CRC mismatch",
    -503: "malformed PYRA v6 stream",
}

SUBSAMPLE_AUTO = 0
SUBSAMPLE_444 = 1
SUBSAMPLE_420 = 2


def _load():
    """One-shot load of the cdylib; caches the result (None = not built)."""
    global _lib, _load_attempted
    if _load_attempted:
        return _lib
    _load_attempted = True
    if os.environ.get("PYRA_DISABLE_NATIVE") or os.environ.get("PYRA_NATIVE") == "0":
        return None
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "_libpyra_core.so")
    try:
        lib = ctypes.CDLL(path)
    except OSError:
        return None
    _sz = ctypes.c_size_t
    _isize = ctypes.c_ssize_t
    _u8p = ctypes.POINTER(ctypes.c_ubyte)
    try:
        lib.pyra_lossy_encode.restype = _isize
        lib.pyra_lossy_encode.argtypes = [
            _u8p, _sz, _sz, _sz, _sz, ctypes.c_int32, ctypes.c_int32,
            ctypes.c_int32, ctypes.POINTER(_u8p), ctypes.POINTER(_sz)]
        lib.pyra_lossy_decode.restype = _isize
        lib.pyra_lossy_decode.argtypes = [
            _u8p, _sz, ctypes.POINTER(_u8p), ctypes.POINTER(_sz)]
        lib.pyra_lossy_info.restype = _isize
        lib.pyra_lossy_info.argtypes = [
            _u8p, _sz, ctypes.POINTER(ctypes.c_int32)]
        lib.pyra_free_buf.restype = None
        lib.pyra_free_buf.argtypes = [_u8p, _sz]
    except AttributeError:
        return None
    _lib = lib
    return _lib


def available() -> bool:
    """True when the native lossy codec is built and enabled."""
    return _load() is not None


def _error(rc: int) -> CorruptedFileError:
    return CorruptedFileError(_ERR.get(rc, f"PYRA v6 codec error {rc}"))


def channels_for(color_space: int) -> int:
    if color_space == CS_GRAY:
        return 1
    if color_space == CS_RGB:
        return 3
    if color_space == CS_RGBA:
        return 4
    raise ValueError(f"invalid colour space: {color_space}")


def encode(raw: bytes, width: int, height: int, color_space: int = CS_RGB,
           quality: int = 90, subsampling: int = SUBSAMPLE_AUTO):
    """Encode interleaved 8-bit samples into a PYRA v6 lossy container."""
    lib = _load()
    if lib is None:
        return None
    ch = channels_for(color_space)
    need = width * height * ch
    if len(raw) < need:
        raise ValueError("raw sample buffer is too small")
    buf = ctypes.create_string_buffer(bytes(raw[:need]), need)
    src = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    out_p = ctypes.POINTER(ctypes.c_ubyte)()
    out_len = ctypes.c_size_t()
    rc = lib.pyra_lossy_encode(src, need, width, height, ch, color_space,
                               int(quality), int(subsampling),
                               ctypes.byref(out_p), ctypes.byref(out_len))
    if rc != 0 or not out_p:
        raise _error(rc)
    try:
        return ctypes.string_at(out_p, out_len.value)
    finally:
        lib.pyra_free_buf(out_p, out_len.value)


def decode(blob: bytes):
    """Decode a PYRA v6 container into `(meta, raw_samples)`."""
    lib = _load()
    if lib is None:
        raise CorruptedFileError("the native PYRA v6 codec is not available")
    raw = ctypes.create_string_buffer(bytes(blob), len(blob))
    src = ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte))
    out_p = ctypes.POINTER(ctypes.c_ubyte)()
    out_len = ctypes.c_size_t()
    rc = lib.pyra_lossy_decode(src, len(blob), ctypes.byref(out_p),
                               ctypes.byref(out_len))
    if rc != 0 or not out_p:
        raise _error(rc)
    try:
        samples = ctypes.string_at(out_p, out_len.value)
    finally:
        lib.pyra_free_buf(out_p, out_len.value)
    meta = info(blob)
    if meta is None:
        raise CorruptedFileError("malformed PYRA v6 header")
    return meta, samples


def info(blob: bytes):
    """Read a PYRA v6 header (validating the CRC) without decoding planes."""
    lib = _load()
    if lib is None:
        return None
    raw = ctypes.create_string_buffer(bytes(blob), len(blob))
    src = ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte))
    meta = (ctypes.c_int32 * 8)()
    rc = lib.pyra_lossy_info(src, len(blob), meta)
    if rc != 0:
        return None
    m = list(meta)
    return {
        "version": 6,
        "width": m[0],
        "height": m[1],
        "bit_depth": m[2],
        "color_space": m[3],
        "quality": m[4],
        "subsampling": 2 if m[5] else 1,
        "num_planes": m[6],
        "mode": m[7],
        "lossy": True,
    }
