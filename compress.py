# PYRA - RLE + LZ compression stages (pure Python, stdlib only).
#
# Pipeline: residuals -> pack(byte stream) -> RLE -> LZ77 -> tokens -> entropy
#
# Design notes:
# - Byte-oriented intermediate representation (fixed-width packing of
#   residual symbols) lets RLE and LZ77 operate uniformly for any bit depth.
# - RLE rules (byte level, unambiguous, reversible):
#     (b)             literal byte b, b not in {0xFF, 0xFE}
#     (0xFF, c>=1)    run of 0xFF, length c in [1,255]
#     (0xFF, 0)       literal 0xFF  (decoder support; encoder emits (0xFF,1))
#     (0xFE, 0)       literal 0xFE
#     (0xFE, m>=1, L) run of byte (m-1), length L in [1,255]
#                     (covers zero-runs via m=1 and 0xFE-runs via m=0xFF)
# - LZ77 tokens (symbol level, alphabet 258):
#     0..255    literal byte
#     256       MATCH: followed by 3 symbols (len-4, dist0_lo, dist0_hi);
#               distance in [1, 65535], match length in [4, 255]
#     257       EOF: token stream terminator
# The decoder consumes tokens lazily until EOF, then verifies the target
# byte count was produced exactly; the encoder never overshoots the target.
# Every non-EOF token produces at least one output byte (a MATCH emits
# >= 4 bytes), so valid streams contain at most target_len + 1 symbols;
# the decoder enforces that cap against infinite/decompression-bomb streams.

from .constants import CorruptedFileError

# Token alphabet
TOKEN_MATCH = 256
TOKEN_EOF = 257
TOKEN_SYMBOLS = 258          # 0..255 literal bytes, 256 MATCH, 257 EOF

MINMATCH = 4
MAXMATCH = MINMATCH + 251    # 255 (len byte holds L - MINMATCH in [0,251])
MAXDIST = 65535
_MAXRUN = 255

# ---------------------------------------------------------------- packing

def pack_residuals(residuals: list[int], mod: int) -> bytes:
    """Pack residual symbols (each in [0, mod)) into a byte stream with a
    fixed little-endian width of ceil(bits) bytes per symbol."""
    width = (mod.bit_length() + 7) // 8
    out = bytearray()
    for r in residuals:
        out.extend(r.to_bytes(width, "little"))
    return bytes(out)


def unpack_residuals(data: bytes, mod: int, count: int) -> list[int]:
    """Inverse of pack_residuals for exactly `count` symbols."""
    width = (mod.bit_length() + 7) // 8
    if len(data) < count * width:
        raise CorruptedFileError("truncated packed residual stream")
    out = []
    for i in range(count):
        v = int.from_bytes(data[i * width:(i + 1) * width], "little")
        if v >= mod:
            raise CorruptedFileError(f"residual symbol out of range: {v}")
        out.append(v)
    return out

# ---------------------------------------------------------------- RLE

def rle_encode(data: bytes) -> bytes:
    """Byte-level RLE. Greedy maximal-run policy, deterministic."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        j = i
        while j < n and data[j] == b and (j - i) < _MAXRUN:
            j += 1
        L = j - i
        if b == 0xFF:
            out.append(0xFF)
            out.append(L)
            i = j
        elif b == 0xFE:
            if L == 1:
                out.append(0xFE)
                out.append(0x00)
            else:
                # run of FE through the general run rule: m = B+1 = 0xFF
                out.append(0xFE)
                out.append(0xFF)
                out.append(L)
            i = j
        else:
            if L >= 3:
                # general run: marker byte encodes B+1 (so 0-byte runs use m=1)
                out.append(0xFE)
                out.append(b + 1)
                out.append(L)
                i = j
            else:
                out.append(b)
                i += 1
    return bytes(out)


def rle_decode(data: bytes, target_len: int) -> bytes:
    """Inverse of rle_encode; reconstructs exactly target_len bytes.
    Any inconsistency raises CorruptedFileError."""
    if target_len < 0:
        raise CorruptedFileError("negative RLE target length")
    out = bytearray()
    i = 0
    n = len(data)
    while len(out) < target_len:
        if i >= n:
            raise CorruptedFileError("truncated RLE stream")
        b = data[i]
        i += 1
        if b not in (0xFF, 0xFE):
            out.append(b)
        elif b == 0xFF:
            if i >= n:
                raise CorruptedFileError("truncated RLE escape")
            c = data[i]
            i += 1
            if c == 0:
                out.append(0xFF)
            else:
                if len(out) + c > target_len:
                    raise CorruptedFileError("RLE run overshoots target length")
                out.extend(bytes([0xFF]) * c)
        else:  # 0xFE
            if i >= n:
                raise CorruptedFileError("truncated RLE escape")
            m = data[i]
            i += 1
            if m == 0:
                out.append(0xFE)
            else:
                if i >= n:
                    raise CorruptedFileError("truncated RLE run")
                L = data[i]
                i += 1
                if L == 0:
                    raise CorruptedFileError("RLE run length 0")
                B = m - 1
                if len(out) + L > target_len:
                    raise CorruptedFileError("RLE run overshoots target length")
                out.extend(bytes([B]) * L)
    if len(out) != target_len:
        raise CorruptedFileError("RLE output length mismatch")
    return bytes(out)

# ---------------------------------------------------------------- LZ77

def lz_encode(data: bytes) -> list[int]:
    """Byte stream -> flat LZ77 token symbol list.

    Greedy longest-match with hash buckets. Ties (equal length) resolve to
    the most recent candidate (smallest distance) => deterministic."""
    out: list[int] = []
    i = 0
    n = len(data)
    if n == 0:
        return [TOKEN_EOF]

    # 3-byte key -> most recent positions (newest first), bounded depth
    table: dict[int, list[int]] = {}
    BUCKET_MAX = 32

    def bucket_for(key: int):
        positions = table.get(key)
        if positions is None:
            positions = table[key] = []
        return positions

    def add_key(pos: int):
        if pos + 3 <= n:
            key = data[pos] | (data[pos + 1] << 8) | (data[pos + 2] << 16)
            positions = bucket_for(key)
            positions.append(pos)
            if len(positions) > BUCKET_MAX:
                positions.pop(0)

    while i < n:
        best_len = 0
        best_dist = 0
        if i + MINMATCH <= n:
            key = data[i] | (data[i + 1] << 8) | (data[i + 2] << 16)
            positions = table.get(key)
            if positions:
                max_len = min(n - i, MAXMATCH)
                for cand in reversed(positions):
                    dist = i - cand
                    if dist == 0 or dist > MAXDIST:
                        break
                    # verify match; self-overlap (cand+L >= i) is allowed and
                    # handled identically by the decoder's -dist copy
                    L = 0
                    while L < max_len and data[cand + L] == data[i + L]:
                        L += 1
                    if L > best_len:
                        best_len = L
                        best_dist = dist
                        if L >= max_len:
                            break
        if best_len >= MINMATCH:
            out.append(TOKEN_MATCH)
            out.append(best_len - MINMATCH)
            # distance is stored zero-based: decoder computes dist = 1 + value
            d0 = best_dist - 1
            out.append(d0 & 0xFF)
            out.append(d0 >> 8)
            step = best_len
            # index skipped positions for future matches
            for k in range(i, i + step):
                add_key(k)
            i += step
        else:
            out.append(data[i])
            add_key(i)
            i += 1
    out.append(TOKEN_EOF)
    return out


def lz_decode(src, target_len: int) -> bytes:
    """Decode LZ77 tokens lazily from symbol generator `src` until the EOF
    token. src must be callable returning the next token symbol (0..255
    literal, 256 MATCH, 257 EOF) or raising beyond the end. Verifies the
    produced byte count equals target_len exactly; every non-EOF token
    produces >= 1 byte, so the symbol cap target_len + 1 guards against
    endless/degenerate streams (e.g. zero-padding from a truncated file)."""
    if target_len < 0:
        raise CorruptedFileError("negative LZ target length")
    out = bytearray()
    symbols = 0
    cap = target_len + 1
    while True:
        try:
            sym = src()
        except (StopIteration, IndexError):
            raise CorruptedFileError("truncated LZ token stream")
        symbols += 1
        if symbols > cap:
            raise CorruptedFileError("LZ token stream exceeds target length")
        if sym == TOKEN_EOF:
            break
        if 0 <= sym < TOKEN_MATCH:
            if len(out) >= target_len:
                raise CorruptedFileError("LZ literal overshoots target length")
            out.append(sym)
            continue
        if sym != TOKEN_MATCH:
            raise CorruptedFileError(f"invalid LZ token symbol {sym}")
        # MATCH: 3 extra symbols
        extra = []
        for _ in range(3):
            try:
                s = src()
            except (StopIteration, IndexError):
                raise CorruptedFileError("truncated LZ match tokens")
            if s < 0 or s > 255:
                raise CorruptedFileError("malformed LZ match extra symbol")
            extra.append(s)
            symbols += 1
        L = extra[0] + MINMATCH
        d0 = extra[1] | (extra[2] << 8)
        dist = 1 + d0
        if dist > len(out):
            raise CorruptedFileError(f"invalid LZ back-reference distance {dist}")
        if L + len(out) > target_len:
            raise CorruptedFileError("LZ match overshoots target length")
        for _ in range(L):
            out.append(out[-dist])
    if len(out) != target_len:
        raise CorruptedFileError("LZ output length mismatch")
    return bytes(out)
