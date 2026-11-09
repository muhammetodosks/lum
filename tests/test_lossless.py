# PYRA lossless roundtrip / determinism / corruption / security test suite.
# Run:  python3 -m pyra.tests.test_lossless
#
# Every lossless path must guarantee decoded == original, bit-exact.

import os
import random
import struct
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pyra import *
from pyra.codec import PyraCodec
from pyra.container import PyraContainer
from pyra.security import SecurityError, CorruptedFileError

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, ok: bool):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL {name}")


def make_pixels(kind: str, w: int, h: int, ch: int, bit_depth: int) -> list[int]:
    top = (1 << bit_depth) - 1
    rnd = random.Random(1337)
    if kind == "solid":
        return [top // 3] * (w * h * ch)
    if kind == "gradient":
        span = w if w > 1 else 2
        return [(x * top // span) for y in range(h) for x in range(w) for _ in range(ch)]
    if kind == "noise":
        return [rnd.randint(0, top) for _ in range(w * h * ch)]
    if kind == "photo":
        # smooth blobs +量化 noise mix
        px = []
        for y in range(h):
            for x in range(w):
                base = (x * 7 + y * 13) % (top + 1)
                for c in range(ch):
                    px.append(min(top, max(0, base + rnd.randint(-8, 8))))
        return px
    raise ValueError(kind)


# ---------------------------------------------------------------- roundtrips
def test_roundtrips():
    rt = tempfile.mktemp(prefix="pyra_rt_")
    for_w, for_h = 40, 30
    for cs, ch in [(CS_GRAY, 1), (CS_RGB, 3), (CS_RGBA, 4)]:
        for bd in (8, 16):
            for kind in ("solid", "gradient", "noise", "photo"):
                px = make_pixels(kind, for_w, for_h, ch, bd)
                pred_ok = []
                for pred in (PRED_NONE, PRED_LEFT, PRED_UP, PRED_AVERAGE,
                             PRED_PAETH, PRED_GRADIENT, PRED_MEDIAN):
                    fb = PyraCodec.encode(px, for_w, for_h, bd, cs, pred, 16)
                    _, dec = PyraCodec.decode(fb)
                    pred_ok.append(dec == px)
                check(f"roundtrip {kind} cs={cs} bd={bd} x7preds", all(pred_ok))
    # multi-tile (odd tile split)
    px = make_pixels("photo", 37, 23, 3, 8)
    ok = True
    for ts in (1, 4, 8, 37, 64):
        fb = PyraCodec.encode(px, 37, 23, 8, CS_RGB, PRED_PAETH, ts)
        _, dec = PyraCodec.decode(fb)
        ok = ok and dec == px
    check("roundtrip multi-tile tile_sizes", ok)
    if os.path.exists(rt):
        os.unlink(rt)


def test_determinism():
    px = make_pixels("photo", 64, 48, 3, 8)
    ok = True
    for pred in (PRED_NONE, PRED_LEFT, PRED_UP, PRED_AVERAGE,
                 PRED_PAETH, PRED_GRADIENT, PRED_MEDIAN):
        a = PyraCodec.encode(px, 64, 48, 8, CS_RGB, pred)
        b = PyraCodec.encode(px, 64, 48, 8, CS_RGB, pred)
        ok = ok and a == b
    check("deterministic encode (same input -> same bytes, x7 preds)", ok)


def test_container():
    px = make_pixels("photo", 32, 32, 3, 8)
    fb = PyraCodec.encode(px, 32, 32, 8, CS_RGB, PRED_PAETH, 16)
    check("magic bytes", fb.startswith(MAGIC))
    check("header version", struct.unpack(">I", fb[8:12])[0] == VERSION)
    meta, tiles = PyraContainer.unpack_file(fb)
    check("meta roundtrip",
          meta["width"] == 32 and meta["height"] == 32 and meta["bit_depth"] == 8)
    # CRC corruption must be caught (mutate payload byte)
    corrupt = bytearray(fb)
    corrupt[len(fb) // 2] ^= 0xFF
    try:
        PyraContainer.unpack_file(bytes(corrupt))
        check("CRC mismatch raises CorruptedFileError", False)
    except CorruptedFileError:
        check("CRC mismatch raises CorruptedFileError", True)
    # truncation caught
    try:
        PyraContainer.unpack_file(fb[:len(fb) // 2])
        check("truncation raises CorruptedFileError", False)
    except (CorruptedFileError, IndexError):
        check("truncation raises CorruptedFileError", True)
    # bad magic caught
    try:
        PyraContainer.unpack_file(b"NOPE" + fb[4:])
        check("bad magic raises CorruptedFileError", False)
    except CorruptedFileError:
        check("bad magic raises CorruptedFileError", True)


def test_security():
    # bomb dimensions
    for w, h in [(0, 1), (1, 0), (1 << 17, 1), (1, 1 << 17), (16385, 16385)]:
        try:
            PyraContainer.pack_file(w, h, 8, CS_RGB, PRED_PAETH, METHOD_RAW, 256, [b""])
            check(f"dimensions {w}x{h} rejected", False)
        except SecurityError:
            check(f"dimensions {w}x{h} rejected", True)
    # oversize chunk
    try:
        PyraContainer.pack_file(4, 4, 8, CS_RGB, PRED_PAETH, METHOD_RAW, 256,
                                [b"x" * (600 * 1024 * 1024)])
        check("512MB chunk limit", False)
    except (SecurityError, MemoryError):
        check("512MB chunk limit", True)


def test_region_and_tiles():
    w, h, ch = 48, 32, 3
    px = make_pixels("photo", w, h, ch, 8)
    fb = PyraCodec.encode(px, w, h, 8, CS_RGB, PRED_PAETH, 16)
    # full region
    _, full = decode_region(fb, 0, 0, w, h)
    check("decode_region full == image", full == px)
    # sub region
    rx, ry, rw, rh = 10, 5, 20, 12
    _, reg = decode_region(fb, rx, ry, rw, rh)
    ok = True
    for y in range(rh):
        for x in range(rw):
            src = ((ry + y) * w + (rx + x)) * ch
            dst = (y * rw + x) * ch
            if reg[dst:dst + ch] != px[src:src + ch]:
                ok = False
    check("decode_region sub-region exact", ok)
    # iter_tiles covers all pixels exactly once
    got = [0] * (w * h * ch)
    stamps = [0] * (w * h * ch)
    for i, (tmeta, tpix) in enumerate(iter_tiles(fb)):
        t_x, t_y = tmeta["x"], tmeta["y"]
        t_w, t_h = tmeta["width"], tmeta["height"]
        for y in range(t_h):
            src = (y * t_w) * ch
            dst = ((t_y + y) * w + t_x) * ch
            got[dst:dst + t_w * ch] = tpix[src:src + t_w * ch]
            for k in range(t_w * ch):
                stamps[dst + k] += 1
    check("iter_tiles assembles exact image", got == px)
    check("iter_tiles no overlap/gap", all(s == 1 for s in stamps))


def test_files_and_cli_io():
    tmp = tempfile.mkdtemp(prefix="pyra_files_")
    # 8-bit PPM roundtrip through .pyra
    ppm = os.path.join(tmp, "a.ppm")
    py1 = os.path.join(tmp, "a.pyra")
    py2 = os.path.join(tmp, "b.pyra")
    out = os.path.join(tmp, "out.ppm")
    w, h = 24, 18
    px = make_pixels("photo", w, h, 3, 8)
    with open(ppm, "wb") as f:
        f.write(f"P6\n{w} {h}\n255\n".encode() + bytes(px))
    m1 = encode_file(ppm, py1)
    decode_file(py1, py2)
    with open(ppm, "rb") as f:
        orig = f.read()
    with open(py2, "rb") as f:
        redone = f.read()
    check("PPM->pyra->PPM byte-identical", orig == redone)
    check("encode_file meta", m1["width"] == w and m1["height"] == h)
    # 16-bit PPM roundtrip
    ppm16 = os.path.join(tmp, "a16.ppm")
    pya16 = os.path.join(tmp, "a16.pyra")
    out16 = os.path.join(tmp, "out16.ppm")
    px16 = make_pixels("noise", w, h, 3, 16)
    body = struct.pack(f">{w*h*3}H", *px16)
    with open(ppm16, "wb") as f:
        f.write(f"P6\n{w} {h}\n65535\n".encode() + body)
    encode_file(ppm16, pya16)
    decode_file(pya16, out16)
    with open(ppm16, "rb") as f:
        orig16 = f.read()
    with open(out16, "rb") as f:
        redone16 = f.read()
    check("16-bit PPM->pyra->PPM byte-identical", orig16 == redone16)
    # PGM gray roundtrip
    pgm = os.path.join(tmp, "g.pgm")
    pyg = os.path.join(tmp, "g.pyra")
    outg = os.path.join(tmp, "gout.pgm")
    pxg = make_pixels("gradient", w, h, 1, 8)
    with open(pgm, "wb") as f:
        f.write(f"P5\n{w} {h}\n255\n".encode() + bytes(pxg))
    encode_file(pgm, pyg)
    decode_file(pyg, outg)
    with open(pgm, "rb") as f:
        o = f.read()
    with open(outg, "rb") as f:
        r = f.read()
    check("PGM->pyra->PGM byte-identical", o == r)


def test_compression_sanity():
    # gradient compresses, noise doesn't expand absurdly, solid compresses hard
    w, h = 32, 32
    grad = make_pixels("gradient", w, h, 3, 8)
    noise = make_pixels("noise", w, h, 3, 8)
    solid = make_pixels("solid", w, h, 3, 8)
    raw = w * h * 3
    g_fb = PyraCodec.encode(grad, w, h, 8, CS_RGB, PRED_LEFT, 0xFFFF)
    n_fb = PyraCodec.encode(noise, w, h, 8, CS_RGB, PRED_NONE, 0xFFFF)
    s_fb = PyraCodec.encode(solid, w, h, 8, CS_RGB, PRED_LEFT, 0xFFFF)
    check(f"solid compresses <25% of raw ({len(s_fb)}/{raw})", len(s_fb) < raw // 4)
    check(f"gradient compresses below raw ({len(g_fb)}/{raw})", len(g_fb) < raw)
    check(f"noise expansion bounded (<2.2x) ({len(n_fb)}/{raw})", len(n_fb) < raw * 2.2)


def test_animation():
    w, h = 32, 32
    frames = [make_pixels(k, w, h, 3, 8) for k in ("solid", "gradient", "noise")]
    anim = encode_animation(frames, w, h, fps=30, bit_depth=8, color_space=CS_RGB,
                            pred_type=PRED_PAETH, tile_size=16)
    n, fps = struct.unpack(">II", anim[:8])
    check("animation frame count", n == 3 and fps == 30)
    ok = True
    body = anim[8:]
    # Each frame is a complete PYRA file; its length is discoverable from the
    # header: header + index_size_field + index + tiles + trailing CRC32.
    hdr_size = struct.calcsize(">8sIIIBBBBII")
    pos = 0
    for i, frame in enumerate(frames):
        magic, version, fw, fh, bd, cs, pd, mm, ts, nt = struct.unpack(
            ">8sIIIBBBBII", body[pos:pos + hdr_size])
        # tile offsets are absolute within this frame's file
        index_start = pos + hdr_size + 4
        last_off, last_len = struct.unpack(
            ">II", body[index_start + (nt - 1) * 8:index_start + (nt - 1) * 8 + 8])
        flen = last_off + last_len + 4
        fbytes = body[pos:pos + flen]
        _, dec = PyraCodec.decode(fbytes)
        ok = ok and dec == frame
        pos += flen
    check("animation frames roundtrip exactly", ok and pos == len(body))


# ------------------------------------------------- compress stages + methods
def test_compress_methods():
    from pyra.compress import (pack_residuals, unpack_residuals, rle_encode,
                               rle_decode, lz_encode, lz_decode, TOKEN_EOF,
                               TOKEN_MATCH, CorruptedFileError as CFE)

    # ---- RLE stage unit tests
    for data in (b"", b"\x00", b"\xff", b"\xfe", b"abcdefgh",
                 b"\x00" * 300, b"\xff" * 300, b"\xfe" * 300,
                 bytes(range(256)) * 2, b"\xfe\xfe\xfe" + b"ab\xfe"):
        out = rle_decode(rle_encode(data), len(data))
        check(f"RLE roundtrip ({len(data)} bytes, 0x{data[:1].hex() if data else 'empty'})", out == data)

    # RLE adversarial corruption (decoder must never leak/crash)
    rnd = random.Random(77)
    bad = 0
    for _ in range(150):
        n = rnd.randrange(0, 64)
        stream = bytes(rnd.randrange(256) for _ in range(n))
        target = rnd.randrange(0, 100)
        try:
            out = rle_decode(stream, target)
            # only acceptable if it decodes exactly target bytes whose
            # re-encoding is consistent
            if rle_decode(rle_encode(out), len(out)) != out or len(out) != target:
                bad += 1
        except CFE:
            pass
    check(f"RLE adversarial corruption contained ({bad} leaks)", bad == 0)

    # ---- LZ77 stage unit tests
    streams = (b"", b"a", b"abc", b"aaaa", b"aaaaaaaaaaaa",
               bytes(range(256)) * 3, b"abcd" + b"x" * 600,
               bytes(rnd.randrange(4) for _ in range(4096)))
    for data in streams:
        toks = lz_encode(data)
        check(f"LZ EOF-terminated ({len(data)} bytes)", toks[-1] == TOKEN_EOF if data else toks == [TOKEN_EOF])
        out = lz_decode(iter(toks).__next__, len(data))
        check(f"LZ roundtrip ({len(data)} bytes)", out == data)

    # LZ decoder adversarial cases -> CorruptedFileError, never other errors
    cases = [
        ("no EOF", lambda: lz_decode(iter([1, 2, 3]).__next__, 5)),
        ("literal overshoot", lambda: lz_decode(iter([1] * 7 + [TOKEN_EOF]).__next__, 5)),
        ("EOF too early", lambda: lz_decode(iter([TOKEN_EOF]).__next__, 5)),
        ("bad symbol", lambda: lz_decode((lambda: 999), 5)),
        ("token cap (bomb)", lambda: lz_decode(iter([65] * 100 + [TOKEN_EOF]).__next__, 5)),
        ("bad match extra", lambda: lz_decode(iter([TOKEN_MATCH, 0, 300, 0]).__next__, 64)),
        ("distance > output", lambda: lz_decode(iter([1, TOKEN_MATCH, 0, 4, 0, TOKEN_EOF]).__next__, 64)),
        ("match overshoot", lambda: lz_decode(iter([1, TOKEN_MATCH, 250, 0, 0, TOKEN_EOF]).__next__, 5)),
        ("missing match extras", lambda: lz_decode(iter([TOKEN_MATCH, 0, 0]).__next__, 64)),
    ]
    for name, fn in cases:
        try:
            fn()
            check(f"LZ corrupt raises CFE: {name}", False)
        except CFE:
            check(f"LZ corrupt raises CFE: {name}", True)
        except Exception as e:
            check(f"LZ corrupt raises CFE: {name} (got {type(e).__name__}: {e})", False)

    # exact match-loop semantics (self-overlapping RLE with matches)
    data = b"aabbaabbaabb" * 10
    check("LZ self-overlap roundtrip", lz_decode(iter(lz_encode(data)).__next__, len(data)) == data)

    # ---- pack/unpack residual symbols
    for mod in (1 << 8, 1 << 11, 1 << 16, 1 << 19):
        syms = [rnd.randrange(mod) for _ in range(50)]
        check(f"pack/unpack mod={mod}", unpack_residuals(pack_residuals(syms, mod), mod, len(syms)) == syms)
    try:
        unpack_residuals(b"\x00\x01", 256, 2)
        check("unpack range guard", False)
    except CFE:
        check("unpack range guard", True)

    # ---- forced per-method tile roundtrips (all methods x image types)
    cases = [
        ("solid gray", CS_GRAY, 8, make_pixels("solid", 16, 16, 1, 8)),
        ("noise gray 16-bit", CS_GRAY, 16, make_pixels("noise", 10, 10, 1, 16)),
        ("solid RGB", CS_RGB, 8, make_pixels("solid", 24, 24, 3, 8)),
        ("gradient RGB", CS_RGB, 8, make_pixels("gradient", 16, 16, 3, 8)),
        ("noise RGBA", CS_RGBA, 8, make_pixels("noise", 12, 12, 4, 8)),
        ("photo RGBA", CS_RGBA, 8, make_pixels("photo", 14, 14, 4, 8)),
    ]
    for name, cs, depth, px in cases:
        nch = PyraCodec._channels_for_colorspace(cs)
        side = _side_for(len(px), nch)
        px = px[:side * side * nch]
        tiles = split_into_tiles(px, side, side, nch, 8)
        for m in (METHOD_RAW, METHOD_RLE, METHOD_LZ):
            comps = [PyraCodec.encode_tile(t, cs, PRED_PAETH, depth, m) for t in tiles]
            outs = [PyraCodec.decode_tile(c, t.width, t.height, cs, PRED_PAETH, depth, m)
                    for c, t in zip(comps, tiles)]
            # assemble pixels
            out = [0] * len(px)
            for tile, tp in zip(tiles, outs):
                for y in range(tile.height):
                    for x in range(tile.width):
                        si = (y * tile.width + x) * nch
                        di = ((tile.y + y) * side + tile.x + x) * nch
                        out[di:di + nch] = tp[si:si + nch]
            check(f"{name} forced method={m} roundtrip", out == px)

    # ---- global method selection + container validation
    solid = make_pixels("solid", 64, 64, 3, 8)
    data = PyraCodec.encode(solid, 64, 64, 8, CS_RGB, PRED_LEFT, 64)
    meta, out = PyraCodec.decode(data)
    check("meta contains method", "method" in meta)
    check("solid container picks RLE/LZ", meta["method"] in (METHOD_RLE, METHOD_LZ))
    check("solid full roundtrip via chosen method", out == solid)
    check(f"solid compressed hard ({len(data)} bytes)", len(data) < 64 * 64 * 3 // 4)

    # invalid method byte (crc recomputed) must raise CFE
    mangled = bytearray(data)
    mangled[23] = 9  # method field in ">8sIIIBBBBII": 8+4+4+4+1+1+1 = 23
    body = bytes(mangled[:-4])
    try:
        PyraContainer.unpack_file(body + zlib.crc32(body).to_bytes(4, "big"))
        check("container rejects invalid method", False)
    except CorruptedFileError:
        check("container rejects invalid method", True)

    # header height is a full 32-bit field now (old fmt packed it into a byte)
    tall = make_pixels("gradient", 16, 300, 3, 8)
    data_t = PyraCodec.encode(tall, 16, 300, 8, CS_RGB, PRED_NONE, 256)
    meta_t, out_t = PyraCodec.decode(data_t)
    check("height=300 roundtrip (header I-field fix)", meta_t["height"] == 300 and out_t == tall)

    # ---- forced-METHOD_LZ containers: corruption must raise CFE only
    base = PyraContainer.pack_file(16, 16, 8, CS_RGB, PRED_PAETH, METHOD_LZ, 16, [
        PyraCodec.encode_tile(t, CS_RGB, PRED_PAETH, 8, METHOD_LZ)
        for t in split_into_tiles(make_pixels("photo", 16, 16, 3, 8), 16, 16, 3, 16)
    ])
    base_meta, base_pixels = PyraCodec.decode(base)
    orig = make_pixels("photo", 16, 16, 3, 8)
    check("forced LZ container roundtrip", base_pixels == orig)
    bad = 0
    for _ in range(80):
        raw = bytearray(base)
        for _ in range(rnd.randrange(1, 6)):
            i = rnd.randrange(28, len(raw))
            raw[i] = rnd.randrange(256)
        corrupted = bytes(raw[:rnd.randrange(36, len(raw) + 1)])
        try:
            _, dec = PyraCodec.decode(corrupted)
            if dec != orig:
                bad += 1
        except CorruptedFileError:
            pass
    check(f"corrupted LZ containers contained ({bad} leaks)", bad == 0)


def _side_for(total: int, channels: int) -> int:
    import math
    return math.isqrt(total // channels)


def main():
    print("=== PYRA lossless test suite ===")
    test_roundtrips()
    test_determinism()
    test_container()
    test_security()
    test_region_and_tiles()
    test_files_and_cli_io()
    test_compression_sanity()
    test_animation()
    test_compress_methods()
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("Failures:")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
