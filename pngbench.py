"""Minimal stdlib-only PNG encoder, used by the benchmark to compare PYRA
against real PNG output for the *same* pixel data.

Note on fairness: PNG compresses with zlib's C-optimized DEFLATE plus
per-row adaptive filtering (we emulate a practical encoder by always
applying the Paeth filter). PYRA uses a hand-rolled adaptive range coder
in pure Python, so speed comparisons are inherently asymmetric -- only
compression-size comparison is meaningful, and even that favors PNG.
"""

import struct
import zlib

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _filter_image(raw: bytes, width: int, height: int, stride: int, bpp: int,
                  filter_choice: str = "paeth") -> bytes:
    """Apply the chosen PNG per-row filter. filter_choice in
    {"none", "up", "paeth", "adaptive"}; adaptive picks the best of
    none/sub/up/paeth per row by sum-of-|residual| (classic heuristic)."""
    import operator
    filtered = bytearray()
    zero = bytes(stride)
    for y in range(height):
        cur = raw[y * stride:(y + 1) * stride]
        prev = raw[(y - 1) * stride:y * stride] if y else zero
        variants: list[tuple[int, bytearray]] = []

        def build(kind: int) -> bytearray:
            out = bytearray([kind])
            if kind == 0:                      # none
                out += cur
            elif kind == 1:                    # sub
                for i in range(stride):
                    a = cur[i - bpp] if i >= bpp else 0
                    out.append((cur[i] - a) & 0xFF)
            elif kind == 2:                    # up
                for i in range(stride):
                    out.append((cur[i] - prev[i]) & 0xFF)
            elif kind == 3:                    # average
                for i in range(stride):
                    a = cur[i - bpp] if i >= bpp else 0
                    out.append((cur[i] - ((a + prev[i]) >> 1)) & 0xFF)
            else:                              # paeth
                for i in range(stride):
                    a = cur[i - bpp] if i >= bpp else 0
                    c = prev[i - bpp] if i >= bpp else 0
                    out.append((cur[i] - _paeth(a, prev[i], c)) & 0xFF)
            return out

        if filter_choice == "adaptive":
            best_kind, best_score = 0, None
            for kind in (0, 1, 2, 4):
                v = build(kind)
                score = sum(b if b < 128 else 256 - b for b in v[1:])
                if best_score is None or score < best_score:
                    best_kind, best_score = kind, score
                    best_row = v
            filtered += bytes(best_row)
        elif filter_choice == "none":
            filtered += build(0)
        elif filter_choice == "up":
            filtered += build(2)
        else:
            filtered += build(4)
    return bytes(filtered)


def encode_png(pixels: list[int], width: int, height: int, bit_depth: int = 8,
               color_space: int = 2, filter_choice: str = "adaptive") -> bytes:
    """Encode raw pixels (flat channel-major list) into a valid PNG byte string.
    color_space: PYRA numbering (1=gray, 2=rgb, 3=rgba).
    """
    assert bit_depth in (8, 16)
    channels = {1: 1, 2: 3, 3: 4}[color_space]
    png_color_type = {1: 0, 3: 2, 4: 6}[channels]

    if channels not in (1, 3, 4):
        raise ValueError(f"unsupported PNG channels: {channels}")

    bytes_per_sample = bit_depth // 8
    if bit_depth == 8:
        raw = bytes(pixels)
    else:
        raw = struct.pack(f">{len(pixels)}H", *pixels)

    stride = width * channels * bytes_per_sample
    bpp = channels * bytes_per_sample
    filtered = _filter_image(raw, width, height, stride, bpp, filter_choice)

    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, png_color_type, 0, 0, 0)
    return (_PNG_SIG
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(filtered, 9))
            + _chunk(b"IEND", b""))
