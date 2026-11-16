"""PYRC — the PYRA Cloud container (Python binding).

This module is a **thin binding**: every byte of the container is produced
by the Rust core (`_libpyra_core.so`). There is deliberately no pure-Python
fallback here — the cloud profile is implemented once, in Rust, so there is
only one definition of the format.

The data model is

    sample[scene][band][row][col]

with the samples of a scene stored band by band (band planar), each band a
plane of ``width * height`` little endian samples.
"""

import ctypes
import json
import os
import struct

from .constants import CorruptedFileError, PyraError


class CloudError(PyraError):
    """Base class of the cloud profile errors."""


class UnsupportedFormatError(CloudError):
    """The container uses a feature this implementation does not know."""


class CloudCorruptedError(CorruptedFileError, CloudError):
    """The container is damaged, truncated or not a PYRC container.

    It is both a :class:`CloudError` and a
    :class:`pyra.constants.CorruptedFileError`, so either name catches it.
    """

# ---------------------------------------------------------------- formats

SFMT_U8 = 0
SFMT_U16 = 1
SFMT_U32 = 2
SFMT_I16 = 3
SFMT_I32 = 4
SFMT_F32 = 5

SAMPLE_BYTES = {SFMT_U8: 1, SFMT_U16: 2, SFMT_U32: 4,
                SFMT_I16: 2, SFMT_I32: 4, SFMT_F32: 4}

FORMAT_NAMES = {SFMT_U8: "u8", SFMT_U16: "u16", SFMT_U32: "u32",
                SFMT_I16: "i16", SFMT_I32: "i32", SFMT_F32: "f32"}
FORMAT_BY_NAME = {v: k for k, v in FORMAT_NAMES.items()}

# --------------------------------------------------------------- predictors

PRED_NONE = 0
PRED_LEFT = 1
PRED_UP = 2
PRED_AVERAGE = 3
PRED_PAETH = 4
PRED_GRADIENT = 5
PRED_MEDIAN = 6
PRED_TIME = 7
PRED_BAND = 8
PRED_COUNT = 9

PRED_NAMES = {PRED_NONE: "none", PRED_LEFT: "left", PRED_UP: "up",
              PRED_AVERAGE: "average", PRED_PAETH: "paeth",
              PRED_GRADIENT: "gradient", PRED_MEDIAN: "median",
              PRED_TIME: "time", PRED_BAND: "band"}
PRED_BY_NAME = {v: k for k, v in PRED_NAMES.items()}

METHOD_RAW = 0
METHOD_LZ = 1
METHOD_NAMES = {METHOD_RAW: "raw", METHOD_LZ: "lz"}
METHOD_BY_NAME = {v: k for k, v in METHOD_NAMES.items()}

# ------------------------------------------------------------ band / scene

BAND_TYPE_NAMES = {
    "reflectance": 0,
    "radiance": 1,
    "brightness_temperature": 2,
    "quality_flag": 3,
    "classification": 4,
    "precipitation": 5,
    "albedo": 6,
    "elevation": 7,
}
BAND_TYPE_BY_NAME = {v: k for k, v in BAND_TYPE_NAMES.items()}

UNITS_NAMES = {"reflectance": 0, "radiance": 1, "kelvin": 2, "percent": 3,
                "metre": 4, "millimetre": 5, "none": 6}
UNITS_BY_NAME = {v: k for k, v in UNITS_NAMES.items()}

BAND_ENTRY_SIZE = 48
SCENE_ENTRY_SIZE = 40

#: The magic bytes of a PYRC container.
MAGIC = b"PYRC\r\n\x1A\n"

_LIB = None
_TRIED = False


def _load():
    global _LIB, _TRIED
    if _TRIED:
        return _LIB
    _TRIED = True
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "_libpyra_core.so")
    try:
        lib = ctypes.CDLL(path)
    except OSError:
        _LIB = None
        return None
    _sz = ctypes.c_size_t
    _u32 = ctypes.c_uint32
    _i32 = ctypes.c_int32
    _isize = ctypes.c_long if ctypes.sizeof(ctypes.c_long) == 8 else ctypes.c_int
    try:
        lib.pyra_cloud_encode.restype = _isize
        lib.pyra_cloud_encode.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), _sz, _u32, _u32, _u32, _u32, _u32,
            ctypes.POINTER(ctypes.c_ubyte), _sz,
            ctypes.POINTER(ctypes.c_ubyte), _sz,
            ctypes.POINTER(ctypes.c_ubyte), _sz,
            _sz, _i32, _i32,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)), ctypes.POINTER(_sz)]
        lib.pyra_cloud_decode.restype = _isize
        lib.pyra_cloud_decode.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), _sz,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)), ctypes.POINTER(_sz),
            ctypes.POINTER(_i32)]
        lib.pyra_cloud_info.restype = _isize
        lib.pyra_cloud_info.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), _sz, ctypes.POINTER(_i32)]
        lib.pyra_cloud_read_band.restype = _isize
        lib.pyra_cloud_read_band.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), _sz, _u32, _u32,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)), ctypes.POINTER(_sz)]
        lib.pyra_free_buf.restype = None
        lib.pyra_free_buf.argtypes = [ctypes.POINTER(ctypes.c_ubyte), _sz]
    except OSError:
        _LIB = None
        return None
    _LIB = lib
    return lib


def available() -> bool:
    """True when the native cloud core can be used."""
    return _load() is not None


# ------------------------------------------------------------------ tables

def _pack_band_table(bands) -> bytes:
    """Serialise the band table (48 bytes per band)."""
    out = bytearray()
    for band in bands:
        name = str(band.get("name", "")).encode("utf-8")[:16]
        name = name + b"\x00" * (16 - len(name))
        wavelength = float(band.get("wavelength_nm", 0.0))
        btype = int(band.get("type", 0))
        units = int(band.get("units", 0))
        scale = float(band.get("scale", 1.0))
        offset = float(band.get("offset", 0.0))
        vmin = float(band.get("valid_min", 0.0))
        vmax = float(band.get("valid_max", 0.0))
        out += name
        out += struct.pack("<dIIffff", wavelength, btype, units, scale, offset,
                            vmin, vmax)
    return bytes(out)


def _unpack_band_table(blob: bytes):
    out = []
    for i in range(len(blob) // BAND_ENTRY_SIZE):
        off = i * BAND_ENTRY_SIZE
        name = blob[off:off + 16].split(b"\x00", 1)[0].decode("utf-8", "replace")
        wavelength, btype, units, scale, offset, vmin, vmax = struct.unpack_from(
            "<dIIffff", blob, off + 16)
        out.append({
            "name": name,
            "wavelength_nm": wavelength,
            "type": btype,
            "units": units,
            "scale": scale,
            "offset": offset,
            "valid_min": vmin,
            "valid_max": vmax,
        })
    return out


def _pack_scene_table(scenes) -> bytes:
    """Serialise the scene table (40 bytes per scene)."""
    out = bytearray()
    for scene in scenes:
        timestamp = int(scene.get("timestamp", 0))
        lon0 = float(scene.get("lon0", 0.0))
        lat0 = float(scene.get("lat0", 0.0))
        px = float(scene.get("pixel_size_x", 0.0))
        py = float(scene.get("pixel_size_y", 0.0))
        out += struct.pack("<qdddd", timestamp, lon0, lat0, px, py)
    return bytes(out)


def _unpack_scene_table(blob: bytes):
    out = []
    for i in range(len(blob) // SCENE_ENTRY_SIZE):
        off = i * SCENE_ENTRY_SIZE
        timestamp, lon0, lat0, px, py = struct.unpack_from("<qdddd", blob, off)
        out.append({
            "timestamp": timestamp,
            "lon0": lon0,
            "lat0": lat0,
            "pixel_size_x": px,
            "pixel_size_y": py,
        })
    return out


# -------------------------------------------------------------------- api

def encode(samples: bytes, width: int, height: int, bands: int, scenes: int,
           sample_format: int = SFMT_U16, band_meta=None, scene_meta=None,
           metadata=None, tile_size: int = 256, predictor: int = -1,
           method: int = -1) -> bytes:
    """Encode a multi-band, multi-scene cube into a PYRC container.

    `samples` holds the raw little endian samples, scene by scene, band by
    band (band planar). `band_meta` and `scene_meta` are lists of
    dictionaries (see the module docstring for the fields). `predictor` and
    `method` accept -1, which lets the encoder choose.
    """
    lib = _load()
    if lib is None:
        raise UnsupportedFormatError(
            "the native cloud core is not available (build _libpyra_core.so)")
    if sample_format not in SAMPLE_BYTES:
        raise UnsupportedFormatError(f"unknown sample format {sample_format}")
    if width <= 0 or height <= 0 or bands <= 0 or scenes <= 0:
        raise ValueError("degenerate cube geometry")
    if predictor < -1 or predictor >= PRED_COUNT:
        raise UnsupportedFormatError(f"unsupported predictor {predictor}")
    if method not in (-1, METHOD_RAW, METHOD_LZ):
        raise UnsupportedFormatError(f"unsupported method {method}")
    sb = SAMPLE_BYTES[sample_format]
    need = width * height * bands * scenes * sb
    if len(samples) < need:
        raise ValueError("sample buffer too small for the given geometry")

    band_blob = _pack_band_table(band_meta or [{}] * bands)
    scene_blob = _pack_scene_table(scene_meta or [{}] * scenes)
    if metadata is None:
        meta_blob = b""
    elif isinstance(metadata, (bytes, bytearray)):
        meta_blob = bytes(metadata)
    else:
        meta_blob = json.dumps(metadata, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")

    src = ctypes.create_string_buffer(bytes(samples[:need]), need)
    src_ptr = ctypes.cast(src, ctypes.POINTER(ctypes.c_ubyte))
    band_buf = ctypes.create_string_buffer(band_blob, max(1, len(band_blob)))
    band_ptr = ctypes.cast(band_buf, ctypes.POINTER(ctypes.c_ubyte))
    scene_buf = ctypes.create_string_buffer(scene_blob, max(1, len(scene_blob)))
    scene_ptr = ctypes.cast(scene_buf, ctypes.POINTER(ctypes.c_ubyte))
    meta_buf = ctypes.create_string_buffer(meta_blob, max(1, len(meta_blob)))
    meta_ptr = ctypes.cast(meta_buf, ctypes.POINTER(ctypes.c_ubyte))

    out_p = ctypes.POINTER(ctypes.c_ubyte)()
    out_len = ctypes.c_size_t()
    rc = lib.pyra_cloud_encode(
        src_ptr, need, width, height, bands, scenes, sample_format,
        band_ptr, len(band_blob), scene_ptr, len(scene_blob),
        meta_ptr, len(meta_blob), tile_size, predictor, method,
        ctypes.byref(out_p), ctypes.byref(out_len))
    if rc != 0 or not out_p:
        raise CloudError(f"cloud encode failed (native error {rc})")
    try:
        return ctypes.string_at(out_p, out_len.value)
    finally:
        lib.pyra_free_buf(out_p, out_len.value)


def _meta_dict(meta) -> dict:
    return {
        "version": meta[0],
        "flags": meta[1],
        "width": meta[2],
        "height": meta[3],
        "bands": meta[4],
        "scenes": meta[5],
        "sample_format": meta[6],
        "bits": meta[7],
        "tile_size": meta[8],
        "tiles_x": meta[9],
        "tiles_y": meta[10],
        "num_entries": meta[11],
        "predictor": meta[12],
        "band_offset": meta[13],
        "band_size": meta[14],
        "scene_offset": meta[15],
        "scene_size": meta[16],
        "meta_offset": meta[17],
        "meta_size": meta[18],
        "index_offset": meta[19],
        "index_size": meta[20],
    }


def info(blob: bytes) -> dict:
    """Read the header of a container without decoding the samples."""
    lib = _load()
    if lib is None:
        raise UnsupportedFormatError("the native cloud core is not available")
    buf = ctypes.create_string_buffer(bytes(blob), len(blob))
    src = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    meta = (ctypes.c_int32 * 21)()
    rc = lib.pyra_cloud_info(src, len(blob), meta)
    if rc != 0:
        raise CloudCorruptedError(f"not a valid PYRC container (native error {rc})")
    out = _meta_dict(meta)
    out["format"] = FORMAT_NAMES.get(out["sample_format"], "?")
    band_blob = bytes(blob[out["band_offset"]:out["band_offset"] + out["band_size"]])
    scene_blob = bytes(blob[out["scene_offset"]:out["scene_offset"] + out["scene_size"]])
    out["band_table"] = _unpack_band_table(band_blob)
    out["scene_table"] = _unpack_scene_table(scene_blob)
    meta_blob = bytes(blob[out["meta_offset"]:out["meta_offset"] + out["meta_size"]])
    out["metadata"] = json.loads(meta_blob.decode("utf-8")) if meta_blob else {}
    return out


def decode(blob: bytes):
    """Decode a PYRC container into (info dict, raw samples)."""
    lib = _load()
    if lib is None:
        raise UnsupportedFormatError("the native cloud core is not available")
    buf = ctypes.create_string_buffer(bytes(blob), len(blob))
    src = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    out_p = ctypes.POINTER(ctypes.c_ubyte)()
    out_len = ctypes.c_size_t()
    meta = (ctypes.c_int32 * 21)()
    rc = lib.pyra_cloud_decode(src, len(blob), ctypes.byref(out_p),
                                ctypes.byref(out_len), meta)
    if rc != 0 or not out_p:
        raise CloudCorruptedError(f"cloud decode failed (native error {rc})")
    try:
        samples = ctypes.string_at(out_p, out_len.value)
    finally:
        lib.pyra_free_buf(out_p, out_len.value)
    return info(blob), samples


def read_band(blob: bytes, scene: int, band: int) -> bytes:
    """Decode one band of one scene (the rest of the cube is untouched)."""
    lib = _load()
    if lib is None:
        raise UnsupportedFormatError("the native cloud core is not available")
    buf = ctypes.create_string_buffer(bytes(blob), len(blob))
    src = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    out_p = ctypes.POINTER(ctypes.c_ubyte)()
    out_len = ctypes.c_size_t()
    rc = lib.pyra_cloud_read_band(src, len(blob), scene, band,
                                  ctypes.byref(out_p), ctypes.byref(out_len))
    if rc != 0 or not out_p:
        raise CloudCorruptedError(f"band read failed (native error {rc})")
    try:
        return ctypes.string_at(out_p, out_len.value)
    finally:
        lib.pyra_free_buf(out_p, out_len.value)


__all__ = [
    "encode", "decode", "info", "read_band", "available",
    "CloudError", "CloudCorruptedError", "UnsupportedFormatError",
    "SFMT_U8", "SFMT_U16", "SFMT_U32", "SFMT_I16", "SFMT_I32", "SFMT_F32",
    "SAMPLE_BYTES", "FORMAT_NAMES", "FORMAT_BY_NAME",
    "PRED_NONE", "PRED_LEFT", "PRED_UP", "PRED_AVERAGE", "PRED_PAETH",
    "PRED_GRADIENT", "PRED_MEDIAN", "PRED_TIME", "PRED_BAND", "PRED_NAMES",
    "PRED_BY_NAME", "METHOD_RAW", "METHOD_LZ", "METHOD_BY_NAME",
    "BAND_TYPE_NAMES", "BAND_TYPE_BY_NAME", "UNITS_NAMES", "UNITS_BY_NAME",
    "fast_available", "raw_read", "raw_write", "band_stats", "histogram",
    "render_rgb", "interleave", "deinterleave",
]


# ------------------------------------------------------------------- C++

_FAST_LIB = None
_FAST_TRIED = False


class CloudBandStats(ctypes.Structure):
    """Statistics of one band over the whole cube (see the C++ helper)."""

    _fields_ = [
        ("minimum", ctypes.c_double),
        ("maximum", ctypes.c_double),
        ("mean", ctypes.c_double),
        ("stddev", ctypes.c_double),
        ("count", ctypes.c_int64),
        ("histogram", ctypes.c_uint64 * 256),
    ]


def _load_fast():
    """Load the optional C++ cloud helper (`_libpyra_cloud.so`)."""
    global _FAST_LIB, _FAST_TRIED
    if _FAST_TRIED:
        return _FAST_LIB
    _FAST_TRIED = True
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "_libpyra_cloud.so")
    try:
        lib = ctypes.CDLL(path)
    except OSError:
        _FAST_LIB = None
        return None
    _sz = ctypes.c_size_t
    _i32 = ctypes.c_int
    _i64 = ctypes.c_int64
    _u64 = ctypes.c_uint64
    _d = ctypes.c_double
    try:
        lib.pyra_cloud_raw_write.restype = _i32
        lib.pyra_cloud_raw_write.argtypes = [
            ctypes.c_char_p, ctypes.POINTER(ctypes.c_ubyte), _i32, _i32,
            _i32, _i32, _i32]
        lib.pyra_cloud_raw_read.restype = _i32
        lib.pyra_cloud_raw_read.argtypes = [
            ctypes.c_char_p, ctypes.POINTER(ctypes.c_ubyte), _sz,
            ctypes.POINTER(_i32)]
        lib.pyra_cloud_band_stats.restype = _i32
        lib.pyra_cloud_band_stats.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), _sz, _i32, _i32, _i32, _i32,
            _i32, _i32, ctypes.POINTER(CloudBandStats)]
        lib.pyra_cloud_render_rgb.restype = _i32
        lib.pyra_cloud_render_rgb.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), _sz, _i32, _i32, _i32, _i32,
            _i32, _i32, _i32, _i32, _i32,
            _d, _d, _d, _d, _d, _d, ctypes.POINTER(ctypes.c_ubyte)]
        lib.pyra_cloud_interleave.restype = _i32
        lib.pyra_cloud_interleave.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), _sz, _i32, _i32, _i32, _i32,
            _i32, _i32, ctypes.POINTER(ctypes.c_ubyte)]
        lib.pyra_cloud_deinterleave.restype = _i32
        lib.pyra_cloud_deinterleave.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte), _sz, _i32, _i32, _i32, _i32,
            ctypes.POINTER(ctypes.c_ubyte)]
    except OSError:
        _FAST_LIB = None
        return None
    _FAST_LIB = lib
    return lib


def fast_available() -> bool:
    """True when the optional C++ cloud helper is available."""
    return _load_fast() is not None


def raw_write(path: str, data: bytes, width: int, height: int, bands: int,
              scenes: int, sample_format: int = SFMT_U16) -> None:
    """Write a raw cube file (the "PYRX" interchange format)."""
    lib = _load_fast()
    if lib is None:
        raise UnsupportedFormatError("the C++ cloud helper is not available")
    sb = SAMPLE_BYTES[sample_format]
    need = width * height * bands * scenes * sb
    if len(data) < need:
        raise ValueError("cube buffer too small for the given geometry")
    buf = ctypes.create_string_buffer(bytes(data[:need]), need)
    src = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    rc = lib.pyra_cloud_raw_write(path.encode("utf-8"), src, width, height,
                                  bands, scenes, sample_format)
    if rc != 0:
        raise PyraError(f"raw cube write failed (native error {rc})")


def raw_read(path: str):
    """Read a raw cube file.

    Returns (samples, width, height, bands, scenes, sample_format).
    """
    lib = _load_fast()
    if lib is None:
        raise UnsupportedFormatError("the C++ cloud helper is not available")
    cap = 1 << 30
    buf = bytearray(cap)
    view = (ctypes.c_ubyte * cap).from_buffer(buf)
    info = (ctypes.c_int * 8)()
    rc = lib.pyra_cloud_raw_read(path.encode("utf-8"), view, cap, info)
    if rc < 0:
        raise CloudCorruptedError(f"raw cube read failed (native error {rc})")
    return (bytes(buf[:rc]), info[0], info[1], info[2], info[3], info[4])


def band_stats(cube: bytes, width: int, height: int, bands: int, scenes: int,
               sample_format: int, band: int) -> dict:
    """Statistics of one band over the whole cube.

    Returns a dictionary with minimum, maximum, mean, stddev, count and the
    256 bin histogram.
    """
    lib = _load_fast()
    if lib is None:
        raise UnsupportedFormatError("the C++ cloud helper is not available")
    sb = SAMPLE_BYTES[sample_format]
    need = width * height * bands * scenes * sb
    if len(cube) < need:
        raise ValueError("cube buffer too small for the given geometry")
    buf = ctypes.create_string_buffer(bytes(cube[:need]), need)
    src = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    stats = CloudBandStats()
    rc = lib.pyra_cloud_band_stats(src, need, width, height, bands, scenes,
                                   sample_format, band, ctypes.byref(stats))
    if rc != 0:
        raise PyraError(f"band statistics failed (native error {rc})")
    return {
        "minimum": stats.minimum,
        "maximum": stats.maximum,
        "mean": stats.mean,
        "stddev": stats.stddev,
        "count": int(stats.count),
        "histogram": list(stats.histogram),
    }


def histogram(cube: bytes, width: int, height: int, bands: int, scenes: int,
              sample_format: int, band: int) -> list[int]:
    """The 256 bin histogram of one band."""
    return band_stats(cube, width, height, bands, scenes, sample_format,
                      band)["histogram"]


def render_rgb(cube: bytes, width: int, height: int, bands: int, scenes: int,
               sample_format: int, scene: int = 0, band_r: int = 0,
               band_g: int = 1, band_b: int = 2, window_r: float = 0.0,
               level_r: float = 0.0, window_g: float = 0.0,
               level_g: float = 0.0, window_b: float = 0.0,
               level_b: float = 0.0) -> bytes:
    """Render a false colour composite of one scene (RGB bytes)."""
    lib = _load_fast()
    if lib is None:
        raise UnsupportedFormatError("the C++ cloud helper is not available")
    sb = SAMPLE_BYTES[sample_format]
    need = width * height * bands * scenes * sb
    if len(cube) < need:
        raise ValueError("cube buffer too small for the given geometry")
    buf = ctypes.create_string_buffer(bytes(cube[:need]), need)
    src = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    count = width * height * 3
    out = bytearray(count)
    view = (ctypes.c_ubyte * count).from_buffer(out)
    rc = lib.pyra_cloud_render_rgb(src, need, width, height, bands, scenes,
                                   sample_format, scene, band_r, band_g,
                                   band_b, window_r, level_r, window_g, level_g,
                                   window_b, level_b, view)
    if rc != 0:
        raise PyraError(f"render failed (native error {rc})")
    return bytes(out)


def interleave(cube: bytes, width: int, height: int, bands: int, scenes: int,
               sample_format: int, scene: int = 0) -> bytes:
    """Convert a band planar cube into a pixel interleaved image."""
    lib = _load_fast()
    if lib is None:
        raise UnsupportedFormatError("the C++ cloud helper is not available")
    sb = SAMPLE_BYTES[sample_format]
    need = width * height * bands * scenes * sb
    if len(cube) < need:
        raise ValueError("cube buffer too small for the given geometry")
    buf = ctypes.create_string_buffer(bytes(cube[:need]), need)
    src = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    count = width * height * bands * sb
    out = bytearray(count)
    view = (ctypes.c_ubyte * count).from_buffer(out)
    rc = lib.pyra_cloud_interleave(src, need, width, height, bands, scenes,
                                   sample_format, scene, view)
    if rc != 0:
        raise PyraError(f"interleave failed (native error {rc})")
    return bytes(out)


def deinterleave(interleaved: bytes, width: int, height: int, bands: int,
                 sample_format: int) -> bytes:
    """Convert a pixel interleaved image back into band planar order."""
    lib = _load_fast()
    if lib is None:
        raise UnsupportedFormatError("the C++ cloud helper is not available")
    sb = SAMPLE_BYTES[sample_format]
    need = width * height * bands * sb
    if len(interleaved) < need:
        raise ValueError("buffer too small for the given geometry")
    buf = ctypes.create_string_buffer(bytes(interleaved[:need]), need)
    src = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    out = bytearray(need)
    view = (ctypes.c_ubyte * need).from_buffer(out)
    rc = lib.pyra_cloud_deinterleave(src, need, width, height, bands,
                                     sample_format, view)
    if rc != 0:
        raise PyraError(f"deinterleave failed (native error {rc})")
    return bytes(out)
