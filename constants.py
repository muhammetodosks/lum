import struct
import zlib
import sys
import os
from typing import List, Tuple, Optional, BinaryIO, Generator

MAGIC = b"PYRA\r\n\x1A\n"
VERSION = 3
VERSION_LEGACY = 1
VERSION_V2 = 2

# Color Spaces
CS_GRAY = 1
CS_RGB = 2
CS_RGBA = 3

# Predictors
PRED_NONE = 0
PRED_LEFT = 1
PRED_UP = 2
PRED_AVERAGE = 3
PRED_PAETH = 4
PRED_GRADIENT = 5
PRED_MEDIAN = 6

# Color transforms (stored in the container header)
# TRANSFORM_YCOCG: reversible RGB->YCoCg (cross-channel decorrelation),
#                   residual alphabet is 2^(bit_depth+3) wide.
# TRANSFORM_NONE:  channels are predicted as-is, residual alphabet is
#                   2^bit_depth wide (smaller alphabet -> better entropy
#                   coding, no cross-channel decorrelation).
TRANSFORM_YCOCG = 0
TRANSFORM_NONE = 1
_TRANSFORM_COUNT = 2

# Row filter modes (stored in the container header)
# FILTER_GLOBAL:    one predictor for the whole image (header pred_type)
# FILTER_ADAPTIVE:  every row picks its own predictor; the selection is
#                   stored per tile as 3 bits per row
FILTER_GLOBAL = 0
FILTER_ADAPTIVE = 1

# Tile compression methods (stored in the container header)
METHOD_RAW = 0   # residual symbols straight into the entropy coder
METHOD_RLE = 1   # packed bytes -> RLE -> literal tokens -> entropy
METHOD_LZ  = 2   # packed bytes -> RLE -> LZ77 tokens -> entropy
METHOD_MIX = 3   # per-tile best of RAW/RLE/LZ; each tile body starts with a method byte
METHOD_COUNT = 3 # candidate count for global auto-select (RAW/RLE/LZ)
_VALID_METHODS = (METHOD_RAW, METHOD_RLE, METHOD_LZ, METHOD_MIX)

class PyraError(Exception):
    pass

class SecurityError(PyraError):
    pass

class CorruptedFileError(PyraError):
    pass
