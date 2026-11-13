"""LZ77 token stream used by the PYRM container.

The token alphabet is deliberately tiny so that the adaptive entropy coder
stays fast:

=====  =========================================================
0-255  literal byte
256    match, followed by three extra symbols
=====  =========================================================

The three extra symbols of a match are `len_extra`, `dist_hi`, `dist_lo`:

* length   = ``LZ_MIN_MATCH + len_extra``  (4 .. 259)
* distance = ``dist_hi * 256 + dist_lo + 1`` (1 .. 65536)

Symbol 257 is the end-of-stream marker.

The matcher is greedy with a 3 byte hash chain. Candidates are inspected
newest first and a later candidate only wins when it is *strictly* longer,
so the newest of equally long matches is kept. That rule is implemented
identically by the Rust core, which makes both paths byte-exact.
"""

from .constants import (LZ_MAX_DIST, LZ_MAX_MATCH, LZ_MIN_MATCH,
                        LZ_TOKEN_EOF, LZ_TOKEN_MATCH, LZ_TOKEN_SYMBOLS,
                        CorruptedFileError)

#: How many positions are remembered per 3 byte hash key.
CHAIN_LIMIT = 16

#: Size of the fixed hash table (a power of two).
LZ_HASH_BITS = 16
LZ_HASH_SIZE = 1 << LZ_HASH_BITS


def _lz_hash(key: int) -> int:
    """Fibonacci hashing of a 24 bit key into the fixed hash table."""
    return ((key * 2654435761) & 0xFFFFFFFF) >> (32 - LZ_HASH_BITS)


def lz_encode(data: bytes) -> list[int]:
    """Tokenise `data` into the LZ77 symbol stream (without the EOF marker).

    The matcher uses the classic two array layout: `heads` holds the most
    recent position per key hash, `prev` links a position to the previous
    one with the same hash. A fixed size table keeps the working set small
    and needs no allocation at all, which matters a lot on medical volumes
    (millions of distinct keys).
    """
    n = len(data)
    out: list[int] = []
    heads = [-1] * LZ_HASH_SIZE
    prev = [-1] * (n + 1)
    i = 0
    while i < n:
        advance = 1
        if i + LZ_MIN_MATCH <= n:
            key = data[i] | (data[i + 1] << 8) | (data[i + 2] << 16)
            cand = heads[_lz_hash(key)]
            best_len = 0
            best_dist = 0
            limit = LZ_MAX_MATCH if LZ_MAX_MATCH < n - i else n - i
            chain = 0
            while cand >= 0 and chain < CHAIN_LIMIT:
                dist = i - cand
                if dist == 0 or dist > LZ_MAX_DIST:
                    break
                length = 0
                while length < limit and data[cand + length] == data[i + length]:
                    length += 1
                if length > best_len:
                    best_len = length
                    best_dist = dist
                    if best_len == limit:
                        break
                cand = prev[cand]
                chain += 1
            if best_len >= LZ_MIN_MATCH:
                out.append(LZ_TOKEN_MATCH)
                out.append(best_len - LZ_MIN_MATCH)
                out.append((best_dist - 1) >> 8)
                out.append((best_dist - 1) & 0xFF)
                advance = best_len
            else:
                out.append(data[i])
        else:
            out.append(data[i])
        # remember every position covered by this step
        stop = i + advance
        if stop > n - LZ_MIN_MATCH + 1:
            stop = n - LZ_MIN_MATCH + 1
        for j in range(i, stop):
            key = data[j] | (data[j + 1] << 8) | (data[j + 2] << 16)
            h = _lz_hash(key)
            prev[j] = heads[h]
            heads[h] = j
        i += advance
    return out


def lz_decode(symbols: list[int], target_len: int | None = None,
              max_len: int | None = None) -> bytes:
    """Rebuild the byte stream from an LZ77 symbol list.

    `target_len` is the exact expected size of the result. When it is None
    the stream is decoded until the end-of-stream marker and `max_len` bounds
    the result (used by the escape coding, where the length of the symbol
    stream is only known while it is being decoded).
    """
    out = bytearray()
    pos = 0
    n = len(symbols)
    limit = max_len if max_len is not None else (target_len or 0)
    while pos < n:
        sym = symbols[pos]
        pos += 1
        if sym == LZ_TOKEN_EOF:
            break
        if sym < 256:
            if target_len is not None and len(out) >= target_len:
                raise CorruptedFileError("LZ literal overshoots target length")
            if len(out) >= limit:
                raise CorruptedFileError("LZ output exceeds the length limit")
            out.append(sym)
            continue
        if sym != LZ_TOKEN_MATCH:
            raise CorruptedFileError(f"invalid LZ token symbol {sym}")
        if pos + 3 > n:
            raise CorruptedFileError("truncated LZ match")
        len_extra = symbols[pos]
        dist_hi = symbols[pos + 1]
        dist_lo = symbols[pos + 2]
        pos += 3
        if len_extra > 255 or dist_hi > 255 or dist_lo > 255:
            raise CorruptedFileError("malformed LZ match")
        length = LZ_MIN_MATCH + len_extra
        dist = dist_hi * 256 + dist_lo + 1
        if dist > len(out):
            raise CorruptedFileError(f"invalid LZ back-reference distance {dist}")
        if target_len is not None and len(out) + length > target_len:
            raise CorruptedFileError("LZ match overshoots target length")
        if len(out) + length > limit:
            raise CorruptedFileError("LZ output exceeds the length limit")
        for _ in range(length):
            out.append(out[len(out) - dist])
    if target_len is not None and len(out) != target_len:
        raise CorruptedFileError("LZ output length mismatch")
    return bytes(out)


__all__ = ["lz_encode", "lz_decode", "LZ_TOKEN_SYMBOLS", "LZ_TOKEN_EOF"]
