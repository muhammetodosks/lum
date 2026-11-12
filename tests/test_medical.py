# PYRM (PYRA Medical) format test suite.
# Run:  python3 -m pyra.tests.test_medical
#
# The medical format is used for clinical data: every check here is about
# correctness and integrity, never about speed. A damaged container must
# always be rejected with an exception - it must never decode into wrong
# data.

import os
import random
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pyra.medical import (METHOD_LZ, METHOD_RAW, PRED_NONE, PRED_PAETH,
                          PRED_SLICE, SFMT_F32, SFMT_I16, SFMT_I32, SFMT_U8,
                          SFMT_U16, SFMT_U32, CorruptedFileError, MedicalError,
                          decode, encode)
from pyra.medical import codec as _codec
from pyra.medical import container as _container
from pyra.medical import native as _native

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


def make_samples(kind: str, w: int, h: int, d: int, ch: int,
                 sample_format: int) -> bytes:
    """Build raw samples in the little-endian order the decoder returns."""
    fmt = _codec._Format(sample_format)
    rnd = random.Random(20240117)
    count = w * h * d * ch
    out = bytearray()
    if fmt.is_float:
        import struct as _s
        for i in range(count):
            value = 12.5 + (i % 97) * 0.25 if kind != "noise" else rnd.uniform(-5, 5)
            out += _s.pack("<f", value)
        return bytes(out)
    bits = fmt.modulus.bit_length() - 1
    signed = fmt.signed
    for z in range(d):
        for y in range(h):
            for x in range(w):
                for c in range(ch):
                    if kind == "const":
                        value = 7
                    elif kind == "smooth":
                        value = (x * 3 + y * 5 + z * 11 + c) % fmt.modulus
                    else:
                        value = rnd.randrange(fmt.modulus)
                    if signed:
                        value -= fmt.modulus >> 1
                    # raw two's complement bit pattern
                    out += (value & (fmt.modulus - 1)).to_bytes(
                        fmt.sample_bytes, "little")
    return bytes(out)


def pure(fn, *args, **kwargs):
    """Run `fn` with the native backend disabled."""
    old = os.environ.get("PYRA_DISABLE_NATIVE")
    os.environ["PYRA_DISABLE_NATIVE"] = "1"
    try:
        return fn(*args, **kwargs)
    finally:
        if old is None:
            os.environ.pop("PYRA_DISABLE_NATIVE", None)
        else:
            os.environ["PYRA_DISABLE_NATIVE"] = old


# ------------------------------------------------------------------ roundtrip

def test_roundtrip_matrix():
    """Every sample format x geometry x method x predictor round-trips."""
    geometries = [
        (1, 1, 1, 1),
        (17, 9, 1, 1),
        (33, 21, 3, 1),
        (40, 24, 2, 2),
        (9, 7, 1, 3),
    ]
    bad = []
    for sf in ALL_FORMATS:
        for (w, h, d, ch) in geometries:
            for kind in ("smooth", "noise", "const"):
                for method in (-1, METHOD_RAW, METHOD_LZ):
                    for predictor in (-1, PRED_NONE, PRED_PAETH):
                        raw = make_samples(kind, w, h, d, ch, sf)
                        blob = encode(raw, w, h, d, ch, sf, method=method,
                                      predictor=predictor)
                        try:
                            meta, out = decode(blob)
                        except Exception as exc:  # noqa: BLE001
                            bad.append(f"{FORMAT_NAMES[sf]} {w}x{h}x{d}x{ch} "
                                       f"{kind} m={method} p={predictor}: "
                                       f"{type(exc).__name__}: {exc}")
                            continue
                        if out != raw:
                            bad.append(f"{FORMAT_NAMES[sf]} {w}x{h}x{d}x{ch} "
                                       f"{kind} m={method} p={predictor}: "
                                       f"payload mismatch")
    check("roundtrip matrix (6 formats x 5 geometries x 3 kinds x 3 methods "
          "x 3 predictors)", not bad)
    for line in bad[:10]:
        print("       ", line)


def test_metadata():
    """Metadata survives the round trip and its CRC is verified."""
    raw = make_samples("smooth", 20, 12, 1, 1, SFMT_U16)
    meta = {"PatientID": "ANON-001", "Modality": "CT", "SliceThickness": 0.75}
    blob = encode(raw, 20, 12, 1, 1, SFMT_U16, metadata=meta)
    got, out = decode(blob)
    check("metadata round trip", got == meta and out == raw)

    # Damaged metadata must be rejected
    hdr = _container.unpack_header(blob)
    damaged = bytearray(blob)
    damaged[hdr.meta_offset] ^= 0x01
    try:
        decode(bytes(damaged))
        check("damaged metadata rejected", False)
    except MedicalError:
        check("damaged metadata rejected", True)


def test_integrity_checks():
    """CRC and SHA-256 catch every single-bit change in the payload."""
    raw = make_samples("smooth", 24, 16, 2, 1, SFMT_U16)
    blob = encode(raw, 24, 16, 2, 1, SFMT_U16)
    hdr = _container.unpack_header(blob)
    header = bytearray(blob[:_container.HEADER_SIZE])

    # 1. header CRC
    damaged = bytearray(blob)
    damaged[20] ^= 0x80  # height field
    try:
        decode(bytes(damaged))
        ok = False
    except MedicalError:
        ok = True
    check("header CRC rejects a changed header field", ok)

    # 2. tile payload CRC
    damaged = bytearray(blob)
    damaged[hdr.index_offset + hdr.index_size + 3] ^= 0x10
    try:
        decode(bytes(damaged))
        ok = False
    except MedicalError:
        ok = True
    check("tile CRC rejects a changed payload byte", ok)

    # 3. SHA-256 content hash
    #    Rebuild a container whose payload CRC still matches but whose
    #    content hash is wrong: the hash check must catch it.
    body = bytearray(blob)
    payload_start = hdr.index_offset + hdr.index_size
    body[payload_start + 5] ^= 0x20
    # fix the tile CRC and the payload CRC so only the hash is wrong
    import hashlib
    index = _container.unpack_index(
        blob[hdr.index_offset:hdr.index_offset + hdr.index_size], hdr.num_tiles)
    off, length, method, flags, fb, crc = index[0]
    struct.pack_into("<I", body, hdr.index_offset + 12, zlib.crc32(
        bytes(body[off:off + length])) & 0xFFFFFFFF)
    payload = bytes(body[payload_start:])
    struct.pack_into("<I", body, 68, zlib.crc32(payload) & 0xFFFFFFFF)
    try:
        decode(bytes(body))
        ok = False
    except MedicalError:
        ok = True
    check("SHA-256 content hash rejects a rewritten payload", ok)


def test_bitflip_fuzz():
    """Random bit flips must always raise, never return wrong data."""
    raw = make_samples("smooth", 20, 16, 1, 1, SFMT_U16)
    blob = encode(raw, 20, 16, 1, 1, SFMT_U16)
    rnd = random.Random(99)
    leaks = 0
    errors = 0
    for _ in range(400):
        damaged = bytearray(blob)
        pos = rnd.randrange(len(damaged))
        damaged[pos] ^= 1 << rnd.randrange(8)
        try:
            meta, out = decode(bytes(damaged))
        except MedicalError:
            errors += 1
            continue
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        if out == raw:
            errors += 1  # the flip landed in a harmless place
        else:
            leaks += 1
    check("bit-flip fuzz never returns wrong data", leaks == 0)
    check("bit-flip fuzz mostly rejects the file", errors >= 300)


def test_truncated():
    """Truncated containers must raise, not return partial data."""
    raw = make_samples("smooth", 18, 12, 1, 1, SFMT_U16)
    blob = encode(raw, 18, 12, 1, 1, SFMT_U16)
    ok = True
    for cut in (0, 10, 64, len(blob) // 2, len(blob) - 1):
        try:
            decode(blob[:cut])
        except MedicalError:
            pass
        except Exception:  # noqa: BLE001
            ok = False
    check("truncated containers are rejected", ok)


def test_native_parity():
    """The native core and the pure-Python reference agree byte for byte."""
    if not _native.available():
        check("native medical backend present", False)
        return
    check("native medical backend present", True)
    diffs = []
    for sf in ALL_FORMATS:
        for (w, h, d, ch) in [(13, 9, 1, 1), (21, 15, 3, 1), (16, 12, 2, 2)]:
            for kind in ("smooth", "noise"):
                raw = make_samples(kind, w, h, d, ch, sf)
                native_blob = encode(raw, w, h, d, ch, sf)
                pure_blob = pure(encode, raw, w, h, d, ch, sf)
                if native_blob != pure_blob:
                    diffs.append(f"{FORMAT_NAMES[sf]} {w}x{h}x{d}x{ch} {kind}")
                    continue
                _, n_out = decode(native_blob)
                _, p_out = pure(decode, native_blob)
                if n_out != p_out:
                    diffs.append(f"decode {FORMAT_NAMES[sf]} {w}x{h}x{d}x{ch}")
    check("native and pure-Python containers are identical", not diffs)
    for line in diffs[:10]:
        print("       ", line)


def test_determinism():
    """The same input always produces the same bytes."""
    raw = make_samples("smooth", 22, 14, 1, 1, SFMT_I16)
    a = encode(raw, 22, 14, 1, 1, SFMT_I16)
    b = encode(raw, 22, 14, 1, 1, SFMT_I16)
    check("encoding is deterministic", a == b)


def test_edge_cases():
    """Odd but legal geometries and options."""
    raw = make_samples("const", 1, 1, 1, 1, SFMT_U8)
    blob = encode(raw, 1, 1, 1, 1, SFMT_U8)
    _, out = decode(blob)
    check("1x1 image round trip", out == raw)

    raw = make_samples("smooth", 8, 8, 4, 1, SFMT_U8)
    blob = encode(raw, 8, 8, 4, 1, SFMT_U8, tile_size=3)
    _, out = decode(blob)
    check("tile size smaller than the image", out == raw)

    raw = make_samples("smooth", 8, 8, 1, 3, SFMT_U8)
    blob = encode(raw, 8, 8, 1, 3, SFMT_U8, predictor=PRED_SLICE)
    _, out = decode(blob)
    check("slice predictor on a single slice", out == raw)

    try:
        encode(b"\x00" * 4, 4, 4, 1, 1, SFMT_U16)
        check("too small sample buffer is rejected", False)
    except ValueError:
        check("too small sample buffer is rejected", True)

    try:
        encode(b"\x00" * 4, 2, 2, 1, 1, SFMT_U8, method=42)
        check("unknown method is rejected", False)
    except MedicalError:
        check("unknown method is rejected", True)

    try:
        encode(b"\x00" * 4, 2, 2, 1, 1, SFMT_U8, predictor=99)
        check("unknown predictor is rejected", False)
    except MedicalError:
        check("unknown predictor is rejected", True)


def test_info():
    """The header can be read without decoding the samples."""
    raw = make_samples("smooth", 12, 10, 3, 2, SFMT_U16)
    blob = encode(raw, 12, 10, 3, 2, SFMT_U16)
    info = _codec.info(blob)
    ok = (info["width"] == 12 and info["height"] == 10
          and info["depth"] == 3 and info["channels"] == 2
          and info["sample_format"] == SFMT_U16 and info["bits"] == 16)
    check("header info without decoding", ok)


def test_compression():
    """The format really compresses smooth data."""
    raw = make_samples("smooth", 128, 128, 1, 1, SFMT_U16)
    blob = encode(raw, 128, 128, 1, 1, SFMT_U16)
    check("smooth volume compresses below the raw size", len(blob) < len(raw))


def main():
    print("=== PYRM medical format test suite ===")
    test_roundtrip_matrix()
    test_metadata()
    test_integrity_checks()
    test_bitflip_fuzz()
    test_truncated()
    test_native_parity()
    test_determinism()
    test_edge_cases()
    test_info()
    test_compression()
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("Failures:")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
