"""Window/level preview of a medical volume.

The preview is a *display* helper: it never touches the master data, it only
renders an 8 bit grey image of one slice for viewers that cannot show 16 bit
or floating point samples.

Three clinical aspects are honoured, because ignoring any of them shows the
clinician a *different* image than the one that was acquired:

* the **calibration** (rescale slope and intercept) turns the stored numbers
  into their physical units (Hounsfield units for CT, for instance) — the
  display window is defined in those units;
* the **photometric interpretation** decides which end of the range is
  black: ``MONOCHROME2`` (default) is black at the low end, ``MONOCHROME1``
  is white at the low end (the inverted convention used by some modalities);
* the **display window** (centre and width) is applied in the calibrated
  domain and the result is clamped to 0..255.

The native C++ helper (:func:`pyra.fast.medical_preview`) does the work when
it is available; the pure-Python fallback below produces exactly the same
bytes.
"""

import struct

from .. import fast as _fast
from .constants import (SAMPLE_BYTES, SAMPLE_FORMATS, SAMPLE_SIGNED, SFMT_F32,
                        UnsupportedFormatError)
from .dicom import MONOCHROME1, is_monochrome1, rescale
from .dicom import window as _meta_window


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
                  window: float = 400.0, level: float = 40.0,
                  slope: float = 1.0, intercept: float = 0.0,
                  invert: bool = False) -> bytes | None:
    """Render one slice as 8 bit grey samples.

    `window` is the width of the displayed intensity range and `level` its
    centre; both are given in the *calibrated* domain, so `slope` and
    `intercept` are applied to the stored samples first. `invert` selects
    the MONOCHROME1 convention (the low end is white).

    Returns the raw 8 bit pixels (width*height bytes) or None when the
    geometry does not match the buffer.
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
                                   sample_format, slice, window, level,
                                   slope, intercept, 1 if invert else 0)
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
            v = (v / channels) * slope + intercept
            t = (v - low) * 255.0 / span
            if t < 0:
                t = 0.0
            elif t > 255:
                t = 255.0
            value = int(t)
            if invert:
                value = 255 - value
            out[y * width + x] = value
    return bytes(out)


def preview_from_metadata(samples: bytes, width: int, height: int, depth: int,
                          channels: int, sample_format: int, metadata: dict | None,
                          slice: int = 0,
                          window: float | None = None,
                          level: float | None = None) -> bytes | None:
    """Render a slice using the calibration and the window of the metadata.

    The window centre and width come from the metadata when it carries them
    (DICOM ``WindowCenter`` / ``WindowWidth``); the caller's values win when
    they are given. The photometric interpretation of the metadata decides
    whether the image is inverted.
    """
    slope, intercept = rescale(metadata)
    meta_center, meta_width = _meta_window(metadata)
    if level is None:
        level = meta_center if meta_center is not None else 40.0
    if window is None:
        window = meta_width if meta_width is not None else 400.0
    return preview_slice(samples, width, height, depth, channels, sample_format,
                         slice, window, level, slope, intercept,
                         is_monochrome1(metadata))


__all__ = ["preview_slice", "preview_from_metadata", "MONOCHROME1"]
