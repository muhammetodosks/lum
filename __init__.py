# PYRA - Pure Python reversible image format & codec (stdlib only).
__version__ = "0.2.0"

from .constants import (
    MAGIC, VERSION, VERSION_LEGACY,
    CS_GRAY, CS_RGB, CS_RGBA,
    PRED_NONE, PRED_LEFT, PRED_UP, PRED_AVERAGE, PRED_PAETH, PRED_GRADIENT, PRED_MEDIAN,
    METHOD_RAW, METHOD_RLE, METHOD_LZ, METHOD_MIX, METHOD_COUNT,
    TRANSFORM_YCOCG, TRANSFORM_NONE, FILTER_GLOBAL, FILTER_ADAPTIVE,
    PyraError, SecurityError, CorruptedFileError,
)
from .codec import PyraCodec
from .container import PyraContainer
from .tile import Tile, split_into_tiles, assemble_from_tiles
from .security import validate_dimensions, validate_chunk_size, validate_file_size
from .api import (
    encode, decode, encode_file, decode_file,
    decode_region, iter_tiles, encode_animation,
)

__all__ = [
    "MAGIC", "VERSION", "VERSION_LEGACY",
    "CS_GRAY", "CS_RGB", "CS_RGBA",
    "PRED_NONE", "PRED_LEFT", "PRED_UP", "PRED_AVERAGE", "PRED_PAETH", "PRED_GRADIENT", "PRED_MEDIAN",
    "METHOD_RAW", "METHOD_RLE", "METHOD_LZ", "METHOD_MIX", "METHOD_COUNT",
    "TRANSFORM_YCOCG", "TRANSFORM_NONE", "FILTER_GLOBAL", "FILTER_ADAPTIVE",
    "PyraError", "SecurityError", "CorruptedFileError",
    "PyraCodec", "PyraContainer",
    "Tile", "split_into_tiles", "assemble_from_tiles",
    "validate_dimensions", "validate_chunk_size", "validate_file_size",
    "encode", "decode", "encode_file", "decode_file",
    "decode_region", "iter_tiles", "encode_animation",
    "__version__",
]
