"""Constants of the PYRM (PYRA Medical) container format.

The medical profile is built for imaging data: lossless to the last bit,
wide sample domains (8/16/32 bit, signed and float), volumetric data
(many slices), and hard integrity guarantees (per-tile CRC, payload CRC and
a SHA-256 content hash of the raw samples).
"""

MAGIC = b"PYRM\r\n\x1A\n"
VERSION = 2
VERSION_V1 = 1

# ---------------------------------------------------------------- sample types
SFMT_U8 = 0    # unsigned 8 bit
SFMT_U16 = 1   # unsigned 16 bit
SFMT_U32 = 2   # unsigned 32 bit
SFMT_I16 = 3   # signed 16 bit (two's complement)
SFMT_I32 = 4   # signed 32 bit (two's complement)
SFMT_F32 = 5   # IEEE-754 binary32

SAMPLE_FORMATS = (SFMT_U8, SFMT_U16, SFMT_U32, SFMT_I16, SFMT_I32, SFMT_F32)

#: Bits per sample for every sample format.
SAMPLE_BITS = {
    SFMT_U8: 8,
    SFMT_U16: 16,
    SFMT_U32: 32,
    SFMT_I16: 16,
    SFMT_I32: 32,
    SFMT_F32: 32,
}

#: Bytes per sample for every sample format.
SAMPLE_BYTES = {
    SFMT_U8: 1,
    SFMT_U16: 2,
    SFMT_U32: 4,
    SFMT_I16: 2,
    SFMT_I32: 4,
    SFMT_F32: 4,
}

#: True when the samples are signed and need canonicalisation into the
#: unsigned domain before the residual arithmetic.
SAMPLE_SIGNED = (SFMT_I16, SFMT_I32)


def sample_domain(sample_format: int) -> tuple[int, int]:
    """Return (modulus, canonical offset) of a sample format.

    `modulus` is the size of the residual alphabet (2**bits) and
    `canonical offset` is the value added to signed samples so that they
    live in [0, modulus).
    """
    bits = SAMPLE_BITS[sample_format]
    modulus = 1 << bits
    if sample_format in SAMPLE_SIGNED:
        return modulus, modulus >> 1
    return modulus, 0


# ----------------------------------------------------------------- predictors
# 0..6 are the classic 2D filters, 7 adds the volumetric context (the same
# sample in the previous slice).
PRED_NONE = 0
PRED_LEFT = 1
PRED_UP = 2
PRED_AVERAGE = 3
PRED_PAETH = 4
PRED_GRADIENT = 5
PRED_MEDIAN = 6
PRED_SLICE = 7
#: The median edge detector of LOCO-I / JPEG-LS. It picks the neighbour
#: whose difference to the two others is the smallest, which follows the
#: edges of an anatomical structure instead of averaging across them.
PRED_MED = 8

PRED_COUNT = 9

#: Candidate order used by the adaptive row filter. The tie-break is
#: "first candidate wins", so every implementation agrees.
PRED_CANDIDATES_2D = (PRED_NONE, PRED_LEFT, PRED_UP, PRED_AVERAGE,
                      PRED_PAETH, PRED_GRADIENT, PRED_MEDIAN, PRED_MED)
PRED_CANDIDATES_3D = PRED_CANDIDATES_2D + (PRED_SLICE,)

#: Bits per row in the row filter table.
FILTER_BITS_PER_ROW = 4

# --------------------------------------------------------------- tile methods
METHOD_RAW = 0   # residual bytes straight into the entropy coder
METHOD_LZ = 1    # residual bytes -> LZ77 token stream -> entropy coder
METHOD_COUNT = 2

#: Tile body flag bits.
TILE_FLAG_SPLIT = 0x01   # residual bytes were split into byte planes
TILE_FLAG_ADAPTIVE = 0x02  # the body starts with a row filter table
TILE_FLAG_ESCAPE = 0x04  # residuals use the zigzag + escape coding

# ------------------------------------------------------- escape coding
# Wide samples are coded with one symbol per sample instead of one symbol
# per byte: the centred residual is zig-zag encoded into 0..127 and the rare
# large residuals escape into a fixed number of 7 bit chunks. For medical
# data more than 99 % of the residuals fit into +-63, so the entropy coder
# sees half as many symbols and the tile is coded noticeably faster.
ESCAPE_SYMBOL = 128       # marks an escaped residual
ESCAPE_ALPHABET = 129     # 0..127 zig-zag values, 128 escape marker
ZIGZAG_LIMIT = 63         # largest centred residual that fits in 7 bits
ESCAPE_CHUNK_BITS = 7     # bits per escape chunk

# --------------------------------------------------------------- LZ77 tokens
LZ_MIN_MATCH = 4
LZ_MAX_MATCH = 259        # 4 + 255
LZ_MAX_DIST = 65536
LZ_TOKEN_MATCH = 256
LZ_TOKEN_EOF = 257
LZ_TOKEN_SYMBOLS = 258


class MedicalError(Exception):
    """Base class of every medical-format error."""


class CorruptedFileError(MedicalError):
    """The container is damaged, truncated or inconsistent."""


class UnsupportedFormatError(MedicalError):
    """The container uses a feature this implementation does not know."""


class MedicalMetadataError(MedicalError):
    """The metadata block is incomplete or contradicts the image geometry."""
