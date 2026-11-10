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
