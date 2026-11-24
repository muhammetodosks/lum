"""LUMM — the LUMEN Medical container format.

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

    from lum.medical import encode, decode

    blob = encode(raw_samples, width=512, height=512, depth=32,
                  sample_format=SFMT_U16)
    meta, samples = decode(blob)
    assert samples == raw_samples
"""

from .constants import (MAGIC, METHOD_LZ, METHOD_RAW, PRED_AVERAGE,
                        PRED_CANDIDATES_2D, PRED_CANDIDATES_3D, PRED_GRADIENT,
                        PRED_LEFT, PRED_MED, PRED_MEDIAN, PRED_NONE,
                        PRED_PAETH, PRED_SLICE, PRED_UP, SAMPLE_BITS,
                        SAMPLE_BYTES,
                        SAMPLE_FORMATS, SFMT_F32, SFMT_I16, SFMT_I32, SFMT_U8,
                        SFMT_U16, SFMT_U32, VERSION, CorruptedFileError,
                        MedicalError, MedicalMetadataError,
                        UnsupportedFormatError)
from .codec import decode, decode_region, encode, info, verify
from .signature import compute as signature_compute
from .signature import sign, status as signature_status
from .dicom import (MONOCHROME1, MONOCHROME2, RGB, build_dicom_metadata,
                    describe, generate_uid, is_dicom_compatible,
                    validate_dicom_metadata)
from .preview import preview_from_metadata, preview_slice
from . import native

__all__ = [
    "encode", "decode", "decode_region", "info", "verify", "preview_slice",
    "sign", "signature_status", "signature_compute",
    "preview_from_metadata", "native", "MAGIC", "VERSION",
    "build_dicom_metadata", "validate_dicom_metadata", "is_dicom_compatible",
    "generate_uid",
    "describe", "MONOCHROME1", "MONOCHROME2", "RGB",
    "MedicalMetadataError",
    "SFMT_U8", "SFMT_U16", "SFMT_U32", "SFMT_I16", "SFMT_I32", "SFMT_F32",
    "SAMPLE_FORMATS", "SAMPLE_BITS", "SAMPLE_BYTES",
    "METHOD_RAW", "METHOD_LZ",
    "PRED_NONE", "PRED_LEFT", "PRED_UP", "PRED_AVERAGE", "PRED_PAETH",
    "PRED_GRADIENT", "PRED_MEDIAN", "PRED_SLICE", "PRED_MED",
    "PRED_CANDIDATES_2D", "PRED_CANDIDATES_3D",
    "MedicalError", "CorruptedFileError", "UnsupportedFormatError",
]
