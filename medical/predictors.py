"""Predictors, row filter tables and byte-plane helpers for PYRM.

The prediction context is *tile local*: a tile never borrows a pixel from
its neighbour, which keeps every tile independently decodable (and lets the
encoder and the decoder run tiles in parallel). The only exception is the
volumetric predictor :data:`PRED_SLICE`, which reads the same sample from
the previous slice.
"""

from .constants import (FILTER_BITS_PER_ROW, PRED_AVERAGE, PRED_COUNT,
                        PRED_GRADIENT, PRED_LEFT, PRED_MED, PRED_MEDIAN,
                        PRED_NONE, PRED_PAETH, PRED_SLICE, PRED_UP,
                        PRED_CANDIDATES_2D, PRED_CANDIDATES_3D)


def paeth(a: int, b: int, c: int) -> int:
    """Paeth predictor (PNG)."""
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def median(a: int, b: int, c: int) -> int:
    """Median predictor (JPEG-LS / LOCO-I)."""
    if c >= max(a, b):
        return min(a, b)
    if c <= min(a, b):
        return max(a, b)
    return a + b - c


def circular_distance(a: int, b: int, modulus: int) -> int:
    """Distance of two code values on the modular ring.

    The residual alphabet is modular, so 0 and modulus-1 are neighbours; the
    plain difference would call them the farthest apart.
    """
    d = abs(a - b)
    if d > modulus - d:
        d = modulus - d
    return d


def med_predict(left: int, up: int, upper_left: int, modulus: int) -> int:
    """The median edge detector of LOCO-I / JPEG-LS.

    Of the three neighbours, the one whose difference to the other two is
    the smallest wins: the prediction then follows the edge of an anatomical
    structure instead of averaging across it.
    """
    ma = circular_distance(left, upper_left, modulus)
    mb = circular_distance(up, upper_left, modulus)
    mc = circular_distance(up, left, modulus)
    if ma < mb and ma < mc:
        return left
    if mb < mc:
        return up
    return upper_left


def predict_value(pred_type: int, left: int, up: int, upper_left: int,
                  slice_val: int, modulus: int = 0) -> int:
    """Predicted value for one sample.

    `left`/`up`/`upper_left` come from the same row/column inside the tile,
    `slice_val` is the same sample in the previous slice (0 for the first
    slice). `modulus` is the size of the residual alphabet; it is needed by
    the predictors that compare neighbours (the median edge detector)."""
    if pred_type == PRED_NONE:
        return 0
    if pred_type == PRED_LEFT:
        return left
    if pred_type == PRED_UP:
        return up
    if pred_type == PRED_AVERAGE:
        return (left + up) >> 1
    if pred_type == PRED_PAETH:
        return paeth(left, up, upper_left)
    if pred_type == PRED_GRADIENT:
        return left + up - upper_left
    if pred_type == PRED_MEDIAN:
        return median(left, up, upper_left)
    if pred_type == PRED_MED:
        return med_predict(left, up, upper_left, modulus)
    if pred_type == PRED_SLICE:
        return slice_val
    return 0


def candidate_list(depth: int) -> tuple[int, ...]:
    """Predictor candidates offered to the adaptive row filter."""
    return PRED_CANDIDATES_3D if depth > 1 else PRED_CANDIDATES_2D


def row_residuals(row: list[int], prev_row: list[int] | None,
                   slice_row: list[int] | None, channels: int, modulus: int,
                   pred_type: int) -> list[int]:
    """Residuals of one row for one predictor.

    The residual alphabet is modular: every residual is reduced into
    [0, modulus) so the entropy coder sees a bounded alphabet.
    """
    n = len(row)
    residuals = [0] * n
    for i in range(n):
        x = i // channels
        left = row[i - channels] if x > 0 else 0
        up = prev_row[i] if prev_row is not None else 0
        upper_left = prev_row[i - channels] if (prev_row is not None and x > 0) else 0
        slice_val = slice_row[i] if slice_row is not None else 0
        pred = predict_value(pred_type, left, up, upper_left, slice_val,
                             modulus)
        residuals[i] = (row[i] - pred) % modulus
    return residuals


def residual_score(residuals: list[int], modulus: int) -> int:
    """Circular distance of the residuals from zero.

    The residual alphabet is modular, so a residual of `modulus - 1` is as
    close to zero as a residual of 1.
    """
    score = 0
    for r in residuals:
        score += r if r <= modulus - r else modulus - r
    return score


def reconstruct_row(residuals: list[int], prev_row: list[int] | None,
                    slice_row: list[int] | None, channels: int, modulus: int,
                    pred_type: int) -> list[int]:
    """Rebuild one row of canonical samples from its residuals.

    The reconstruction is always exact: the near-lossless mode quantizes
    the samples *before* the prediction (in the sample domain), so the
    residuals that reach the decoder describe the quantized values
    faithfully.
    """
    n = len(residuals)
    row = [0] * n
    for i in range(n):
        x = i // channels
        left = row[i - channels] if x > 0 else 0
        up = prev_row[i] if prev_row is not None else 0
        upper_left = prev_row[i - channels] if (prev_row is not None and x > 0) else 0
        slice_val = slice_row[i] if slice_row is not None else 0
        pred = predict_value(pred_type, left, up, upper_left, slice_val, modulus)
        row[i] = (residuals[i] + pred) % modulus
    return row


def best_row_predictor(row: list[int], prev_row: list[int] | None,
                       slice_row: list[int] | None, channels: int,
                       modulus: int, candidates: tuple[int, ...]) -> tuple[int, list[int]]:
    """Pick the cheapest predictor for one row.

    Returns (predictor id, residuals). Ties keep the first candidate, so the
    choice is deterministic across implementations.
    """
    best_pred = candidates[0]
    best_res = None
    best_score = -1
    for pred_type in candidates:
        res = row_residuals(row, prev_row, slice_row, channels, modulus, pred_type)
        score = residual_score(res, modulus)
        if best_score < 0 or score < best_score:
            best_score = score
            best_res = res
            best_pred = pred_type
    assert best_res is not None
    return best_pred, best_res


def pack_filter_bits(pred_types: list[int]) -> bytes:
    """Pack one predictor id per row (4 bits, MSB first)."""
    out = bytearray()
    acc = 0
    nbits = 0
    for p in pred_types:
        acc = (acc << FILTER_BITS_PER_ROW) | (p & 0xF)
        nbits += FILTER_BITS_PER_ROW
        if nbits >= 8:
            out.append((acc >> (nbits - 8)) & 0xFF)
            nbits -= 8
            acc &= (1 << nbits) - 1
    if nbits:
        out.append((acc << (8 - nbits)) & 0xFF)
    return bytes(out)


def unpack_filter_bits(data: bytes, rows: int) -> list[int]:
    """Inverse of :func:`pack_filter_bits`."""
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
        out.append((acc >> (nbits - FILTER_BITS_PER_ROW)) & 0xF)
        nbits -= FILTER_BITS_PER_ROW
        acc &= (1 << nbits) - 1
    return out


def split_planes(residuals: list[int], sample_bytes: int) -> bytes:
    """Split packed residuals into byte planes (low plane first).

    Multi-byte residuals are usually small, so the high planes turn into
    long runs of zero bytes, which the entropy coder handles very well.
    """
    if sample_bytes == 1:
        return bytes(residuals)
    out = bytearray()
    for plane in range(sample_bytes):
        shift = 8 * plane
        out += bytes((r >> shift) & 0xFF for r in residuals)
    return bytes(out)


def merge_planes(data: bytes, count: int, sample_bytes: int) -> list[int]:
    """Inverse of :func:`split_planes`."""
    if sample_bytes == 1:
        return list(data[:count])
    out = [0] * count
    for plane in range(sample_bytes):
        base = plane * count
        shift = 8 * plane
        for i in range(count):
            out[i] |= data[base + i] << shift
    return out


def pack_residuals(residuals: list[int], sample_bytes: int) -> bytes:
    """Pack residuals little-endian at the exact sample width."""
    out = bytearray()
    for r in residuals:
        out += r.to_bytes(sample_bytes, "little")
    return bytes(out)


def unpack_residuals(data: bytes, count: int, sample_bytes: int) -> list[int]:
    """Inverse of :func:`pack_residuals`."""
    out = []
    for i in range(count):
        chunk = data[i * sample_bytes:(i + 1) * sample_bytes]
        if len(chunk) < sample_bytes:
            raise ValueError("truncated residual stream")
        out.append(int.from_bytes(chunk, "little"))
    return out


__all__ = [
    "paeth", "median", "med_predict", "circular_distance", "predict_value",
    "candidate_list", "row_residuals",
    "residual_score", "best_row_predictor", "pack_filter_bits",
    "unpack_filter_bits", "split_planes", "merge_planes", "pack_residuals",
    "unpack_residuals", "reconstruct_row",
    "PRED_COUNT",
]
