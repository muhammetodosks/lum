"""Window/level preview of a medical volume.

The preview is a *display* helper: it never touches the master data, it only
renders an 8 bit grey image of one slice for viewers that cannot show 16 bit
or floating point samples.

The native C++ helper (:func:`pyra.fast.medical_preview`) does the work when
it is available; the pure-Python fallback below produces exactly the same
bytes.
"""

import struct

from .. import fast as _fast
from .constants import (SAMPLE_BYTES, SAMPLE_FORMATS, SAMPLE_SIGNED, SFMT_F32,
                        UnsupportedFormatError)


def _sample_value(samples: bytes, index: int, sample_format: int) -> float:
    """Read one sample as a float, in the native sample domain."""
    sb = SAMPLE_BYTES[sample_format]
    off = index * sb
    if sample_format == SFMT_F32:
        return struct.unpack_from("<f", samples, off)[0]
    raw = int.from_bytes(samples[off:off + sb], "little")
    if sample_format in SAMPLE_SIGNED:
        bits = 8 * sb
        if raw >= (1 << (bits - 1)):
            raw -= 1 << bits
    return float(raw)


def preview_slice(samples: bytes, width: int, height: int, depth: int,
                  channels: int, sample_format: int, slice: int = 0,
                  window: float = 400.0, level: float = 40.0) -> bytes | None:
    """Render one slice as 8 bit grey samples.

    `window` is the width of the displayed intensity range and `level` its
    centre. Returns the raw 8 bit pixels (width*height bytes) or None when
    the geometry does not match the buffer.
    """
    if sample_format not in SAMPLE_FORMATS:
        raise UnsupportedFormatError(f"unknown sample format {sample_format}")
    if width <= 0 or height <= 0 or depth <= 0 or channels <= 0:
        return None
    if not 0 <= slice < depth:
        return None
    sb = SAMPLE_BYTES[sample_format]
    need = width * height * depth * channels * sb
    if len(samples) < need:
        return None

    native = _fast.medical_preview(samples, width, height, depth, channels,
                                   sample_format, slice, window, level)
    if native is not None:
        return native

    span = window if window > 0 else 1.0
    low = level - span / 2.0
    out = bytearray(width * height)
    base = slice * height * width * channels
    for y in range(height):
        for x in range(width):
            v = 0.0
            for c in range(channels):
                idx = base + (y * width + x) * channels + c
                v += _sample_value(samples, idx, sample_format)
            v /= channels
            t = (v - low) * 255.0 / span
            if t < 0:
                t = 0.0
            elif t > 255:
                t = 255.0
            out[y * width + x] = int(t)
    return bytes(out)


__all__ = ["preview_slice"]
