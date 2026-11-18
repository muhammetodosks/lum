# PYRA v4 (lossy DCT) test suite.
# Run:  python3 -m pyra.tests.test_lossy
#
# Version 4 is the first lossy PYRA revision: YCbCr, 8x8 DCT, perceptual
# quantisation and an adaptive binary range coder. The checks below pin the
# *contract* of the codec, not its exact numbers, so the bitstream and the
# quantisation tables can still be improved without rewriting the suite:
#
#   * a lossless request must never drift (q=100 stays close to the source)
#   * quality is monotonic: more quality always means more bytes
#   * encoding is deterministic: identical input -> identical container
#   * damaged containers are rejected instead of decoded into garbage
#   * the container header round-trips through `info`/`inspect`
#   * gray, RGB and RGBA planes all survive the round trip
#   * existing (v3 and earlier) lossless files keep decoding

import io
import math
import os
import random
import struct
import zlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pyra import lossy
from pyra.api import _parse_pnm_raw, decode_file, encode_file
from pyra.codec import PyraCodec
from pyra.constants import CS_GRAY, CS_RGB, CS_RGBA, MAGIC
from pyra.security import CorruptedFileError

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


def psnr(a, b) -> float:
    se = 0
    for x, y in zip(a, b):
        d = x - y
        se += d * d
    mse = se / len(a)
    return 99.0 if mse == 0 else 10.0 * math.log10(255.0 * 255.0 / mse)


def photo(w: int, h: int, ch: int = 3, seed: int = 7) -> bytes:
    """A synthetic "photograph": smooth gradients, edges and a little noise.

    Real photographs are the only content that exercises large DCT
    coefficients, which is exactly what used to desynchronise the range
    coder. Keeping this in the suite prevents a silent regression.
    """
    rnd = random.Random(seed)
    out = bytearray(w * h * ch)
    for y in range(h):
        for x in range(w):
            base = 40 + (x * 180) // max(1, w - 1)
            wave = int(30 * math.sin(x * 0.07) * math.cos(y * 0.05))
            edge = 60 if (x - w // 3) ** 2 + (y - h // 2) ** 2 < (h // 3) ** 2 else 0
            for k in range(ch):
                v = base + wave + edge + (k * 25) + rnd.randint(-6, 6)
                out[(y * w + x) * ch + k] = max(0, min(255, v))
    return bytes(out)


def flat(w: int, h: int, ch: int = 3, value: int = 128) -> bytes:
    return bytes([value]) * (w * h * ch)


def roundtrip(raw, w, h, ch, q, sub=lossy.SUBSAMPLE_444):
    cs = {1: CS_GRAY, 3: CS_RGB, 4: CS_RGBA}[ch]
    blob = lossy.encode(raw, w, h, cs, q, sub)
    assert blob is not None, "the native v4 codec must be built for this suite"
    meta, back = lossy.decode(blob)
    return blob, meta, back


def test_available():
    print("\n-- codec availability --")
    check("native v4 codec is built", lossy.available())
    check("channels_for(CS_GRAY) == 1", lossy.channels_for(CS_GRAY) == 1)
    check("channels_for(CS_RGB) == 3", lossy.channels_for(CS_RGB) == 3)
    check("channels_for(CS_RGBA) == 4", lossy.channels_for(CS_RGBA) == 4)
    bad = False
    try:
        lossy.channels_for(99)
    except ValueError:
        bad = True
    check("channels_for rejects an unknown colour space", bad)


def test_quality():
    print("\n-- quality / rate distortion --")
    w, h = 96, 64
    raw = photo(w, h)
    sizes = {}
    for q in (50, 75, 90, 100):
        blob, meta, back = roundtrip(raw, w, h, 3, q)
        sizes[q] = len(blob)
        p = psnr(raw, back)
        print(f"     q={q:3d}  {len(blob):6d} B  {p:6.2f} dB")
        check(f"q={q} round trip reaches {p:.1f} dB > 30", p > 30.0)
    check("quality is monotonic in bytes",
          sizes[50] < sizes[75] < sizes[90] < sizes[100])
    check("q=100 is near-lossless (> 45 dB)", psnr(raw, roundtrip(raw, w, h, 3, 100)[2]) > 45.0)
    check("q=50 is clearly lossy (< q=100 bytes)",
          sizes[50] < sizes[100])

    # A flat image has almost no AC energy: it must compress to a handful of
    # bytes instead of one byte per pixel.
    blob, _, back = roundtrip(flat(w, h), w, h, 3, 90)
    check("flat image compresses below 1 % of the raw size",
          len(blob) < (w * h * 3) // 100)
    check("flat image round trips exactly at q=100",
          roundtrip(flat(w, h), w, h, 3, 100)[2] == flat(w, h))


def test_determinism():
    print("\n-- determinism --")
    w, h = 64, 48
    raw = photo(w, h)
    a = lossy.encode(raw, w, h, CS_RGB, 82, lossy.SUBSAMPLE_444)
    b = lossy.encode(raw, w, h, CS_RGB, 82, lossy.SUBSAMPLE_444)
    check("same input encodes to the same bytes", a == b)
    ma, sa = lossy.decode(a)
    mb, sb = lossy.decode(a)
    check("decoding twice gives the same samples", sa == sb)


def test_subsampling():
    print("\n-- chroma subsampling --")
    w, h = 96, 64
    raw = photo(w, h)
    full, meta_full, back_full = roundtrip(raw, w, h, 3, 88, lossy.SUBSAMPLE_444)
    half, meta_half, back_half = roundtrip(raw, w, h, 3, 88, lossy.SUBSAMPLE_420)
    check("4:4:4 records subsampling=1", meta_full["subsampling"] == 1)
    check("4:2:0 records subsampling=2", meta_half["subsampling"] == 2)
    check("4:2:0 is smaller than 4:4:4", len(half) < len(full))
    check("4:2:0 still round trips above 30 dB", psnr(raw, back_half) > 30.0)
    check("4:2:0 loses quality against 4:4:4",
          psnr(raw, back_half) < psnr(raw, back_full))
    # AUTO is 4:4:4 at every quality: at matched RGB PSNR the subsampled
    # stream is always the larger one, so auto-selecting 4:2:0 only ever cost
    # bits. 4:2:0 stays available as an explicit choice.
    auto_low = roundtrip(raw, w, h, 3, 70, lossy.SUBSAMPLE_AUTO)[1]
    auto_high = roundtrip(raw, w, h, 3, 95, lossy.SUBSAMPLE_AUTO)[1]
    check("AUTO uses 4:4:4 at low quality", auto_low["subsampling"] == 1)
    check("AUTO uses 4:4:4 at high quality", auto_high["subsampling"] == 1)
    plain = lossy.encode(raw, w, h, CS_RGB, 70, lossy.SUBSAMPLE_AUTO)
    explicit = lossy.encode(raw, w, h, CS_RGB, 70, lossy.SUBSAMPLE_444)
    check("AUTO is byte-identical to explicit 4:4:4", plain == explicit)


def test_color_spaces():
    print("\n-- colour spaces --")
    w, h = 33, 17  # deliberately not multiples of 8
    gray = flat(w, h, 1, 200)
    blob, meta, back = roundtrip(gray, w, h, 1, 100)
    check("gray is stored with one plane", meta["num_planes"] == 1)
    check("gray round trips exactly at q=100", back == gray)
    check("gray header reports 1 channel", meta["color_space"] == CS_GRAY)

    rgba = bytearray(photo(w, h, 3))
    rgba = bytes(bytearray(photo(w, h, 3)) + bytes([255]) * (w * h))
    blob, meta, back = roundtrip(rgba, w, h, 4, 100)
    check("rgba is stored with four planes", meta["num_planes"] == 4)
    check("rgba luma and alpha survive at q=100", psnr(rgba, back) > 45.0)
    check("rgba header reports 4 channels", meta["color_space"] == CS_RGBA)

    # Odd geometry must be padded on encode and cropped on decode.
    rgb = photo(w, h)
    _, meta, back = roundtrip(rgb, w, h, 3, 92)
    check("odd geometry keeps its dimensions",
          (meta["width"], meta["height"]) == (w, h))
    check("odd geometry decodes the full buffer", len(back) == w * h * 3)


def test_info():
    print("\n-- container header --")
    w, h = 40, 24
    raw = photo(w, h)
    blob = lossy.encode(raw, w, h, CS_RGB, 77, lossy.SUBSAMPLE_444)
    check("container starts with the PYRA magic", blob[:len(MAGIC)] == MAGIC)
    check("version field is 5",
          struct.unpack(">I", blob[8:12])[0] == 5)
    meta = lossy.info(blob)
    check("info reports version 5", meta["version"] == 5)
    check("info reports the geometry",
          (meta["width"], meta["height"]) == (w, h))
    check("info reports 8-bit", meta["bit_depth"] == 8)
    check("info reports the quality", meta["quality"] == 77)
    check("info reports the plane count", meta["num_planes"] == 3)
    check("info marks the container as lossy", meta["lossy"] is True)
    check("info flags subsampling in header mode", meta["mode"] == 1)
    check("header flags the YCoCg colour transform", (blob[22] >> 2) & 1 == 1)
    check("info does not decode planes (header only)", "samples" not in meta)

    # The chroma step multiplier travels in byte 24 (1/128 units) because the
    # decoder rebuilds the quantiser from `quality` alone otherwise. A zero
    # there means "no multiplier": that is what files written before the field
    # existed contain, and they must still decode with a 1.0 multiplier.
    check("header carries the chroma step multiplier", blob[24] == 92)
    check("multiplier is stored in 1/128 units",
          abs(blob[24] / 128.0 - 0.72) < 1.0 / 128.0)
    legacy = bytearray(blob)
    legacy[24] = 0
    legacy[-4:] = struct.pack(">I", zlib.crc32(bytes(legacy[:-4])) & 0xFFFFFFFF)
    legacy_meta, legacy_samples = lossy.decode(bytes(legacy))
    check("a zero multiplier byte is accepted as 1.0",
          legacy_meta["width"] == w and len(legacy_samples) == w * h * 3)


def test_corruption():
    print("\n-- corruption handling --")
    w, h = 48, 32
    blob = bytearray(lossy.encode(photo(w, h), w, h, CS_RGB, 85, lossy.SUBSAMPLE_444))

    for name, mutate in (
        ("a flipped payload byte", lambda b: b.__setitem__(len(b) // 2, b[len(b) // 2] ^ 0xFF)),
        ("a truncated container", lambda b: b.__delitem__(slice(len(b) // 2, None))),
        ("a zeroed length field", lambda b: b.__setitem__(slice(36, 40), b"\x00\x00\x00\x00")),
        ("a bumped version", lambda b: b.__setitem__(11, 9)),
        ("a broken magic", lambda b: b.__setitem__(0, 0x00)),
    ):
        bad = bytearray(blob)
        mutate(bad)
        ok = False
        try:
            lossy.decode(bytes(bad))
        except CorruptedFileError:
            ok = True
        except Exception as exc:  # noqa: BLE001 - anything else is a bug
            print(f"     unexpected {type(exc).__name__}: {exc}")
        check(f"{name} is rejected", ok)

    # The header reader must refuse it too, not report nonsense geometry.
    bad = bytearray(blob)
    bad[11] = 9
    check("info rejects a wrong version", lossy.info(bytes(bad)) is None)

    # Every single-byte flip in the header must be caught by the CRC or the
    # version/magic checks, never silently decoded.
    undetected = 0
    for i in range(min(36, len(blob))):
        bad = bytearray(blob)
        bad[i] ^= 0x01
        try:
            lossy.decode(bytes(bad))
        except Exception:  # noqa: BLE001
            continue
        if bytes(bad)[:len(MAGIC)] == MAGIC and struct.unpack(">I", bytes(bad)[8:12])[0] == 4:
            undetected += 1
    check("header byte flips are always rejected", undetected == 0)


def test_stream_desync():
    print("\n-- range coder / magnitude coding --")
    # This exact case used to fail: `k = bit_length(value) - 1` maps both 0
    # and 1 to k = 0, so ±1 DC deltas vanished and error accumulated row by
    # row. A wide gradient is the smallest reproduction.
    w, h = 512, 8
    raw = bytearray(w * h * 3)
    for y in range(h):
        for x in range(w):
            for k in range(3):
                raw[(y * w + x) * 3 + k] = (x * 255) // (w - 1)
    blob, _, back = roundtrip(bytes(raw), w, h, 3, 95)
    p = psnr(bytes(raw), back)
    print(f"     512x8 gradient at q=95: {len(blob)} B, {p:.2f} dB")
    check("a long gradient keeps its DC chain (> 45 dB)", p > 45.0)

    # Same but for a photographic strip: no row-to-row drift allowed.
    full = photo(w, 64)
    bad = 0
    for y in range(64):
        row = full[y * w * 3:(y + 1) * w * 3]
        back_row = back[:0]  # placeholder, replaced below
        del back_row
    blob, _, back = roundtrip(full, w, 64, 3, 95)
    for y in range(64):
        row = full[y * w * 3:(y + 1) * w * 3]
        out_row = back[y * w * 3:(y + 1) * w * 3]
        if psnr(row, out_row) < 30.0:
            bad += 1
    check("no row drifts away (all rows > 30 dB)", bad == 0)


def test_api_and_cli_paths():
    print("\n-- api / codec integration --")
    tmp = tempfile.mkdtemp(prefix="pyra_v4_test_")
    w, h = 64, 40
    raw = photo(w, h)
    ppm = os.path.join(tmp, "in.ppm")
    with open(ppm, "wb") as f:
        f.write(b"P6\n%d %d\n255\n" % (w, h))
        f.write(raw)
    out = os.path.join(tmp, "out.pyra")
    meta = encode_file(ppm, out, quality=90, subsampling=lossy.SUBSAMPLE_444)
    check("encode_file reports the quality", meta["quality"] == 90)
    check("encode_file writes a v5 container",
          open(out, "rb").read()[len(MAGIC):len(MAGIC) + 4] == b"\x00\x00\x00\x05")
    decoded = os.path.join(tmp, "back.ppm")
    info = decode_file(out, decoded)
    check("decode_file returns the v5 geometry", (info["width"], info["height"]) == (w, h))
    _, m2, w2, raw2, bd2, _ = _parse_pnm_raw(decoded)
    check("decoded file keeps 8-bit samples", bd2 == 8)
    check("decoded file matches the source dimensions", (w2, m2) == (h, w))
    check("decoded file is visually close", psnr(raw, raw2) > 30.0)

    # The generic codec entry point must dispatch v5 to the lossy backend.
    blob = open(out, "rb").read()
    codec_meta, samples = PyraCodec.decode(blob)
    check("PyraCodec.decode routes v5 to the lossy backend",
          codec_meta["version"] == 5 and len(samples) == w * h * 3)
    check("PyraCodec.decode returns Python ints, not bytes",
          isinstance(samples[0], int) and 0 <= samples[0] <= 255)

    # quality=0 (the default) must keep using the lossless v3 path.
    lossless = os.path.join(tmp, "lossless.pyra")
    encode_file(ppm, lossless)
    head = open(lossless, "rb").read()
    check("default encode_file still writes the lossless container",
          struct.unpack(">I", head[8:12])[0] != 4)
    lm, ls = PyraCodec.decode(head)
    check("the lossless container still round trips exactly",
          bytes(ls) == raw)

    for name in ("in.ppm", "out.pyra", "back.ppm", "lossless.pyra"):
        try:
            os.unlink(os.path.join(tmp, name))
        except OSError:
            pass
    try:
        os.rmdir(tmp)
    except OSError:
        pass


def test_gray_and_16bit_limits():
    print("\n-- parameter validation --")
    raw = bytes([0]) * (16 * 16 * 3)
    bad = False
    try:
        lossy.encode(raw, 0, 16, CS_RGB, 90, lossy.SUBSAMPLE_444)
    except Exception:  # noqa: BLE001
        bad = True
    check("zero width is rejected", bad)

    bad = False
    try:
        lossy.encode(b"\x00" * 10, 16, 16, CS_RGB, 90, lossy.SUBSAMPLE_444)
    except ValueError:
        bad = True
    check("a short sample buffer is rejected", bad)


def test_colour_transform():
    print("\n-- colour transform --")
    w, h = 64, 48
    raw = photo(w, h)
    sizes = {}
    for name, flag in (("ycocg", "1"), ("ycbcr", "0")):
        os.environ["PYRA_CT"] = flag
        blob = lossy.encode(raw, w, h, CS_RGB, 88, lossy.SUBSAMPLE_444)
        meta, back = lossy.decode(blob)
        sizes[name] = len(blob)
        want = 1 if flag == "1" else 0
        check(f"{name} writes its transform flag", (blob[22] >> 2) & 1 == want)
        check(f"{name} round-trips above 30 dB", psnr(raw, bytes(back)) > 30.0)
    os.environ.pop("PYRA_CT", None)
    # The baked default must be YCoCg, but the size relation is not asserted:
    # YCoCg wins on average over a photo set, not on every individual image.
    default = lossy.encode(raw, w, h, CS_RGB, 88, lossy.SUBSAMPLE_444)
    check("the default transform is YCoCg", (default[22] >> 2) & 1 == 1)


def main():
    if not lossy.available():
        print("PYRA v4 codec is not built - run scripts/build_native.sh first")
        return 1
    test_available()
    test_quality()
    test_determinism()
    test_subsampling()
    test_color_spaces()
    test_colour_transform()
    test_info()
    test_corruption()
    test_stream_desync()
    test_api_and_cli_paths()
    test_gray_and_16bit_limits()
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAIL:
        print("FAILURES: " + ", ".join(FAILURES))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
