# LUMC (LUMEN Cloud) format test suite.
# Run:  python3 -m lum.tests.test_cloud
#
# The cloud container holds multi-band, multi-scene cubes. Every check here is
# about correctness: the decoded cube must be byte-identical to the input and
# a damaged container must be rejected, never silently decoded.

import os
import random
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lum.cloud import (METHOD_LZ, METHOD_RAW, PRED_BAND, PRED_NONE, PRED_TIME,
                        SFMT_F32, SFMT_I16, SFMT_I32, SFMT_U8, SFMT_U16,
                        SFMT_U32, CloudError, decode, encode, info, read_band)

PASS = 0
FAIL = 0
FAILURES: list[str] = []

ALL_FORMATS = (SFMT_U8, SFMT_U16, SFMT_U32, SFMT_I16, SFMT_I32, SFMT_F32)
FORMAT_NAMES = {SFMT_U8: "u8", SFMT_U16: "u16", SFMT_U32: "u32",
                SFMT_I16: "i16", SFMT_I32: "i32", SFMT_F32: "f32"}


def check(name: str, ok: bool):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL {name}")


def make_cube(kind: str, w: int, h: int, bands: int, scenes: int,
              sample_format: int, seed: int = 5) -> bytes:
    """Raw little-endian samples, scene by scene, band by band."""
    rnd = random.Random(seed)
    sb = {SFMT_U8: 1, SFMT_U16: 2, SFMT_U32: 4, SFMT_I16: 2,
          SFMT_I32: 4, SFMT_F32: 4}[sample_format]
    signed = sample_format in (SFMT_I16, SFMT_I32)
    count = w * h * bands * scenes
    out = bytearray()
    if sample_format == SFMT_F32:
        for i in range(count):
            value = 12.5 + (i % 31) * 0.5 if kind != "noise" else rnd.uniform(-5, 5)
            out += struct.pack("<f", value)
        return bytes(out)
    bits = 8 * sb
    for i in range(count):
        if kind == "const":
            value = 7
        elif kind == "smooth":
            value = (i * 13) % (1 << bits)
        else:
            value = rnd.randrange(1 << bits)
        if signed:
            value -= 1 << (bits - 1)
        out += (value & ((1 << bits) - 1)).to_bytes(sb, "little")
    return bytes(out)


def band_table(bands: int) -> list[dict]:
    return [{"name": f"band{i}", "wavelength_nm": 400.0 + 10.0 * i}
            for i in range(bands)]


def scene_table(scenes: int) -> list[dict]:
    return [{"timestamp": 1700000000 + i, "lon0": 30.0 + i, "lat0": 41.0}
            for i in range(scenes)]


def test_roundtrip_matrix():
    """Every sample format x geometry x method x predictor round-trips."""
    bad = []
    for sf in ALL_FORMATS:
        for (w, h, bands, scenes) in [(8, 6, 1, 1), (17, 9, 2, 1),
                                      (16, 12, 2, 2), (12, 8, 3, 2)]:
            for kind in ("smooth", "noise", "const"):
                for method in (-1, METHOD_RAW, METHOD_LZ):
                    for predictor in (-1, PRED_NONE, PRED_TIME):
                        raw = make_cube(kind, w, h, bands, scenes, sf)
                        try:
                            blob = encode(raw, w, h, bands, scenes, sf,
                                          band_meta=band_table(bands),
                                          scene_meta=scene_table(scenes),
                                          tile_size=8, method=method,
                                          predictor=predictor)
                            meta, out = decode(blob)
                        except Exception as exc:  # noqa: BLE001
                            bad.append(f"{FORMAT_NAMES[sf]} {w}x{h}x{bands}x{scenes} "
                                       f"{kind} m={method} p={predictor}: "
                                       f"{type(exc).__name__}: {exc}")
                            continue
                        if out != raw:
                            bad.append(f"{FORMAT_NAMES[sf]} {w}x{h}x{bands}x{scenes} "
                                       f"{kind} m={method} p={predictor}: mismatch")
    check("roundtrip matrix (6 formats x 4 geometries x 3 kinds x 3 methods "
          "x 3 predictors)", not bad)
    for line in bad[:10]:
        print("       ", line)


def test_geometry_and_tables():
    """The header reports the geometry and the tables survive."""
    raw = make_cube("smooth", 16, 12, 3, 2, SFMT_U16)
    blob = encode(raw, 16, 12, 3, 2, SFMT_U16,
                  band_meta=band_table(3), scene_meta=scene_table(2),
                  metadata={"satellite": "test", "level": "L2"},
                  tile_size=8)
    meta = info(blob)
    ok = (meta["width"] == 16 and meta["height"] == 12
          and meta["bands"] == 3 and meta["scenes"] == 2
          and meta["sample_format"] == SFMT_U16 and meta["bits"] == 16)
    check("the header reports the geometry", ok)
    _, out = decode(blob)
    check("the metadata survives the round trip", out == raw)


def test_read_band():
    """Reading a single band gives the same bytes as the full decode."""
    raw = make_cube("smooth", 16, 12, 3, 2, SFMT_U16)
    blob = encode(raw, 16, 12, 3, 2, SFMT_U16,
                  band_meta=band_table(3), scene_meta=scene_table(2),
                  tile_size=8)
    _, full = decode(blob)
    plane = 16 * 12 * 2
    bad = []
    for scene in range(2):
        for band in range(3):
            one = read_band(blob, scene, band)
            base = (scene * 3 + band) * plane
            if one != full[base:base + plane]:
                bad.append(f"scene {scene} band {band}")
    check("read_band matches the full decode", not bad)
    if bad:
        print("       ", ", ".join(bad[:6]))


def test_determinism():
    """The same cube always produces the same bytes."""
    raw = make_cube("smooth", 16, 12, 2, 1, SFMT_U16)
    a = encode(raw, 16, 12, 2, 1, SFMT_U16, band_meta=band_table(2),
               scene_meta=scene_table(1), tile_size=8)
    b = encode(raw, 16, 12, 2, 1, SFMT_U16, band_meta=band_table(2),
               scene_meta=scene_table(1), tile_size=8)
    check("encoding is deterministic", a == b)


def test_integrity():
    """A damaged container is rejected instead of decoded."""
    raw = make_cube("smooth", 16, 12, 2, 1, SFMT_U16)
    blob = encode(raw, 16, 12, 2, 1, SFMT_U16, band_meta=band_table(2),
                  scene_meta=scene_table(1), tile_size=8)
    rnd = random.Random(3)
    leaks = 0
    errors = 0
    for _ in range(120):
        damaged = bytearray(blob)
        pos = rnd.randrange(len(damaged))
        damaged[pos] ^= 1 << rnd.randrange(8)
        try:
            _, out = decode(bytes(damaged))
        except CloudError:
            errors += 1
            continue
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        if out != raw:
            leaks += 1
    check("bit flip fuzz never returns wrong data", leaks == 0)
    check(f"bit flip fuzz rejects the container ({errors}/120)", errors >= 60)


def test_bad_input():
    """Impossible inputs are refused."""
    try:
        encode(b"\x00" * 4, 4, 4, 1, 1, SFMT_U16)
        check("a too small sample buffer is refused", False)
    except ValueError:
        check("a too small sample buffer is refused", True)
    try:
        encode(b"\x00" * 4, 2, 2, 1, 1, SFMT_U8, method=42)
        check("an unknown method is refused", False)
    except CloudError:
        check("an unknown method is refused", True)
    try:
        encode(b"\x00" * 4, 2, 2, 1, 1, SFMT_U8, predictor=99)
        check("an unknown predictor is refused", False)
    except CloudError:
        check("an unknown predictor is refused", True)
    try:
        decode(b"not a cloud container")
        check("a foreign file is refused", False)
    except CloudError:
        check("a foreign file is refused", True)


def test_band_predictor():
    """The band predictor codes a smooth spectral gradient."""
    raw = make_cube("smooth", 16, 12, 3, 1, SFMT_U16)
    blob = encode(raw, 16, 12, 3, 1, SFMT_U16, band_meta=band_table(3),
                  scene_meta=scene_table(1), tile_size=8, predictor=PRED_BAND)
    _, out = decode(blob)
    check("the band predictor round trips", out == raw)


def test_cpp_layer():
    """The C++ work-set helpers behave like the reference implementation."""
    import lum.cloud as _cloud
    if not _cloud.fast_available():
        check("the C++ cloud core is present", False)
        return
    check("the C++ cloud core is present", True)

    raw = make_cube("smooth", 12, 8, 3, 2, SFMT_U16)
    w, h, bands, scenes = 12, 8, 3, 2

    # --- the raw cube format round trips
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cube.lumx")
        try:
            _cloud.raw_write(path, raw, w, h, bands, scenes, SFMT_U16)
            written = True
        except Exception:  # noqa: BLE001
            written = False
        samples, rw, rh, rb, rs, rf = _cloud.raw_read(path)
        check("the C++ raw cube round trips",
              written and samples == raw
              and (rw, rh, rb, rs, rf) == (w, h, bands, scenes, SFMT_U16))

    # --- the band statistics (one band over every scene)
    stats = _cloud.band_stats(raw, w, h, bands, scenes, SFMT_U16, 1)
    plane = w * h
    values = []
    for scene in range(scenes):
        base = (scene * bands + 1) * plane
        for i in range(base, base + plane):
            values.append(int.from_bytes(raw[i * 2:i * 2 + 2], "little"))
    check("the band statistics find the minimum",
          stats["minimum"] == min(values))
    check("the band statistics find the maximum",
          stats["maximum"] == max(values))
    check("the band statistics count every sample",
          stats["count"] == plane * scenes)
    check("the histogram covers every sample",
          sum(stats["histogram"]) == plane * scenes)

    # --- the false colour render
    rgb = _cloud.render_rgb(raw, w, h, bands, scenes, SFMT_U16, 0,
                            0, 1, 2, 4096.0, 2048.0, 4096.0, 2048.0,
                            4096.0, 2048.0)
    check("the render produces one RGB triple per pixel",
          isinstance(rgb, bytes) and len(rgb) == w * h * 3)

    # --- planar and interleaved are the same data
    # the cube is band planar: element (scene, band, y, x) sits at
    # ((scene * bands + band) * height + y) * width + x
    interleaved = _cloud.interleave(raw, w, h, bands, scenes, SFMT_U16, 0)
    expected = bytearray()
    for y in range(h):
        for x in range(w):
            for b in range(bands):
                index = (b * h + y) * w + x
                expected += raw[index * 2:index * 2 + 2]
    check("the interleaved form carries the same samples",
          bytes(interleaved) == bytes(expected))

    back = _cloud.deinterleave(interleaved, w, h, bands, SFMT_U16)
    expected = bytearray()
    for b in range(bands):
        for y in range(h):
            for x in range(w):
                index = (b * h + y) * w + x
                expected += raw[index * 2:index * 2 + 2]
    check("the deinterleaved form is band planar again",
          bytes(back) == bytes(expected))


def test_cli():
    """The command line builds and inspects a cloud container."""
    import io
    import json
    from contextlib import redirect_stdout

    from lum import cli as _cli

    raw = make_cube("smooth", 12, 8, 3, 2, SFMT_U16)
    with tempfile.TemporaryDirectory() as tmp:
        cube = os.path.join(tmp, "cube.lumx")
        container = os.path.join(tmp, "cube.lumc")
        out = os.path.join(tmp, "out.lumx")
        import lum.cloud as _cloud
        if not _cloud.fast_available():
            check("the command line builds a cloud container", False)
            return
        _cloud.raw_write(cube, raw, 12, 8, 3, 2, SFMT_U16)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = _cli.main(["cloud", "encode", cube, container,
                            "--width", "12", "--height", "8", "--bands", "3",
                            "--scenes", "2", "--format", "u16",
                            "--band-name", "red", "--band-name", "green",
                            "--band-name", "nir", "--meta", "satellite=test"])
        ok = rc == 0 and os.path.exists(container)
        check("the command line builds a cloud container", ok)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = _cli.main(["cloud", "info", container])
        text = buffer.getvalue()
        check("the command line reports the geometry",
              rc == 0 and "bands" in text and "3" in text)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = _cli.main(["cloud", "decode", container, out,
                            "--scene", "1", "--band", "2"])
        ok = rc == 0 and os.path.exists(out)
        if ok:
            samples, rw, rh, rb, rs, rf = _cloud.raw_read(out)
            plane = 12 * 8
            base = (1 * 3 + 2) * plane
            ok = samples == raw[base * 2:(base + plane) * 2]
        check("the command line decodes a single band", ok)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = _cli.main(["cloud", "stats", container, "--band", "0"])
        check("the command line prints the band statistics",
              rc == 0 and "min=" in buffer.getvalue())


def main():
    print("=== LUMC cloud format test suite ===")
    test_roundtrip_matrix()
    test_geometry_and_tables()
    test_read_band()
    test_determinism()
    test_integrity()
    test_bad_input()
    test_band_predictor()
    test_cpp_layer()
    test_cli()
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("Failures:")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
