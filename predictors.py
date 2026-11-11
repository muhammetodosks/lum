from .constants import *
from . import native as _native

def paeth_predictor(a: int, b: int, c: int) -> int:
    """Paeth predictor as used in PNG."""
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    elif pb <= pc:
        return b
    else:
        return c

def median_predictor(a: int, b: int, c: int) -> int:
    """Median predictor (JPEG-LS / LOCO-I)."""
    if c >= max(a, b):
        return min(a, b)
    elif c <= min(a, b):
        return max(a, b)
    else:
        return a + b - c

def predict_pixel(pred_type: int, left: int, up: int, upper_left: int) -> int:
    if pred_type == PRED_NONE:
        return 0
    elif pred_type == PRED_LEFT:
        return left
    elif pred_type == PRED_UP:
        return up
    elif pred_type == PRED_AVERAGE:
        return (left + up) >> 1
    elif pred_type == PRED_PAETH:
        return paeth_predictor(left, up, upper_left)
    elif pred_type == PRED_GRADIENT:
        # Gradient predictor: left + up - upper_left clamped or wrapped
        return left + up - upper_left
    elif pred_type == PRED_MEDIAN:
        return median_predictor(left, up, upper_left)
    else:
        return 0

def apply_predictor_row(row: list[int], prev_row: Optional[list[int]], pred_type: int, channels: int, mod: int) -> list[int]:
    """Applies prediction to a row, returning residuals in [0, mod).

    `mod` is the full modular alphabet width (already includes every
    value produced by prediction + transform stages)."""
    native_result = _native.apply_predictor_row(row, prev_row, pred_type, channels, mod)
    if native_result is not None:
        return native_result

    width_channels = len(row)
    residuals = [0] * width_channels

    for i in range(width_channels):
        c = i % channels
        x = i // channels

        # Get left, up, upper_left
        left = row[i - channels] if x > 0 else 0
        up = prev_row[i] if prev_row is not None else 0
        upper_left = prev_row[i - channels] if (prev_row is not None and x > 0) else 0

        pred = predict_pixel(pred_type, left, up, upper_left)
        # Ensure mod arithmetic
        res = (row[i] - pred) % mod
        residuals[i] = res

    return residuals

def reconstruct_row(residuals: list[int], prev_row: Optional[list[int]], pred_type: int, channels: int, mod: int) -> list[int]:
    """Reconstructs row from residuals using the same modular alphabet."""
    native_result = _native.reconstruct_row(residuals, prev_row, pred_type, channels, mod)
    if native_result is not None:
        return native_result

    width_channels = len(residuals)
    row = [0] * width_channels

    for i in range(width_channels):
        c = i % channels
        x = i // channels

        left = row[i - channels] if x > 0 else 0
        up = prev_row[i] if prev_row is not None else 0
        upper_left = prev_row[i - channels] if (prev_row is not None and x > 0) else 0

        pred = predict_pixel(pred_type, left, up, upper_left)
        orig = (residuals[i] + pred) % mod
        row[i] = orig

    return row


# ---------------------------------------------------------------------------
# Adaptive row filters (v2 format).
#
# Every row picks the predictor whose modular residuals are cheapest, where
# "cheapest" is the sum of the circular distance of every residual from
# zero (min(r, mod-r)). The circular distance is used instead of |r| because
# the residual alphabet is modular: a residual of mod-1 is as close to zero
# as a residual of 1.
#
# The candidate order is fixed and the tie-break is "first candidate wins",
# so the native (Rust/C++) and the pure-Python paths always agree.
# ---------------------------------------------------------------------------

PRED_CANDIDATES = (PRED_NONE, PRED_LEFT, PRED_UP, PRED_AVERAGE,
                   PRED_PAETH, PRED_GRADIENT, PRED_MEDIAN)

FILTER_BITS_PER_ROW = 3
FILTER_CANDIDATE_COUNT = len(PRED_CANDIDATES)


def _predict_row_raw(row: list[int], prev_row: Optional[list[int]], pred_type: int,
                     channels: int, mod: int) -> list[int]:
    """Residuals of one row for one predictor (no native dispatch)."""
    width_channels = len(row)
    residuals = [0] * width_channels
    for i in range(width_channels):
        x = i // channels
        left = row[i - channels] if x > 0 else 0
        up = prev_row[i] if prev_row is not None else 0
        upper_left = prev_row[i - channels] if (prev_row is not None and x > 0) else 0
        pred = predict_pixel(pred_type, left, up, upper_left)
        residuals[i] = (row[i] - pred) % mod
    return residuals


def apply_predictor_row_adaptive(row: list[int], prev_row: Optional[list[int]],
                                 channels: int, mod: int) -> tuple[list[int], int]:
    """Best predictor for one row + its residuals."""
    best_res = None
    best_pred = PRED_NONE
    best_score = -1
    for pred_type in PRED_CANDIDATES:
        res = _predict_row_raw(row, prev_row, pred_type, channels, mod)
        score = 0
        for r in res:
            score += r if r <= mod - r else mod - r
        if best_score < 0 or score < best_score:
            best_score = score
            best_res = res
            best_pred = pred_type
    return best_res, best_pred


def pack_filter_bits(pred_types: list[int]) -> bytes:
    """Pack one 3-bit predictor id per row (MSB first)."""
    out = bytearray()
    acc = 0
    nbits = 0
    for p in pred_types:
        acc = (acc << FILTER_BITS_PER_ROW) | (p & 7)
        nbits += FILTER_BITS_PER_ROW
        if nbits >= 8:
            out.append((acc >> (nbits - 8)) & 0xFF)
            nbits -= 8
            acc &= (1 << nbits) - 1
    if nbits:
        out.append((acc << (8 - nbits)) & 0xFF)
    return bytes(out)


def unpack_filter_bits(data: bytes, rows: int) -> list[int]:
    """Inverse of pack_filter_bits (MSB-first bit stream, fields may straddle bytes)."""
    out = []
    acc = 0
    nbits = 0
    pos = 0
    for _ in range(rows):
        while nbits < FILTER_BITS_PER_ROW:
            if pos >= len(data):
                acc <<= FILTER_BITS_PER_ROW
                nbits += FILTER_BITS_PER_ROW
                break
            acc = (acc << 8) | data[pos]
            pos += 1
            nbits += 8
        out.append((acc >> (nbits - FILTER_BITS_PER_ROW)) & 7)
        nbits -= FILTER_BITS_PER_ROW
        acc &= (1 << nbits) - 1
    return out
