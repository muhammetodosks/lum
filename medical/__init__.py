"""PYRM — the PYRA Medical container format.

A lossless, integrity-protected image format built for medical imaging:

* **lossless to the last bit** — the decoded samples are byte-identical to
  the input, and the container carries a SHA-256 hash of the raw samples so
  a mixed-up series is detected immediately;
* **wide sample domains** — 8/16/32 bit unsigned, 16/32 bit signed and
  IEEE-754 float32;
* **volumetric** — many slices, with a predictor that reads the previous
  slice;
* **fast** — the whole pipeline runs in the native Rust core, spread over
  the available cores, and a pure-Python reference path stays available.

Example::

    from pyra.medical import encode, decode

    blob = encode(raw_samples, width=512, height=512, depth=32,
                  sample_format=SFMT_U16)
    meta, samples = decode(blob)
    assert samples == raw_samples
"""

from .constants import (MAGIC, METHOD_LZ, METHOD_RAW, PRED_AVERAGE,
                        PRED_CANDIDATES_2D, PRED_CANDIDATES_3D, PRED_GRADIENT,
                        PRED_LEFT, PRED_MEDIAN, PRED_NONE, PRED_PAETH,
                        PRED_SLICE, PRED_UP, SAMPLE_BITS, SAMPLE_BYTES,
                        SAMPLE_FORMATS, SFMT_F32, SFMT_I16, SFMT_I32, SFMT_U8,
                        SFMT_U16, SFMT_U32, VERSION, CorruptedFileError,
                        MedicalError, UnsupportedFormatError)
from .codec import decode, encode, info
from .preview import preview_slice
from . import native

__all__ = [
    "encode", "decode", "info", "preview_slice", "native", "MAGIC", "VERSION",
    "SFMT_U8", "SFMT_U16", "SFMT_U32", "SFMT_I16", "SFMT_I32", "SFMT_F32",
    "SAMPLE_FORMATS", "SAMPLE_BITS", "SAMPLE_BYTES",
    "METHOD_RAW", "METHOD_LZ",
    "PRED_NONE", "PRED_LEFT", "PRED_UP", "PRED_AVERAGE", "PRED_PAETH",
    "PRED_GRADIENT", "PRED_MEDIAN", "PRED_SLICE",
    "PRED_CANDIDATES_2D", "PRED_CANDIDATES_3D",
    "MedicalError", "CorruptedFileError", "UnsupportedFormatError",
]
