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
CHAIN_LIMIT = 64


def lz_encode(data: bytes) -> list[int]:
    """Tokenise `data` into the LZ77 symbol stream (without the EOF marker)."""
    n = len(data)
    out: list[int] = []
    table: dict[bytes, list[int]] = {}
    i = 0
    while i < n:
        advance = 1
        if i + LZ_MIN_MATCH <= n:
            key = data[i:i + 3]
            chain = table.get(key)
            best_len = 0
            best_dist = 0
            if chain:
                limit = LZ_MAX_MATCH if LZ_MAX_MATCH < n - i else n - i
                for pos in reversed(chain):
                    dist = i - pos
                    if dist > LZ_MAX_DIST:
                        break
                    length = 0
                    while length < limit and data[pos + length] == data[i + length]:
                        length += 1
                    if length > best_len:
                        best_len = length
                        best_dist = dist
                        if best_len == limit:
                            break
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
            key = data[j:j + 3]
            chain = table.get(key)
            if chain is None:
                table[key] = [j]
            else:
                chain.append(j)
                if len(chain) > CHAIN_LIMIT:
                    del chain[0]
        i += advance
    return out


def lz_decode(symbols: list[int], target_len: int) -> bytes:
    """Rebuild the byte stream from an LZ77 symbol list.

    `target_len` is the exact expected size of the result.
    """
    out = bytearray()
    pos = 0
    n = len(symbols)
    while pos < n:
        sym = symbols[pos]
        pos += 1
        if sym == LZ_TOKEN_EOF:
            break
        if sym < 256:
            if len(out) >= target_len:
                raise CorruptedFileError("LZ literal overshoots target length")
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
        if len(out) + length > target_len:
            raise CorruptedFileError("LZ match overshoots target length")
        for _ in range(length):
            out.append(out[len(out) - dist])
    if len(out) != target_len:
        raise CorruptedFileError("LZ output length mismatch")
    return bytes(out)


__all__ = ["lz_encode", "lz_decode", "LZ_TOKEN_SYMBOLS", "LZ_TOKEN_EOF"]
