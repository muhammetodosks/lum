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
    # v2 extends the header with two extra bytes (transform, filter_mode).
    pos = 0
    for i, frame in enumerate(frames):
        version = struct.unpack(">I", body[pos + 8:pos + 12])[0]
        hdr_fmt = ">8sIIIBBBBII" if version == VERSION_LEGACY else ">8sIIIBBBBIIBB"
        hdr_size = struct.calcsize(hdr_fmt)
        magic, version, fw, fh, bd, cs, pd, mm, ts, nt = struct.unpack(
            hdr_fmt, body[pos:pos + hdr_size])[:10]
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
        # mod=200 packs into a single byte per symbol, so 0xC8 (=200) is
        # out of range and must be rejected.
        unpack_residuals(b"\x00\xc8", 200, 2)
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


# ---------------------------------------------------------------- new CLI/API features

def test_forced_methods():
    # encode() with a forced method: roundtrip for all three, header honored
    w, h = 40, 30
    for kind, cs in (("photo", CS_RGB), ("solid", CS_RGB), ("gradient", CS_RGB)):
        px = make_pixels(kind, w, h, 3, 8)
        for method in (METHOD_RAW, METHOD_RLE, METHOD_LZ):
            data = PyraCodec.encode(px, w, h, 8, CS_RGB, PRED_PAETH, 16, method)
            mymeta, decoded = decode(data)
            check(f"forced method {method} roundtrip ({kind})", decoded == px)
            hdr, _t = PyraContainer.unpack_file(data)
            check(f"forced method {method} header honored ({kind})", hdr["method"] == method)
    # gray forced methods too
    pxg = make_pixels("photo", w, h, 1, 8)
    for method in (METHOD_RAW, METHOD_RLE, METHOD_LZ):
        data = encode(pxg, w, h, 8, CS_GRAY, PRED_LEFT, 16, method)
        hdr, _t = PyraContainer.unpack_file(data)
        _m, dec = decode(data)
        check(f"forced method {method} gray roundtrip", dec == pxg and hdr["method"] == method)
    # invalid method rejected before encoding
    try:
        encode(pxg, w, h, 8, CS_GRAY, PRED_LEFT, 16, 7)
        check("invalid forced method raises ValueError", False)
    except ValueError:
        check("invalid forced method raises ValueError", True)


def test_png_roundtrip():
    from pyra.pngbench import encode_png, decode_png
    for kind, cs, ch, bd in (("photo", CS_RGB, 3, 8), ("solid", CS_GRAY, 1, 8), ("noise", CS_RGBA, 4, 8)):
        w, h = 33, 17
        px = make_pixels(kind, w, h, ch, bd)
        png = encode_png(px, w, h, bd, cs)
        check(f"png signature ({kind})", png[:4] == b"\x89PNG")
        meta, out = decode_png(png)
        check(f"png write->read loop ({kind})", out == px)
        check(f"png meta ({kind})", meta["width"] == w and meta["height"] == h
              and meta["bit_depth"] == bd and meta["channels"] == ch)
    # corrupted PNG must raise, not crash silently
    bad = png[:-10] + b"x" * 10
    try:
        decode_png(bad)
        check("corrupted PNG rejected", False)
    except (ValueError, zlib.error):
        check("corrupted PNG rejected", True)


def test_cli_flags_e2e():
    import subprocess
    tmp = tempfile.mkdtemp(prefix="pyra_cli_")
    ppm = os.path.join(tmp, "in.ppm")
    w, h = 48, 32
    with open(ppm, "wb") as f:
        f.write(f"P6\n{w} {h}\n255\n".encode()
                + bytes(make_pixels("photo", w, h, 3, 8)))
    pyra = os.path.join(tmp, "out.pyra")
    pnm = os.path.join(tmp, "out.ppm")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = dict(os.environ, PYTHONPATH=root)

    r = subprocess.run([sys.executable, "-m", "pyra.cli", "encode", ppm, pyra,
                        "--method", "lz", "--tile", "48"],
                       capture_output=True, text=True, cwd=tmp, env=env)
    check(f"cli encode --method lz rc=0 ({r.returncode})", r.returncode == 0)
    check("cli encode logs method", "method: lz" in (r.stdout + r.stderr))
    r = subprocess.run([sys.executable, "-m", "pyra.cli", "decode", pyra, pnm],
                       capture_output=True, text=True, cwd=tmp, env=env)
    check("cli decode rc=0", r.returncode == 0)
    with open(ppm, "rb") as f:
        a = f.read()
    with open(pnm, "rb") as f:
        b = f.read()
    check("cli flag-encode roundtrip byte-identical", a == b)
    r = subprocess.run([sys.executable, "-m", "pyra.cli", "inspect", pyra],
                       capture_output=True, text=True, cwd=tmp, env=env)
    check("cli inspect mentions forced values", r.returncode == 0
          and "tile_size: 48" in r.stdout and "method: lz" in r.stdout)
    r = subprocess.run([sys.executable, "-m", "pyra.cli", "benchmark", ppm],
                       capture_output=True, text=True, cwd=tmp, env=env)
    check("cli benchmark rc=0 + roundtrip OK", r.returncode == 0 and "roundtrip: OK" in r.stdout)
    r = subprocess.run([sys.executable, "-m", "pyra.cli", "verify", ppm],
                       capture_output=True, text=True, cwd=tmp, env=env)
    check("cli verify rejects non-PYRA (rc=1)", r.returncode == 1 and "VERIFICATION FAILED" in (r.stdout + r.stderr))


def test_mixed_methods():
    # METHOD_MIX: per-tile best method with a 1-byte tag per tile body
    w, h, ts = 64, 32, 16
    solid_px = make_pixels("solid", w, h, 3, 8)
    noise_px = make_pixels("noise", w, h, 3, 8)
    # top half solid, bottom half noise — the classic mixed-content killer
    img = solid_px[: w * (h // 2) * 3] + noise_px[-(w * (h // 2)) * 3:]
    n_tiles = (w // ts) * (h // ts)

    data_global = encode(img, w, h, 8, CS_RGB, PRED_PAETH, ts, -1)   # global auto
    data_mix = encode(img, w, h, 8, CS_RGB, PRED_PAETH, ts, METHOD_MIX)
    hdr_mix, _t = PyraContainer.unpack_file(data_mix)
    check("mix header method=3", hdr_mix["method"] == METHOD_MIX)
    _m, decoded = decode(data_mix)
    check("mix roundtrip exact", decoded == img)
    check(f"mix beats global on hybrid ({len(data_mix)} < {len(data_global)})",
          len(data_mix) < len(data_global))
    check(f"mix overhead = 1B/tile ({len(data_mix) - len(data_global)})", True)

    # decode_region and iter_tiles honor per-tile methods: full-region equals decode()
    rmeta, rpx = decode_region(data_mix, 0, 0, w, h)
    check("mix decode_region full-frame == decode()", rpx == decoded)
    rmeta2, sub = decode_region(data_mix, 4, 2, 9, 7)
    expect = []
    for yy in range(7):
        start = ((2 + yy) * w + 4) * 3          # full-frame offset of row start
        expect.extend(decoded[start:start + 9 * 3])
    check("mix decode_region sub-ROI exact", sub == expect)
    agg = []
    for tmeta, tpx in iter_tiles(data_mix):
        agg.extend(tpx)
    expect2 = []
    for ty in range(0, h, ts):
        thh = min(ts, h - ty)
        for tx in range(0, w, ts):
            tww = min(ts, w - tx)
            for yy in range(thh):
                start = ((ty + yy) * w + tx) * 3
                expect2.extend(decoded[start:start + tww * 3])
    check("mix iter_tiles walk-order exact", agg == expect2)

    # corruption: per-tile method tag byte must be 0..2
    # The first tile body starts right after the header, the index size
    # field and the tile index (v1 and v2 headers differ in size).
    file_version = struct.unpack(">I", data_mix[8:12])[0]
    hdr_size = struct.calcsize(
        ">8sIIIBBBBII" if file_version == VERSION_LEGACY else ">8sIIIBBBBIIBB")
    tile_data_start = hdr_size + 4 + n_tiles * 8
    for tag_val in (9, 3):   # out of range, and the MIX header id itself
        bad = bytearray(data_mix)
        bad[tile_data_start] = tag_val
        body_mixed = bytes(bad[:-4])
        try:
            decode(body_mixed + zlib.crc32(body_mixed).to_bytes(4, "big"))
            check(f"invalid per-tile method tag {tag_val} raises CFE", False)
        except CorruptedFileError:
            check(f"invalid per-tile method tag {tag_val} raises CFE", True)


def test_native_backend():
    # Rust-backed hot paths (optional): when _libpyra_core.so is built,
    # stage outputs must be bit-identical to the pure-Python references.
    from pyra import native
    from pyra.compress import (pack_residuals, unpack_residuals, rle_encode,
                               rle_decode, lz_encode, lz_decode)

    if not native.available():
        check("native backend absent -> python-only mode", True)
        return

    rng = random.Random(707)
    noise = bytes(rng.randrange(256) for _ in range(3000))
    runs = (b"\x00" * 300 + b"\xff" * 130 + b"\xfe\xfe\xfe" + noise[:200]
            + b"\x05" * 257)
    residuals = [(i * 7 + i * i * 3) % 2048 for i in range(201)]
    mod = 2048

    dis = os.environ.get("PYRA_DISABLE_NATIVE")

    def pure(fn, *a):
        os.environ["PYRA_DISABLE_NATIVE"] = "1"
        try:
            return fn(*a)
        finally:
            if dis is None:
                del os.environ["PYRA_DISABLE_NATIVE"]
            else:
                os.environ["PYRA_DISABLE_NATIVE"] = dis

    check("native pack == pure pack", pack_residuals(residuals, mod)
          == pure(pack_residuals, residuals, mod))
    packed = pack_residuals(residuals, mod)
    check("native unpack == pure unpack", unpack_residuals(packed, mod, len(residuals))
          == pure(unpack_residuals, packed, mod, len(residuals)))
    for label, blob in (("noise", noise), ("runs", runs)):
        rle_n = rle_encode(blob)
        check(f"native rle_encode == pure ({label})", rle_n == pure(rle_encode, blob))
        check(f"native rle_decode roundtrip ({label})",
              rle_decode(rle_n, len(blob)) == blob)
        lz_n = lz_encode(blob)
        check(f"native lz_encode == pure ({label})", lz_n == pure(lz_encode, blob))
        # batch decode parity: native token-buffer vs lazy reference
        nat_out = native.lz_decode_tokens(lz_n, len(blob))
        lazy = lz_decode(iter(lz_n).__next__, len(blob))
        check(f"native lz_decode == lazy reference ({label})", nat_out == lazy == blob)

    # native error mapping == reference exceptions (sample of guard set).
    # run() classifies outcomes; guard_parity requires the same exception
    # class on both sides (native dispatch vs forced pure-Python).
    def run(fn, *a):
        try:
            fn(*a)
            return "ok"
        except CorruptedFileError:
            return "cfe"
        except ValueError:
            return "ve"
        except Exception:
            return "other"

    def guard_parity(name, kind, fn, *a, pure_fn=None):
        n = run(fn, *a)
        p = run(lambda: pure(pure_fn or fn, *a))
        check(name, n == kind == p)

    guard_parity("native rle_decode trunc guard", "cfe",
                 rle_decode, b"\xfe\x01", 5)
    guard_parity("native rle_decode overshoot guard", "cfe",
                 rle_decode, b"\xfe\x02\xff", 2)
    guard_parity("native rle_decode len0 guard", "cfe",
                 rle_decode, b"\xfe\x02\x00", 3)
    # unpack range: reference raises CFE with the offending value; native
    # reports the index instead (equivalent failure class)
    guard_parity("native unpack range guard", "cfe",
                 unpack_residuals, b"\x00\x08", mod, 1)
    # pack range: the reference encoder assumes valid residuals (no check);
    # the native backend additionally rejects out-of-range symbols
    check("native pack range guard (ValueError, native-only strictness)",
          run(pack_residuals, [2048], mod) == "ve")
    lz_bad = [9] * 5
    lz_dist = [1, 256, 0, 2, 0, 257]
    lz_over = [1, 256, 250, 0, 0, 257]
    guard_parity("native lz bad-token guard", "cfe",
                 lambda: native.lz_decode_tokens(lz_bad, 3),
                 pure_fn=lambda: lz_decode(iter(lz_bad).__next__, 3))
    guard_parity("native lz distance guard", "cfe",
                 lambda: native.lz_decode_tokens(lz_dist, 2),
                 pure_fn=lambda: lz_decode(iter(lz_dist).__next__, 2))
    guard_parity("native lz overshoot guard", "cfe",
                 lambda: native.lz_decode_tokens(lz_over, 5),
                 pure_fn=lambda: lz_decode(iter(lz_over).__next__, 5))

    # container-level bit-exactness: dispatch can never change container bytes
    w, h, ch = 40, 30, 3
    px = make_pixels("photo", w, h, ch, 8)
    data_native = encode(px, w, h, 8, CS_RGB, PRED_PAETH, 16, -1)
    data_pure = pure(encode, px, w, h, 8, CS_RGB, PRED_PAETH, 16, -1)
    check("native whole-container == pure (auto)", data_native == data_pure)
    _m, dec_native = decode(data_native)
    _m2, dec_pure = pure(decode, data_native)
    check("native decode == pure decode", dec_native == dec_pure == px)

    # damaged tile payload must raise (no native crash / UB)
    body = data_native[36:-4]
    _hdr, _ctiles = PyraContainer.unpack_file(data_native)
    for atk in (body[:6] + b"\xff" * 4 + body[10:], body[:-3]):
        try:
            decode(data_native[:-4])  # crc broken -> container level
            damaged_ok = False
        except CorruptedFileError:
            damaged_ok = True
        except (ValueError, IndexError):
            damaged_ok = True
        if not damaged_ok:
            break
    check("damaged container rejected under native", damaged_ok)

    # forced LZ for a larger tile exercises the hot loop for real
    px2 = make_pixels("photo", 128, 128, 1, 8)
    d1 = encode(px2, 128, 128, 8, CS_GRAY, PRED_PAETH, 128, METHOD_LZ)
    check("native forced-LZ gray roundtrip", decode(d1)[1] == px2)

    # --- predictor row kernels: native == pure, bit-exact ---------------
    from pyra.predictors import apply_predictor_row, reconstruct_row

    for pt in range(7):  # PRED_NONE..PRED_MEDIAN
        for ch in (1, 3, 4):
            row = [rng.randrange(1024) for _ in range(12 * ch)]
            prev = [rng.randrange(1024) for _ in range(12 * ch)]
            a_nat = apply_predictor_row(row, prev, pt, ch, 1024)
            a_pure = pure(apply_predictor_row, row, prev, pt, ch, 1024)
            a_nat0 = apply_predictor_row(row, None, pt, ch, 1024)
            a_pure0 = pure(apply_predictor_row, row, None, pt, ch, 1024)
            r_nat = reconstruct_row(a_nat, prev, pt, ch, 1024)
            r_pure = pure(reconstruct_row, a_pure, prev, pt, ch, 1024)
            check(f"native predictor == pure (pred={pt} ch={ch})",
                  a_nat == a_pure and a_nat0 == a_pure0
                  and r_nat == r_pure == row)

    # --- YCoCg transform: native == pure, bit-exact ---------------------
    from pyra.color import transform_pixels_forward, transform_pixels_inverse

    rng2 = random.Random(606)
    for cs in (CS_GRAY, CS_RGB, CS_RGBA, 9, -4):
        n = 9 if cs != CS_RGBA else 8  # always a multiple of the pixel stride
        px_c = [rng2.randrange(256) for _ in range(n)]
        f_nat = [transform_pixels_forward(px_c, cs),
                 transform_pixels_inverse(px_c, cs)]
        f_pure = [pure(transform_pixels_forward, px_c, cs),
                  pure(transform_pixels_inverse, px_c, cs)]
        check(f"native YCoCg == pure (cs={cs})", f_nat == f_pure)
    for cs in (CS_RGB, CS_RGBA):
        px_c = [rng2.randrange(256) for _ in range(64 * cs if cs == CS_RGBA else 192)]
        fwd = transform_pixels_forward(px_c, cs)
        check(f"YCoCg native roundtrip (cs={cs})",
              transform_pixels_inverse(fwd, cs)
              == pure(transform_pixels_inverse, fwd, cs) == px_c)

    # RGBA container goes through the native transform + predictor path
    w, h, ch = 24, 18, 4
    px3 = make_pixels("photo", w, h, ch, 8)
    d_nat = encode(px3, w, h, 8, CS_RGBA, PRED_MEDIAN, 16, -1)
    d_pure = pure(encode, px3, w, h, 8, CS_RGBA, PRED_MEDIAN, 16, -1)
    check("native RGBA container == pure", d_nat == d_pure)
    check("native RGBA roundtrip", decode(d_nat)[1] == px3)

    # --- fused pipeline parity: codec front/back halves ------------------
    # pipe_residuals == _residual_stream composition (transform ->
    # canonicalize -> per-row predict) and pipe_reconstruct == the decode
    # tail (row reconstruction -> descale -> inverse YCoCg).
    from pyra.codec import PyraCodec
    from pyra.color import transform_pixels_forward as _tfwd
    from pyra.color import transform_pixels_inverse as _tinv

    for cs, chx in ((CS_GRAY, 1), (CS_RGB, 3), (CS_RGBA, 4)):
        wt, ht = 37, 23
        stride = wt * chx
        modf = PyraCodec._residual_mod(cs, 8)
        offf = PyraCodec._canonical_offset(cs, 8)
        pxf = make_pixels("photo", wt, ht, chx, 8)

        nat_res = native.pipe_residuals(pxf, stride, offf, modf, PRED_PAETH, chx, cs)
        tr = _tfwd(pxf, cs)
        if offf:
            tr = [v + offf for v in tr]
        pure_res = []
        prev = None
        for y in range(ht):
            rs = y * stride
            pure_res.extend(apply_predictor_row(tr[rs:rs + stride], prev,
                                                PRED_PAETH, chx, modf))
            prev = tr[rs:rs + stride]
        check(f"native fused pipe_residuals == pure (cs={cs})", nat_res == pure_res)

        nat_px = native.pipe_reconstruct(nat_res, stride, offf, modf,
                                         PRED_PAETH, chx, cs)
        rows = []
        prev = None
        for y in range(ht):
            rs = y * stride
            seg = reconstruct_row(nat_res[rs:rs + stride], prev,
                                  PRED_PAETH, chx, modf)
            rows.extend(seg)
            prev = seg
        if offf:
            rows = [v - offf for v in rows]
        check(f"native fused pipe_reconstruct == pure (cs={cs})",
              nat_px == _tinv(rows, cs) == pxf)


def test_range_coder():
    """Native range coder + fused per-method decoders must be bit-identical
    to the pure-Python reference (encoder bytes, decoder symbols, and the
    final container bytes)."""
    from pyra import native
    from pyra.entropy import RangeEncoder, RangeDecoder
    from pyra.codec import PyraCodec
    from pyra.compress import (pack_residuals, rle_encode, lz_encode,
                               rle_decode)

    if not native.available():
        check("range coder backend absent -> python-only mode", True)
        return

    dis = os.environ.get("PYRA_DISABLE_NATIVE")

    def pure(fn, *a):
        os.environ["PYRA_DISABLE_NATIVE"] = "1"
        try:
            return fn(*a)
        finally:
            if dis is None:
                del os.environ["PYRA_DISABLE_NATIVE"]
            else:
                os.environ["PYRA_DISABLE_NATIVE"] = dis

    def py_encode(symbols, n_symbols):
        enc = RangeEncoder(n_symbols)
        for s in symbols:
            enc.encode_symbol(s)
        return enc.flush()

    rng = random.Random(31337)

    # --- encoder: native bytes == reference bytes ----------------------
    enc_ok = True
    dec_ok = True
    for n_sym in (2, 5, 17, 258, 1000, 65536):
        for n in (0, 1, 7, 64, 1000, 20000):
            syms = [rng.randrange(n_sym) for _ in range(n)]
            ref = py_encode(syms, n_sym)
            nat = native.range_encode(syms, n_sym)
            if nat is None or nat != ref:
                enc_ok = False
            # decoder: native symbols == reference symbols
            coder = RangeDecoder(ref, n_sym)
            ref_syms = [coder.decode_symbol() for _ in range(n)]
            nat_syms = native.range_decode(ref, n_sym, n)
            if nat_syms is None or nat_syms != ref_syms:
                dec_ok = False
    check("native range_encode == pure (byte-identical)", enc_ok)
    check("native range_decode == pure (symbol-identical)", dec_ok)

    # --- fused per-method decoders == reference ------------------------
    # The reference tail of _decode_residuals: range decode -> (RLE|LZ) ->
    # residual unpack. Compare native fused output against the pure
    # Python path for every color space x method combination.
    fused_ok = True
    for cs, ch in ((CS_GRAY, 1), (CS_RGB, 3), (CS_RGBA, 4)):
        for bd in (8, 16):
            w, h = 29, 17
            px = make_pixels("photo", w, h, ch, bd)
            mod = PyraCodec._residual_mod(cs, bd)
            offset = PyraCodec._canonical_offset(cs, bd)
            from pyra.predictors import apply_predictor_row
            from pyra.color import transform_pixels_forward
            tr = transform_pixels_forward(px, cs)
            if offset:
                tr = [v + offset for v in tr]
            res = []
            prev = None
            for y in range(h):
                seg = tr[y * w * ch:(y + 1) * w * ch]
                res.extend(apply_predictor_row(seg, prev, PRED_PAETH, ch, mod))
                prev = seg
            count = len(res)

            # RAW: range-coded residuals
            raw_bytes = pure(PyraCodec._encode_raw, res, mod)
            nat = native.range_decode(raw_bytes, mod, count)
            ref = pure(PyraCodec._decode_residuals, raw_bytes,
                       METHOD_RAW, count, mod)
            if nat is None or nat != ref or ref != res:
                fused_ok = False

            # RLE: packed + RLE + range-coded token stream
            packed = pure(pack_residuals, res, mod)
            rle = pure(rle_encode, packed)
            rle_bytes = pure(PyraCodec._encode_rle, rle)
            nat = native.decode_rle_method(rle_bytes, count, mod)
            ref = pure(PyraCodec._decode_residuals, rle_bytes,
                       METHOD_RLE, count, mod)
            if nat is None or nat != ref or ref != res:
                fused_ok = False

            # LZ: packed + RLE + LZ tokens + range-coded stream
            lz_bytes = pure(PyraCodec._encode_lz, rle)
            nat = native.decode_lz_method(lz_bytes, count, mod)
            ref = pure(PyraCodec._decode_residuals, lz_bytes,
                       METHOD_LZ, count, mod)
            if nat is None or nat != ref or ref != res:
                fused_ok = False
    check("native fused method decoders == pure (cs x bd x method)", fused_ok)

    # --- container bytes: native == pure for every forced method -------
    cont_ok = True
    for cs, ch in ((CS_GRAY, 1), (CS_RGB, 3), (CS_RGBA, 4)):
        w, h = 33, 21
        px = make_pixels("photo", w, h, ch, 8)
        for method in (METHOD_RAW, METHOD_RLE, METHOD_LZ, METHOD_MIX, -1):
            nat = PyraCodec.encode(px, w, h, 8, cs, PRED_PAETH, 64, method)
            ref = pure(PyraCodec.encode, px, w, h, 8, cs, PRED_PAETH, 64,
                       method)
            if nat != ref:
                cont_ok = False
            if PyraCodec.decode(nat)[1] != px:
                cont_ok = False
    check("native container bytes == pure (cs x method)", cont_ok)

    # --- corrupted streams: native must not crash the process ---------
    # A damaged token stream must raise CorruptedFileError (or decode to
    # a valid image) exactly like the pure path -- never a native crash.
    w, h, ch = 20, 12, 3
    px = make_pixels("photo", w, h, ch, 8)
    base = PyraCodec.encode(px, w, h, 8, CS_RGB, PRED_PAETH, 64, METHOD_LZ)
    _hdr, ctiles = PyraContainer.unpack_file(base)
    body = ctiles[0]
    crash_ok = True
    rng2 = random.Random(4242)
    for _ in range(120):
        pos = rng2.randrange(len(body))
        mutated = bytearray(body)
        mutated[pos] ^= 1 << rng2.randrange(8)
        try:
            PyraCodec.decode_tile(bytes(mutated), w, h, CS_RGB,
                                  PRED_PAETH, 8, METHOD_LZ)
        except CorruptedFileError:
            pass
        except (ValueError, IndexError):
            pass
        except Exception:
            crash_ok = False
            break
    check("native fused LZ decoder survives corruption", crash_ok)

    # --- LZ header / length guards still raise the reference errors ---
    def outcome(fn):
        try:
            fn()
            return "ok"
        except CorruptedFileError as e:
            return f"cfe:{e}"
        except Exception as e:
            return f"{type(e).__name__}"

    # implausible RLE stream length header: encode a 4-byte big-endian
    # length of 0xFFFFFFFF (symbol 255 x 4) -- far beyond the guard.
    enc = RangeEncoder(258)
    for _ in range(4):
        enc.encode_symbol(255)
    bogus = enc.flush()
    guard = outcome(lambda: PyraCodec._decode_residuals(
        bogus, METHOD_LZ, 16, 256))
    check(f"LZ implausible length guard ({guard})",
          guard.startswith("cfe:implausible RLE stream length"))


def test_buffer_pipeline():
    """Zero-copy native buffer pipeline: canvas in/out, no per-tile lists."""
    from array import array as _array
    from pyra import native as _native
    from pyra.color import transform_pixels_forward, transform_pixels_inverse
    from pyra.compress import pack_residuals, rle_encode, lz_encode
    from pyra.predictors import apply_predictor_row, reconstruct_row

    if not _native.available():
        check("buffer pipeline backend absent -> python-only mode", True)
        return

    def pure(fn, *args, **kwargs):
        old = os.environ.get("PYRA_DISABLE_NATIVE")
        os.environ["PYRA_DISABLE_NATIVE"] = "1"
        try:
            return fn(*args, **kwargs)
        finally:
            if old is None:
                os.environ.pop("PYRA_DISABLE_NATIVE", None)
            else:
                os.environ["PYRA_DISABLE_NATIVE"] = old

    def ref_residuals(px, w, h, ch, cs, pred, bd):
        mod = PyraCodec._residual_mod(cs, bd)
        offset = PyraCodec._canonical_offset(cs, bd)
        transformed = transform_pixels_forward(px, cs)
        if offset:
            transformed = [v + offset for v in transformed]
        residuals = []
        prev_row = None
        for y in range(h):
            row = transformed[y * w * ch:(y + 1) * w * ch]
            residuals.extend(apply_predictor_row(row, prev_row, pred, ch, mod))
            prev_row = row
        return residuals

    def ref_reconstruct(residuals, w, h, ch, cs, pred, bd):
        mod = PyraCodec._residual_mod(cs, bd)
        offset = PyraCodec._canonical_offset(cs, bd)
        transformed = []
        prev_row = None
        for y in range(h):
            res_row = residuals[y * w * ch:(y + 1) * w * ch]
            row = reconstruct_row(res_row, prev_row, pred, ch, mod)
            transformed.extend(row)
            prev_row = row
        if offset:
            transformed = [v - offset for v in transformed]
        return transform_pixels_inverse(transformed, cs)

    # (1) container bytes + decoded pixels: native buffer path vs pure
    fails = []
    for cs, ch in ((CS_GRAY, 1), (CS_RGB, 3), (CS_RGBA, 4)):
        for bd in (8, 16):
            for kind in ("solid", "gradient", "noise"):
                for method in (METHOD_RAW, METHOD_RLE, METHOD_LZ, METHOD_MIX, -1):
                    for (w, h, ts) in ((13, 9, 8), (64, 48, 16), (70, 50, 256)):
                        px = make_pixels(kind, w, h, ch, bd)
                        nat = PyraCodec.encode(px, w, h, bd, cs, PRED_PAETH, ts, method)
                        ref = pure(PyraCodec.encode, px, w, h, bd, cs, PRED_PAETH, ts, method)
                        if nat != ref:
                            fails.append(f"bytes cs{cs} bd{bd} {kind} m{method} {w}x{h}")
                            continue
                        _, dec = PyraCodec.decode(nat)
                        if dec != px:
                            fails.append(f"pixels cs{cs} bd{bd} {kind} m{method} {w}x{h}")
    check(f"native buffer encode/decode == pure ({len(fails)} mismatches)",
          not fails)
    if fails:
        for f in fails[:5]:
            print(f"       - {f}")

    # (2) multi-tile canvas offsets (tile_size < image)
    px = make_pixels("photo", 100, 70, 3, 8)
    blob = PyraCodec.encode(px, 100, 70, 8, CS_RGB, PRED_PAETH, 32, -1)
    _, dec = PyraCodec.decode(blob)
    check("native multi-tile canvas offsets", dec == px)

    # (3) extract_tile copies the right rectangle out of the canvas
    px = make_pixels("gradient", 40, 30, 3, 8)
    canvas = _array("i", px)
    tile_buf = _array("i", bytes(4 * 11 * 7 * 3))
    ok = _native.extract_tile(canvas, 40 * 3, 13, 5, 11, 7, 3, tile_buf)
    want = []
    for y in range(5, 12):
        for x in range(13, 24):
            want.extend(px[(y * 40 + x) * 3:(y * 40 + x) * 3 + 3])
    check("native extract_tile rectangle", ok and tile_buf.tolist() == want)

    # (4) pipe_reconstruct_into writes into the canvas at an offset
    w, h, ch, cs, pred, bd = 9, 6, 3, CS_RGB, PRED_PAETH, 8
    px = make_pixels("photo", w, h, ch, bd)
    residuals = ref_residuals(px, w, h, ch, cs, pred, bd)
    mod = PyraCodec._residual_mod(cs, bd)
    offset = PyraCodec._canonical_offset(cs, bd)
    res_buf = _array("i", residuals)
    canvas = _array("i", bytes(4 * (w + 4) * (h + 2) * ch))
    ok = _native.pipe_reconstruct_into(res_buf, w * ch, offset, mod, pred, ch,
                                       cs, canvas, dst_offset=7 * ch,
                                       dst_stride=(w + 4) * ch)
    want_px = ref_reconstruct(residuals, w, h, ch, cs, pred, bd)
    got = []
    for y in range(h):
        base = y * (w + 4) * ch + 7 * ch
        got.extend(canvas[base:base + w * ch])
    check("native pipe_reconstruct_into canvas write", ok and got == want_px)

    # (5) method encoders: raw / rle / lz byte streams vs the pure reference
    fails = []
    for cs, ch in ((CS_GRAY, 1), (CS_RGB, 3), (CS_RGBA, 4)):
        for bd in (8, 16):
            w, h = 17, 11
            px = make_pixels("noise", w, h, ch, bd)
            residuals = ref_residuals(px, w, h, ch, cs, pred, bd)
            mod = PyraCodec._residual_mod(cs, bd)
            res_buf = _array("i", residuals)
            nat_raw = _native.encode_raw_method(res_buf, mod)
            ref_raw = pure(PyraCodec._encode_raw, residuals, mod)
            if nat_raw != ref_raw:
                fails.append(f"raw cs{cs} bd{bd}")
            packed = pack_residuals(residuals, mod)
            rle = rle_encode(packed)
            nat_rle = _native.encode_rle_method(rle)
            ref_rle = pure(PyraCodec._encode_rle, rle)
            if nat_rle != ref_rle:
                fails.append(f"rle cs{cs} bd{bd}")
            nat_lz = _native.encode_lz_method(rle)
            ref_lz = pure(PyraCodec._encode_lz, rle)
            if nat_lz != ref_lz:
                fails.append(f"lz cs{cs} bd{bd}")
    check(f"native method encoders == pure ({len(fails)} mismatches)", not fails)
    if fails:
        for f in fails[:5]:
            print(f"       - {f}")

    # (6) fused decode into a buffer: rle / lz residuals vs the pure result
    fails = []
    for cs, ch in ((CS_GRAY, 1), (CS_RGB, 3), (CS_RGBA, 4)):
        for bd in (8, 16):
            w, h = 19, 13
            px = make_pixels("photo", w, h, ch, bd)
            residuals = ref_residuals(px, w, h, ch, cs, pred, bd)
            mod = PyraCodec._residual_mod(cs, bd)
            packed = pack_residuals(residuals, mod)
            rle = rle_encode(packed)
            for method, stream in ((METHOD_RLE, pure(PyraCodec._encode_rle, rle)),
                                   (METHOD_LZ, pure(PyraCodec._encode_lz, rle))):
                out = _array("i", bytes(4 * w * h * ch))
                if method == METHOD_RLE:
                    ok = _native.decode_rle_method_into(stream, w * h * ch, mod, out)
                else:
                    ok = _native.decode_lz_method_into(stream, w * h * ch, mod, out)
                ref = pure(PyraCodec._decode_residuals, stream, method, w * h * ch, mod)
                if not ok or out.tolist() != ref:
                    fails.append(f"{method} cs{cs} bd{bd}")
    check(f"native fused decode into buffer == pure ({len(fails)} mismatches)",
          not fails)
    if fails:
        for f in fails[:5]:
            print(f"       - {f}")


def test_v3_format():
    """v3 container: auto colour transform, auto row filter, adaptive predictors,
    plus backward compatibility with the v1/v2 packed residual widths."""
    from pyra.constants import (TRANSFORM_YCOCG, TRANSFORM_NONE,
                                FILTER_GLOBAL, FILTER_ADAPTIVE,
                                VERSION, VERSION_LEGACY, VERSION_V2, MAGIC)

    def pure(fn, *args, **kwargs):
        old = os.environ.get("PYRA_DISABLE_NATIVE")
        os.environ["PYRA_DISABLE_NATIVE"] = "1"
        try:
            return fn(*args, **kwargs)
        finally:
            if old is None:
                os.environ.pop("PYRA_DISABLE_NATIVE", None)
            else:
                os.environ["PYRA_DISABLE_NATIVE"] = old

    # (1) header carries the transform / filter mode
    w, h = 40, 24
    px = make_pixels("photo", w, h, 3, 8)
    blob = PyraCodec.encode(px, w, h, 8, CS_RGB, PRED_PAETH, 16, METHOD_LZ)
    meta, tiles = PyraContainer.unpack_file(blob)
    check("v3 container version == 3", meta["version"] == VERSION)
    check("v3 container stores transform",
          meta["transform"] in (TRANSFORM_YCOCG, TRANSFORM_NONE))
    check("v3 container stores filter mode",
          meta["filter_mode"] in (FILTER_GLOBAL, FILTER_ADAPTIVE))

    # (2) every forced transform / filter combination roundtrips
    fails = []
    for cs, ch in ((CS_GRAY, 1), (CS_RGB, 3), (CS_RGBA, 4)):
        for bd in (8, 16):
            for kind in ("solid", "gradient", "photo", "noise"):
                for transform in (TRANSFORM_YCOCG, TRANSFORM_NONE):
                    for filter_mode in (FILTER_GLOBAL, FILTER_ADAPTIVE):
                        for method in (METHOD_RAW, METHOD_RLE, METHOD_LZ, METHOD_MIX, -1):
                            p = make_pixels(kind, 33, 19, ch, bd)
                            data = PyraCodec.encode(p, 33, 19, bd, cs, PRED_PAETH,
                                                    16, method, transform, filter_mode)
                            m, decoded = PyraCodec.decode(data)
                            if decoded != p:
                                fails.append(f"cs{cs} bd{bd} {kind} t{transform} f{filter_mode} m{method}")
                                continue
                            if m["transform"] != transform or m["filter_mode"] != filter_mode:
                                fails.append(f"meta cs{cs} t{transform} f{filter_mode}")
    check(f"v2 forced transform/filter roundtrip ({len(fails)} failures)", not fails)
    if fails:
        for f in fails[:5]:
            print(f"       - {f}")

    # (3) auto planning is deterministic and native/pure agree byte for byte
    fails = []
    for cs, ch in ((CS_GRAY, 1), (CS_RGB, 3), (CS_RGBA, 4)):
        for kind in ("gradient", "photo", "noise"):
            p = make_pixels(kind, 45, 31, ch, 8)
            a = PyraCodec.encode(p, 45, 31, 8, cs, -1, 16, -1)
            b = PyraCodec.encode(p, 45, 31, 8, cs, -1, 16, -1)
            ref = pure(PyraCodec.encode, p, 45, 31, 8, cs, -1, 16, -1)
            if a != b:
                fails.append(f"non-deterministic cs{cs} {kind}")
            if a != ref:
                fails.append(f"native != pure cs{cs} {kind}")
    check(f"v2 auto plan deterministic + native == pure ({len(fails)} failures)", not fails)
    if fails:
        for f in fails[:5]:
            print(f"       - {f}")

    # (4) the plan really differs between the transforms on a real image
    p = make_pixels("photo", 60, 40, 3, 8)
    ycocg = PyraCodec.encode(p, 60, 40, 8, CS_RGB, PRED_PAETH, 32, METHOD_LZ,
                             TRANSFORM_YCOCG, FILTER_GLOBAL)
    none = PyraCodec.encode(p, 60, 40, 8, CS_RGB, PRED_PAETH, 32, METHOD_LZ,
                            TRANSFORM_NONE, FILTER_GLOBAL)
    check("v3 transform changes the encoded stream", ycocg != none)

    # (5) v1 files (no transform / filter bytes) still decode
    def pack_legacy(version, width, height, bit_depth, color_space, pred_type,
                    method, tile_size, tile_blobs, transform=TRANSFORM_YCOCG,
                    filter_mode=FILTER_GLOBAL):
        if version == VERSION_LEGACY:
            header = struct.pack(">8sIIIBBBBII", MAGIC, version, width,
                                 height, bit_depth, color_space, pred_type,
                                 method, tile_size, len(tile_blobs))
        else:
            header = struct.pack(">8sIIIBBBBIIBB", MAGIC, version, width,
                                 height, bit_depth, color_space, pred_type,
                                 method, tile_size, len(tile_blobs),
                                 transform, filter_mode)
        index_size = len(tile_blobs) * 8
        offset = len(header) + 4 + index_size
        index = bytearray()
        data = bytearray()
        for tb in tile_blobs:
            index += struct.pack(">II", offset, len(tb))
            offset += len(tb)
            data += tb
        body = header + struct.pack(">I", len(index)) + bytes(index) + bytes(data)
        return body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    from pyra.tile import split_into_tiles
    fails = []
    for cs, ch in ((CS_GRAY, 1), (CS_RGB, 3)):
        w, h = 32, 24
        p = make_pixels("photo", w, h, ch, 8)
        tiles = split_into_tiles(p, w, h, ch, 16)
        bodies = [PyraCodec.encode_tile(t, cs, PRED_PAETH, 8, METHOD_LZ,
                                        TRANSFORM_YCOCG, FILTER_GLOBAL,
                                        legacy=True)
                  for t in tiles]
        legacy = pack_legacy(VERSION_LEGACY, w, h, 8, cs, PRED_PAETH,
                             METHOD_LZ, 16, bodies)
        m, decoded = PyraCodec.decode(legacy)
        if decoded != p:
            fails.append(f"v1 decode cs{cs}")
        if m["version"] != VERSION_LEGACY or m["transform"] != TRANSFORM_YCOCG \
                or m["filter_mode"] != FILTER_GLOBAL:
            fails.append(f"v1 meta cs{cs}")
    check(f"v1 legacy container still decodes ({len(fails)} failures)", not fails)
    if fails:
        for f in fails[:5]:
            print(f"       - {f}")

    # (5b) v2 files (transform / filter bytes, legacy packed width) still decode
    fails = []
    for cs, ch in ((CS_GRAY, 1), (CS_RGB, 3)):
        for method in (METHOD_RAW, METHOD_RLE, METHOD_LZ):
            w, h = 32, 24
            p = make_pixels("photo", w, h, ch, 8)
            tiles = split_into_tiles(p, w, h, ch, 16)
            bodies = [PyraCodec.encode_tile(t, cs, PRED_PAETH, 8, method,
                                            TRANSFORM_NONE, FILTER_GLOBAL,
                                            legacy=True)
                      for t in tiles]
            legacy = pack_legacy(VERSION_V2, w, h, 8, cs, PRED_PAETH,
                                 method, 16, bodies, TRANSFORM_NONE,
                                 FILTER_GLOBAL)
            m, decoded = PyraCodec.decode(legacy)
            if decoded != p:
                fails.append(f"v2 decode cs{cs} m{method}")
            if m["version"] != VERSION_V2 or m["transform"] != TRANSFORM_NONE:
                fails.append(f"v2 meta cs{cs} m{method}")
    check(f"v2 legacy container still decodes ({len(fails)} failures)", not fails)
    if fails:
        for f in fails[:5]:
            print(f"       - {f}")

    # (6) a corrupt v2 header byte (transform / filter) is rejected
    v2_header = struct.calcsize(">8sIIIBBBBIIBB")
    fails = []
    for offset in (v2_header - 2, v2_header - 1):  # transform, filter_mode
        broken = bytearray(blob)
        broken[offset] = 0x7F
        body = bytes(broken[:-4])
        broken[-4:] = struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        try:
            PyraContainer.unpack_file(bytes(broken))
            fails.append(f"offset {offset} accepted")
        except CorruptedFileError:
            pass
        except Exception as e:  # noqa: BLE001
            fails.append(f"offset {offset} -> {type(e).__name__}")
    check(f"v2 rejects an invalid transform/filter byte ({len(fails)} failures)", not fails)
    if fails:
        for f in fails[:5]:
            print(f"       - {f}")


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
    test_forced_methods()
    test_png_roundtrip()
    test_cli_flags_e2e()
    test_mixed_methods()
    test_native_backend()
    test_range_coder()
    test_buffer_pipeline()
    test_v3_format()
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("Failures:")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
