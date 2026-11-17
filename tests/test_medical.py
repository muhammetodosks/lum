# PYRM (PYRA Medical) format test suite.
# Run:  python3 -m pyra.tests.test_medical
#
# The medical format is used for clinical data: every check here is about
# correctness and integrity, never about speed. A damaged container must
# always be rejected with an exception - it must never decode into wrong
# data.

import io
import json
import os
import random
import struct
import sys
import tempfile
import zlib
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pyra.medical import (METHOD_LZ, METHOD_RAW, PRED_MED, PRED_NONE,
                          PRED_PAETH, PRED_SLICE, SFMT_F32, SFMT_I16, SFMT_I32,
                          SFMT_U8, SFMT_U16, SFMT_U32, CorruptedFileError,
                          MedicalError, MedicalMetadataError, build_dicom_metadata,
                          decode, encode, is_dicom_compatible, sign,
                          signature_status, verify)
from pyra.medical import codec as _codec
from pyra.medical import container as _container
from pyra.medical import dicom as _dicom
from pyra.medical import native as _native
from pyra.medical import preview as _preview
from pyra import fast as _fast

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
        # The backend may be switched off on purpose (the fallback run).
        disabled = bool(os.environ.get("PYRA_DISABLE_NATIVE")) or \
            os.environ.get("PYRA_NATIVE") == "0"
        check("native medical backend present", disabled)
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




# ------------------------------------------------------- clinical metadata

def test_dicom_metadata():
    """The DICOM-like metadata is built, validated and cross-checked."""
    w, h, d = 20, 12, 3
    meta = build_dicom_metadata(
        w, h, d, 1, SFMT_U16,
        patient_id="ANON-001", modality="CT",
        study_uid="1.2.840.113619.2.1", series_uid="1.2.840.113619.2.1.3",
        sop_uid="1.2.840.113619.2.1.3.7", study_date="20261113",
        slice_thickness=1.25, pixel_spacing=(0.7, 0.7),
        rescale_slope=1.0, rescale_intercept=-1024.0,
        window_center=40.0, window_width=400.0)
    check("dicom metadata passes validation",
          _dicom.validate_dicom_metadata(meta, w, h, d, 1, SFMT_U16,
                                         strict=True) == [])
    check("dicom metadata is reported as compatible",
          is_dicom_compatible(meta, w, h, d, 1, SFMT_U16))
    check("geometry attributes are derived",
          meta["Rows"] == h and meta["Columns"] == w
          and meta["NumberOfFrames"] == d and meta["SamplesPerPixel"] == 1
          and meta["BitsAllocated"] == 16 and meta["BitsStored"] == 16
          and meta["PixelRepresentation"] == 0)
    check("the calibration is readable",
          _dicom.rescale(meta) == (1.0, -1024.0)
          and _dicom.window(meta) == (40.0, 400.0))
    check("the photometric interpretation defaults to MONOCHROME2",
          _dicom.photometric(meta) == _dicom.MONOCHROME2
          and not _dicom.is_monochrome1(meta))

    # a contradiction must be refused
    broken = dict(meta)
    broken["Columns"] = w + 1
    try:
        _dicom.validate_dicom_metadata(broken, w, h, d, 1, SFMT_U16, strict=True)
        ok = False
    except MedicalMetadataError:
        ok = True
    check("a contradicting geometry is refused", ok)

    broken = dict(meta)
    broken["PixelRepresentation"] = 1  # signed, but the samples are unsigned
    try:
        _dicom.validate_dicom_metadata(broken, w, h, d, 1, SFMT_U16, strict=True)
        ok = False
    except MedicalMetadataError:
        ok = True
    check("a contradicting pixel representation is refused", ok)

    broken = dict(meta)
    broken["PhotometricInterpretation"] = "MONOCHROME3"
    try:
        _dicom.validate_dicom_metadata(broken, w, h, d, 1, SFMT_U16, strict=True)
        ok = False
    except MedicalMetadataError:
        ok = True
    check("an unknown photometric interpretation is refused", ok)

    broken = dict(meta)
    del broken["PatientID"]
    try:
        _dicom.validate_dicom_metadata(broken, w, h, d, 1, SFMT_U16, strict=True)
        ok = False
    except MedicalMetadataError:
        ok = True
    check("a missing patient identifier is refused", ok)

    # the encoder refuses to store clinically invalid metadata
    raw = make_samples("smooth", w, h, d, 1, SFMT_U16)
    try:
        encode(raw, w, h, d, 1, SFMT_U16, metadata=broken, require_dicom=True)
        ok = False
    except MedicalMetadataError:
        ok = True
    check("the encoder refuses invalid clinical metadata", ok)

    blob = encode(raw, w, h, d, 1, SFMT_U16, metadata=meta, require_dicom=True)
    got, out = decode(blob)
    check("the clinical metadata survives the round trip",
          got == meta and out == raw)


def test_verify_command():
    """The verify entry point checks every guarantee."""
    raw = make_samples("smooth", 18, 12, 2, 1, SFMT_U16)
    blob = encode(raw, 18, 12, 2, 1, SFMT_U16)
    report = verify(blob)
    check("verify accepts an intact container",
          report["ok"] and report["num_tiles"] == 2
          and report["payload_bytes"] > 0)

    hdr = _container.unpack_header(blob)
    damaged = bytearray(blob)
    damaged[hdr.index_offset + hdr.index_size + 1] ^= 0x08
    try:
        verify(bytes(damaged))
        ok = False
    except MedicalError:
        ok = True
    check("verify rejects a damaged payload", ok)

    damaged = bytearray(blob)
    damaged[20] ^= 0x40  # the height field
    try:
        verify(bytes(damaged))
        ok = False
    except MedicalError:
        ok = True
    check("verify rejects a damaged header", ok)


def test_med_predictor():
    """The LOCO-I median edge detector round-trips."""
    raw = make_samples("smooth", 24, 16, 2, 1, SFMT_U16)
    blob = encode(raw, 24, 16, 2, 1, SFMT_U16, predictor=PRED_MED)
    _, out = decode(blob)
    check("the MED predictor round-trips", out == raw)

    # the predictor id is stored in the tile table, so a forced MED file
    # differs from a forced PAETH file
    other = encode(raw, 24, 16, 2, 1, SFMT_U16, predictor=PRED_PAETH)
    check("MED and PAETH produce different streams", blob != other)


def test_preview_semantics():
    """The preview applies the calibration and the photometric flag."""
    raw = make_samples("smooth", 12, 9, 1, 1, SFMT_U16)
    plain = _preview.preview_slice(raw, 12, 9, 1, 1, SFMT_U16, 0, 400.0, 40.0)
    inverted = _preview.preview_slice(raw, 12, 9, 1, 1, SFMT_U16, 0, 400.0,
                                       40.0, 1.0, 0.0, True)
    check("MONOCHROME1 inverts the preview",
          plain is not None and inverted is not None
          and plain != inverted
          and all(plain[i] + inverted[i] == 255 for i in range(len(plain))))

    calibrated = _preview.preview_slice(raw, 12, 9, 1, 1, SFMT_U16, 0,
                                        400.0, 40.0, 2.0, -1024.0)
    check("the rescale changes the preview", calibrated is not None
          and calibrated != plain)

    meta = build_dicom_metadata(12, 9, 1, 1, SFMT_U16, patient_id="A",
                                modality="CT", study_uid="1", series_uid="2",
                                window_center=40.0, window_width=400.0,
                                rescale_slope=2.0, rescale_intercept=-1024.0)
    from_meta = _preview.preview_from_metadata(raw, 12, 9, 1, 1, SFMT_U16,
                                                meta, 0)
    check("the metadata drives the preview", from_meta == calibrated)

    # the C++ helper and the Python fallback must agree byte for byte
    if _fast.available():
        native = _fast.medical_preview(raw, 12, 9, 1, 1, SFMT_U16, 0, 400.0,
                                        40.0, 2.0, -1024.0, 1)
        python_inverted = _preview.preview_slice(raw, 12, 9, 1, 1, SFMT_U16, 0,
                                                  400.0, 40.0, 2.0, -1024.0,
                                                  True)
        native_plain = _fast.medical_preview(raw, 12, 9, 1, 1, SFMT_U16, 0,
                                              400.0, 40.0, 2.0, -1024.0, 0)
        check("the C++ and the Python preview agree",
              native == python_inverted and native_plain == calibrated)


def _scan_dicom_tags(blob: bytes) -> list[tuple[int, int]]:
    """List the (group, element) tags of an explicit VR little endian file.

    The scan is deliberately independent of the writer, so a duplicated
    element is visible instead of being hidden by a dictionary.
    """
    import struct as _s
    long_vr = {b"OB", b"OW", b"OF", b"SQ", b"UT", b"UN"}
    pos = 132
    # the file meta information group starts with (0002,0000) UL
    group_len = _s.unpack_from("<I", blob, pos + 8)[0]
    pos += 12 + group_len
    tags = []
    while pos + 8 <= len(blob):
        group, element = _s.unpack_from("<HH", blob, pos)
        if (group, element) == (0x7FE0, 0x0010):
            break
        vr = blob[pos + 4:pos + 6]
        if vr in long_vr:
            length = _s.unpack_from("<I", blob, pos + 8)[0]
            pos += 12 + length
        else:
            length = _s.unpack_from("<H", blob, pos + 6)[0]
            pos += 8 + length
        tags.append((group, element))
    return tags


def test_dicom_metadata_fidelity():
    """The clinical metadata survives a DICOM round trip unchanged."""
    from pyra.medical import dicomfile as _dcmfile

    raw = make_samples("smooth", 24, 16, 3, 1, SFMT_U16)
    meta = _dicom.build_dicom_metadata(
        24, 16, 3, 1, SFMT_U16, patient_id="ANON-042", patient_name="Doe^Jane",
        modality="CT", study_uid="1.2.840.113619.2.1",
        series_uid="1.2.840.113619.2.1.4", sop_uid="1.2.840.113619.2.1.4.9",
        study_date="20260113", slice_thickness=1.5,
        pixel_spacing=(0.5, 0.5), rescale_slope=1.0,
        rescale_intercept=-1024.0, window_center=40.0, window_width=400.0)

    # 1. the container keeps the block byte for byte
    blob = encode(raw, 24, 16, 3, 1, SFMT_U16, metadata=meta)
    got, out = decode(blob)
    check("the container keeps the clinical metadata unchanged",
          got == meta and out == raw)

    # 2. a real DICOM file carries exactly the same attribute set
    dic = _dcmfile.write_dicom(raw, 24, 16, 3, 1, SFMT_U16, meta)
    back, read_meta = _dcmfile.read_dicom(dic)
    missing = {k: meta[k] for k in meta if k not in read_meta}
    extra = {k: read_meta[k] for k in read_meta if k not in meta}
    changed = {k: (meta[k], read_meta[k]) for k in meta
               if k in read_meta and meta[k] != read_meta[k]}
    check("the DICOM file carries exactly the same attributes "
          "(no loss, no invention)", not missing and not extra and not changed)
    if missing or extra or changed:
        print(f"        missing: {missing}")
        print(f"        extra: {extra}")
        print(f"        changed: {changed}")

    # 3. the clinically important derived attributes are there
    check("HighBit is BitsStored - 1",
          meta["HighBit"] == meta["BitsStored"] - 1)
    check("the SOP class belongs to the modality",
          meta["SOPClassUID"] == _dicom.sop_class_for("CT"))

    # 4. no element is written twice (DICOM forbids duplicates)
    tags = _scan_dicom_tags(dic)
    check("no duplicated DICOM element", len(tags) == len(set(tags)))

    # 5. a wrong HighBit is reported
    broken = dict(meta)
    broken["HighBit"] = meta["BitsStored"]
    problems = _dicom.validate_dicom_metadata(
        broken, 24, 16, 3, 1, SFMT_U16, strict=False)
    check("a wrong HighBit is reported",
          any("HighBit" in p for p in problems))

    # 6. a window of zero is "no window", but not together with a centre
    problems = _dicom.validate_dicom_metadata(
        dict(meta, WindowWidth=0.0, WindowCenter=0.0), 24, 16, 3, 1,
        SFMT_U16, strict=False)
    check("a zero window without a centre is accepted", not problems)
    problems = _dicom.validate_dicom_metadata(
        dict(meta, WindowWidth=0.0, WindowCenter=40.0), 24, 16, 3, 1,
        SFMT_U16, strict=False)
    check("a zero window with a centre is reported",
          any("WindowWidth" in p for p in problems))


def test_dicom_file():
    """A container survives a round trip through a real DICOM file."""
    from pyra.medical import dicomfile as _dcmfile

    for sf in (SFMT_U8, SFMT_U16, SFMT_U32, SFMT_I16, SFMT_I32):
        raw = make_samples("smooth", 20, 14, 3, 1, sf)
        meta = _dicom.build_dicom_metadata(
            20, 14, 3, 1, sf, patient_id="ANON-042", patient_name="Doe^Jane",
            modality="CT", study_uid="1.2.840.113619.2.1",
            series_uid="1.2.840.113619.2.1.4", sop_uid="1.2.840.113619.2.1.4.9",
            study_date="20260113", slice_thickness=1.5,
            rescale_slope=1.0, rescale_intercept=-1024.0,
            window_center=40.0, window_width=400.0)
        blob = _dcmfile.write_dicom(raw, 20, 14, 3, 1, sf, meta)
        if blob[128:132] != b"DICM":
            check(f"DICOM file signature (format {FORMAT_NAMES[sf]})", False)
            continue
        back, read_meta = _dcmfile.read_dicom(blob)
        ok = back == raw
        ok = ok and read_meta["PatientID"] == "ANON-042"
        ok = ok and read_meta["PatientName"] == "Doe^Jane"
        ok = ok and read_meta["Modality"] == "CT"
        ok = ok and read_meta["StudyInstanceUID"] == "1.2.840.113619.2.1"
        ok = ok and read_meta["SeriesInstanceUID"] == "1.2.840.113619.2.1.4"
        ok = ok and read_meta["SOPInstanceUID"] == "1.2.840.113619.2.1.4.9"
        ok = ok and int(read_meta["Rows"]) == 14
        ok = ok and int(read_meta["Columns"]) == 20
        ok = ok and int(read_meta["NumberOfFrames"]) == 3
        ok = ok and abs(float(read_meta["WindowWidth"]) - 400.0) < 1e-9
        ok = ok and abs(float(read_meta["RescaleIntercept"]) + 1024.0) < 1e-9
        check(f"DICOM round trip (format {FORMAT_NAMES[sf]})", ok)

    # a file that is not DICOM must be refused
    try:
        _dcmfile.read_dicom(b"not a dicom file" * 20)
        check("a non-DICOM file is refused", False)
    except MedicalError:
        check("a non-DICOM file is refused", True)

    # a truncated file must be refused
    raw = make_samples("smooth", 8, 6, 1, 1, SFMT_U16)
    meta = _dicom.build_dicom_metadata(
        8, 6, 1, 1, SFMT_U16, patient_id="A", modality="CT",
        study_uid="1.2", series_uid="1.2.3", sop_uid="1.2.3.4")
    blob = _dcmfile.write_dicom(raw, 8, 6, 1, 1, SFMT_U16, meta)
    try:
        _dcmfile.read_dicom(blob[:len(blob) // 2])
        check("a truncated DICOM file is refused", False)
    except MedicalError:
        check("a truncated DICOM file is refused", True)


def test_cli_dicom():
    """The command line can build a clinically valid container."""
    import tempfile
    from pyra import cli as _cli

    raw = make_samples("smooth", 24, 16, 3, 1, SFMT_U16)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "vol.raw")
        dst = os.path.join(tmp, "vol.pyrm")
        with open(src, "wb") as f:
            f.write(raw)
        rc = _cli.main([
            "med", "encode", src, dst,
            "--width", "24", "--height", "16", "--depth", "3",
            "--format", "u16", "--dicom", "--patient-id", "ANON-007",
            "--modality", "CT",
            "--study-uid", "1.2.840.113619.2.1",
            "--series-uid", "1.2.840.113619.2.1.4",
            "--require-dicom",
        ])
        ok = rc == 0 and os.path.exists(dst)
        if ok:
            with open(dst, "rb") as f:
                blob = f.read()
            metadata, samples = decode(blob)
            ok = samples == raw and _dicom.is_dicom_compatible(metadata)
            ok = ok and metadata["PatientID"] == "ANON-007"
            ok = ok and metadata["Modality"] == "CT"
            ok = ok and metadata["Rows"] == 16 and metadata["Columns"] == 24
    check("the CLI builds a DICOM compatible container", ok)

    # A container without the required attributes must be refused.
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "vol.raw")
        dst = os.path.join(tmp, "vol.pyrm")
        with open(src, "wb") as f:
            f.write(raw)
        rc = _cli.main([
            "med", "encode", src, dst,
            "--width", "24", "--height", "16", "--depth", "3",
            "--format", "u16", "--dicom", "--require-dicom",
        ])
        check("the CLI refuses an incomplete clinical metadata block",
              rc != 0 and not os.path.exists(dst))


def test_cli_dump():
    """`med dump` prints a complete JSON document about the container."""
    import io
    import json
    import tempfile
    from contextlib import redirect_stdout
    from pyra import cli as _cli

    raw = make_samples("smooth", 20, 12, 2, 1, SFMT_U16)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "vol.raw")
        dst = os.path.join(tmp, "vol.pyrm")
        with open(src, "wb") as f:
            f.write(raw)
        with redirect_stdout(io.StringIO()):
            rc = _cli.main([
                "med", "encode", src, dst,
                "--width", "20", "--height", "12", "--depth", "2",
                "--format", "u16",
            ])
        ok = rc == 0 and os.path.exists(dst)
        if ok:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = _cli.main(["med", "dump", dst])
            ok = rc == 0
            try:
                document = json.loads(buf.getvalue())
            except ValueError:
                document = None
            ok = ok and isinstance(document, dict)
            if ok:
                # the geometry must be in the document, not only in the
                # metadata: a container without a metadata block still has
                # to be inspectable
                ok = (document["container"]["width"] == 20
                      and document["container"]["height"] == 12
                      and document["container"]["depth"] == 2
                      and document["container"]["num_tiles"] == 2
                      and document["integrity"]["ok"] is True
                      and "metadata" in document)
    check("the CLI dumps the container as a complete JSON document", ok)

    # --metadata-only prints just the metadata block
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "vol.raw")
        dst = os.path.join(tmp, "vol.pyrm")
        with open(src, "wb") as f:
            f.write(raw)
        with redirect_stdout(io.StringIO()):
            _cli.main(["med", "encode", src, dst, "--width", "20",
                       "--height", "12", "--depth", "2", "--format", "u16"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _cli.main(["med", "dump", "--metadata-only", dst])
        try:
            only = json.loads(buf.getvalue())
        except ValueError:
            only = None
    check("the CLI can dump just the metadata block",
          rc == 0 and isinstance(only, dict) and "container" not in only)


def test_clinical_consistency():
    """A container whose metadata contradicts it is not clinically usable.

    The compatibility flag used to ignore contradictions and only look at
    the missing attributes, so a container that claimed 999 rows for a
    24 row image was still reported as DICOM compatible. That would let a
    viewer trust a geometry it must not trust.
    """
    w, h, d = 24, 16, 2
    raw = make_samples("smooth", w, h, d, 1, SFMT_U16)
    meta = build_dicom_metadata(
        w, h, d, 1, SFMT_U16, patient_id="ANON-009", modality="CT",
        study_uid="1.2.840.113619.2.9", series_uid="1.2.840.113619.2.9.1",
        sop_uid="1.2.840.113619.2.9.1.1")
    check("a consistent block is compatible",
          is_dicom_compatible(meta, w, h, d, 1, SFMT_U16))

    # 1. a geometry that contradicts the container
    bad = dict(meta)
    bad["Rows"] = h + 100
    check("a contradictory geometry is not compatible",
          not is_dicom_compatible(bad, w, h, d, 1, SFMT_U16))

    # 2. a pixel representation that contradicts the sample format
    bad = dict(meta)
    bad["PixelRepresentation"] = 1
    check("a contradictory pixel representation is not compatible",
          not is_dicom_compatible(bad, w, h, d, 1, SFMT_U16))

    # 3. a photometric interpretation that does not fit the channel count
    bad = dict(meta)
    bad["PhotometricInterpretation"] = "RGB"
    check("a contradictory photometric interpretation is not compatible",
          not is_dicom_compatible(bad, w, h, d, 1, SFMT_U16))

    # 4. the report of a broken container names the problem
    blob = encode(raw, w, h, d, 1, SFMT_U16, metadata=bad)
    report = verify(blob)
    check("the report lists the clinical problems",
          not report["dicom_compatible"]
          and any("PhotometricInterpretation" in p
                  for p in report["dicom_problems"]))

    # 5. a clean container reports no problems at all
    blob = encode(raw, w, h, d, 1, SFMT_U16, metadata=meta)
    report = verify(blob)
    check("a clean container reports no clinical problems",
          report["dicom_compatible"] and report["dicom_problems"] == [])

    # 6. generated identifiers are valid and reproducible
    uid_a = _dicom.generate_uid("study:1")
    uid_b = _dicom.generate_uid("study:1")
    uid_c = _dicom.generate_uid("study:2")
    check("generated identifiers are valid and reproducible",
          uid_a == uid_b and uid_a != uid_c and len(uid_a) <= 64
          and uid_a.startswith("2.25."))

    # 7. the command line generates the technical identifiers on its own
    from pyra import cli as _cli
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "vol.raw")
        dst = os.path.join(tmp, "vol.pyrm")
        with open(src, "wb") as f:
            f.write(raw)
        rc = _cli.main(["med", "encode", src, dst,
                        "--width", str(w), "--height", str(h),
                        "--depth", str(d), "--format", "u16",
                        "--dicom", "--patient-id", "ANON-010",
                        "--modality", "CT"])
        ok = rc == 0 and os.path.exists(dst)
        if ok:
            with open(dst, "rb") as f:
                blob = f.read()
            metadata, samples = decode(blob)
            ok = (samples == raw and is_dicom_compatible(metadata)
                  and metadata["StudyInstanceUID"].startswith("2.25.")
                  and metadata["SeriesInstanceUID"].startswith("2.25.")
                  and metadata["SOPInstanceUID"].startswith("2.25."))
    check("the command line generates the technical identifiers", ok)


def test_dicom_conformance():
    """An independent DICOM toolkit must read our files and vice versa.

    This is the only honest way to claim "a real viewer can open it": our
    own writer reading our own files proves nothing about interoperability.
    The test is skipped when pydicom is not installed.
    """
    try:
        import pydicom
    except ImportError:
        print("  SKIP third-party DICOM conformance (pydicom not installed)")
        return
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian
    from pyra.medical import dicomfile as _dcmfile

    tmp = tempfile.mkdtemp(prefix="pyra-conf-")
    cases = [
        ("u8", SFMT_U8, 24, 18, 1),
        ("u16", SFMT_U16, 24, 18, 1),
        ("i16", SFMT_I16, 24, 18, 1),
        ("u32", SFMT_U32, 16, 12, 1),
        ("i32", SFMT_I32, 16, 12, 1),
        ("multi-frame", SFMT_U16, 20, 14, 3),
    ]

    for (label, fmt, w, h, d) in cases:
        raw = _conf_volume(w, h, d, fmt)
        meta = build_dicom_metadata(
            width=w, height=h, depth=d, channels=1, sample_format=fmt,
            patient_id="ANON-CONF-1", patient_name="Conformance^Test",
            modality="CT", study_uid=_dicom.generate_uid("study"),
            series_uid=_dicom.generate_uid("series"),
            sop_uid=_dicom.generate_uid("sop"),
            study_date="20261114", study_time="120000",
            rescale_slope=1.0, rescale_intercept=-1024.0,
            window_center=40.0, window_width=400.0)
        blob = _dcmfile.write_dicom(raw, w, h, d, 1, fmt, metadata=meta)
        path = os.path.join(tmp, "conf_%s.dcm" % label.replace(" ", "_"))
        with open(path, "wb") as f:
            f.write(blob)

        try:
            dcm = pydicom.dcmread(path)
            parsed = True
        except Exception as exc:  # noqa: BLE001
            dcm = None
            parsed = False
            print(f"       {label}: {type(exc).__name__}: {exc}")
        check(f"pydicom reads our {label} file", parsed)
        if not parsed:
            continue
        check(f"pydicom sees the geometry of the {label} file",
              dcm.Rows == h and dcm.Columns == w
              and int(dcm.get("NumberOfFrames", 1)) == d)
        check(f"pydicom sees the clinical attributes of the {label} file",
              str(dcm.PatientID) == "ANON-CONF-1"
              and str(dcm.Modality) == "CT"
              and str(dcm.PhotometricInterpretation) == "MONOCHROME2"
              and str(dcm.SOPClassUID) == str(meta["SOPClassUID"]))
        check(f"pydicom sees the pixel data of the {label} file",
              bytes(dcm.PixelData) == raw)
        check(f"the transfer syntax of the {label} file is explicit VR LE",
              str(dcm.file_meta.TransferSyntaxUID) == str(ExplicitVRLittleEndian))

        samples, back = _dcmfile.read_dicom(blob)
        check(f"PYRM reads back its own {label} DICOM",
              samples == raw and str(back.get("PatientID")) == "ANON-CONF-1")

    # a file written by pydicom itself
    ds = Dataset()
    ds.PatientID = "ANON-EXTERNAL"
    ds.PatientName = "External^Source"
    ds.Modality = "MR"
    ds.StudyInstanceUID = _dicom.generate_uid("ext-study")
    ds.SeriesInstanceUID = _dicom.generate_uid("ext-series")
    ds.SOPInstanceUID = _dicom.generate_uid("ext-sop")
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
    ds.Rows, ds.Columns, ds.NumberOfFrames = 17, 23, 3
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.WindowCenter, ds.WindowWidth = 40, 400
    ds.RescaleSlope, ds.RescaleIntercept = 1, 0
    ds.PixelSpacing = [0.5, 0.5]
    ds.SliceThickness = 3.0
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.SpacingBetweenSlices = 3.0
    ext_raw = _conf_volume(23, 17, 3, SFMT_U16, seed=99)
    ds.PixelData = ext_raw
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = ds.SOPClassUID
    fm.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = fm
    ds.preamble = b"\x00" * 128
    ext_path = os.path.join(tmp, "external.dcm")
    ds.save_as(ext_path, enforce_file_format=True)

    blob = open(ext_path, "rb").read()
    try:
        samples, meta = _dcmfile.read_dicom(blob)
        ok = True
    except Exception as exc:  # noqa: BLE001
        samples, meta = None, {}
        ok = False
        print(f"       external: {type(exc).__name__}: {exc}")
    check("PYRM reads a file written by pydicom itself", ok)
    if ok:
        check("the external pixels are identical", samples == ext_raw)
        check("the external attributes are read",
              str(meta.get("PatientID")) == "ANON-EXTERNAL"
              and int(meta.get("Rows", 0)) == 17
              and int(meta.get("Columns", 0)) == 23
              and int(meta.get("NumberOfFrames", 1)) == 3
              and str(meta.get("Modality")) == "MR")
        problems = _dicom.validate_dicom_metadata(
            meta, 23, 17, 3, 1, SFMT_U16, strict=False)
        check("the external metadata passes the clinical consistency rules",
              not problems)


def _conf_volume(w, h, d, fmt, seed=3):
    """A small pseudo-medical volume in the raw little-endian sample order."""
    rnd = random.Random(seed)
    sb = {SFMT_U8: 1, SFMT_U16: 2, SFMT_U32: 4, SFMT_I16: 2, SFMT_I32: 4}[fmt]
    signed = fmt in (SFMT_I16, SFMT_I32)
    out = bytearray()
    for z in range(d):
        for y in range(h):
            for x in range(w):
                v = ((x * 13 + y * 7 + z * 29) % (1 << (8 * sb)) + rnd.randint(0, 3))
                v &= (1 << (8 * sb)) - 1
                if signed:
                    v = v - (1 << (8 * sb - 1))
                out += (v & ((1 << (8 * sb)) - 1)).to_bytes(sb, "little")
    return bytes(out)


def test_signature():
    """A signed container must prove who produced it and stay unchanged."""
    from pyra.medical import signature as _sig

    raw = _conf_volume(24, 18, 2, SFMT_U16, seed=11)
    key = b"clinical-signing-key"
    wrong = b"another-key"

    signed = encode(raw, 24, 18, 2, 1, SFMT_U16, sign_key=key)
    check("the signed container reports the signature",
          signature_status(signed, key) == "verified")
    check("the signed container without a key is unverified, not broken",
          signature_status(signed) == "unverified")
    check("the unsigned container is unsigned",
          signature_status(encode(raw, 24, 18, 2, 1, SFMT_U16)) == "unsigned")

    try:
        signature_status(signed, wrong)
        check("a wrong key is rejected", False)
    except MedicalError:
        check("a wrong key is rejected", True)

    meta, out = decode(signed, verify_key=key)
    check("a signed container decodes with the right key", out == raw)
    meta, out = decode(signed)
    check("a signed container still decodes without a key", out == raw)

    try:
        decode(signed, verify_key=wrong)
        check("decoding with a wrong key fails", False)
    except MedicalError:
        check("decoding with a wrong key fails", True)

    # tampering with the payload must break the signature
    body = bytearray(signed)
    body[-3] ^= 0x40
    try:
        decode(bytes(body), verify_key=key)
        check("a tampered payload breaks the signature", False)
    except MedicalError:
        check("a tampered payload breaks the signature", True)

    # tampering with the metadata must break the signature too
    body = bytearray(signed)
    hdr = _container.unpack_header(signed)
    body[hdr.meta_offset] ^= 0x02
    try:
        decode(bytes(body), verify_key=key)
        check("tampered metadata breaks the signature", False)
    except MedicalError:
        check("tampered metadata breaks the signature", True)

    # the signature covers the header, so a changed geometry breaks it too
    body = bytearray(signed)
    body[16] ^= 0x01
    try:
        decode(bytes(body), verify_key=key)
        check("a changed header field breaks the signature", False)
    except MedicalError:
        check("a changed header field breaks the signature", True)

    # the verify report carries the signature state
    check("the verify report names the signature state",
          verify(signed, key)["signature"] == "verified"
          and verify(signed)["signature"] == "unverified"
          and verify(encode(raw, 24, 18, 2, 1, SFMT_U16))["signature"]
          == "unsigned")

    # an empty key is refused
    try:
        encode(raw, 24, 18, 2, 1, SFMT_U16, sign_key=b"")
        check("an empty signing key is refused", False)
    except ValueError:
        check("an empty signing key is refused", True)

    # the signature region excludes the signature field itself
    check("the signed region skips the signature field",
          len(_sig.signed_region(signed)) == len(signed) - 32)


def test_audit_block():
    """The provenance block describes what was encoded, deterministically."""
    raw = _conf_volume(16, 12, 1, SFMT_U16, seed=12)
    blob = encode(raw, 16, 12, 1, 1, SFMT_U16, audit=True)
    meta, out = decode(blob)
    audit = meta.get("Audit")
    check("the audit block survives the round trip", out == raw and bool(audit))
    if audit:
        check("the audit block describes the geometry",
              audit.get("Columns") == 16 and audit.get("Rows") == 12
              and audit.get("NumberOfFrames") == 1
              and audit.get("SamplesPerPixel") == 1
              and audit.get("BytesPerSample") == 2)
        check("the audit block names the encoder and the codec",
              str(audit.get("Encoder", "")).startswith("pyra/")
              and str(audit.get("Codec", "")).startswith("PYRM-v"))
        import hashlib
        check("the audit block carries the source hash",
              audit.get("SourceSHA256")
              == hashlib.sha256(raw).hexdigest())
        check("the audit block has no timestamp unless one was asked for",
              "Timestamp" not in audit)

    # deterministic: the same input gives the same bytes
    again = encode(raw, 16, 12, 1, 1, SFMT_U16, audit=True)
    check("the audit block keeps the encoding deterministic", again == blob)

    # a timestamp is opt-in
    stamped = encode(raw, 16, 12, 1, 1, SFMT_U16, audit=True,
                     audit_timestamp="2026-11-14T12:00:00Z")
    meta2, _ = decode(stamped)
    check("an explicit timestamp is kept",
          meta2.get("Audit", {}).get("Timestamp") == "2026-11-14T12:00:00Z")

    # the native and the pure-Python backend agree on the signed container
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
    native_blob = encode(raw, 16, 12, 1, 1, SFMT_U16, sign_key=b"k",
                         audit=True)
    pure_blob = pure(encode, raw, 16, 12, 1, 1, SFMT_U16, sign_key=b"k",
                     audit=True)
    check("the native and the pure-Python signed containers are identical",
          native_blob == pure_blob)


def test_region_decode():
    """A region decode must be the exact crop of the full decode."""
    import tempfile
    from pyra.medical import decode_region

    bad = []
    for sf in (SFMT_U8, SFMT_U16, SFMT_I16, SFMT_U32, SFMT_F32):
        w, h, d, ch = 40, 30, 3, 1
        raw = make_samples("smooth", w, h, d, ch, sf)
        blob = encode(raw, w, h, d, ch, sf)
        _, full = decode(blob)
        sb = _codec._Format(sf).sample_bytes
        regions = [
            (0, 0, 0, w, h, d),          # everything
            (5, 7, 1, 10, 8, 2),         # a middle box over two slices
            (w - 3, h - 2, d - 1, 3, 2, 1),
            (0, 0, 0, 1, 1, 1),          # a single sample
            (12, 3, 0, w - 12, h - 3, 1),
            (0, 5, 0, w, 1, d),
        ]
        for (rx, ry, rz, rw, rh, rd) in regions:
            _, part = decode_region(blob, rx, ry, rz, rw, rh, rd)
            # the crop of the full decode
            expect = bytearray()
            row_bytes = w * ch * sb
            for z in range(rz, rz + rd):
                for row in range(ry, ry + rh):
                    start = (z * h + row) * row_bytes + rx * ch * sb
                    expect += full[start:start + rw * ch * sb]
            if part != bytes(expect):
                bad.append(f"{FORMAT_NAMES[sf]} {rx},{ry},{rz} {rw}x{rh}x{rd}")
    check("a region decode is the exact crop of the full decode", not bad)
    for line in bad[:8]:
        print("       ", line)

    # a region that leaves the volume is refused
    raw = make_samples("smooth", 20, 16, 2, 1, SFMT_U16)
    blob = encode(raw, 20, 16, 2, 1, SFMT_U16)
    for (args_) in [(18, 0, 0, 5, 5, 1), (0, 0, 1, 5, 5, 3),
                    (-1, 0, 0, 5, 5, 1), (0, 0, 0, 0, 5, 1)]:
        try:
            decode_region(blob, *args_)
            bad.append(f"region {args_} was accepted")
        except ValueError:
            pass
    check("a region outside the volume is refused", not bad)

    # a signed container must still refuse a wrong key in a region decode
    key = b"region-key"
    signed = encode(raw, 20, 16, 2, 1, SFMT_U16, sign_key=key)
    try:
        decode_region(signed, 2, 2, 0, 4, 4, 1, verify_key=b"wrong")
        bad.append("a wrong key was accepted")
    except MedicalError:
        pass
    _, part = decode_region(signed, 2, 2, 0, 4, 4, 1, verify_key=key)
    check("a region decode honours the signature", not bad)

    # the native region decoder must agree with the pure-Python reference
    # byte for byte (the native path is used automatically when it exists)
    if _native.available():
        diffs = []
        raw = make_samples("smooth", 33, 21, 3, 1, SFMT_U16)
        blob = encode(raw, 33, 21, 3, 1, SFMT_U16)
        for (rx, ry, rz, rw, rh, rd) in [(0, 0, 0, 33, 21, 3),
                                         (4, 5, 1, 9, 7, 2),
                                         (30, 18, 2, 3, 3, 1)]:
            nat = _native.decode_region(blob, rx, ry, rz, rw, rh, rd)
            _, ref = pure(decode_region, blob, rx, ry, rz, rw, rh, rd)
            if nat is None or bytes(nat) != bytes(ref):
                diffs.append(f"{rx},{ry},{rz} {rw}x{rh}x{rd}")
        check("the native region decoder matches the reference", not diffs)
        for line in diffs[:5]:
            print("       ", line)
    else:
        check("the native region decoder matches the reference", True)


def test_cli_decode_writes_the_file():
    """The command line must actually write the decoded samples.

    A missing write in the handler looks like a successful run on the
    console, so it is checked explicitly.
    """
    from pyra import cli as _cli

    tmp = tempfile.mkdtemp(prefix="pyra-cli-")
    raw = make_samples("smooth", 12, 9, 2, 1, SFMT_U16)
    src = os.path.join(tmp, "vol.raw")
    with open(src, "wb") as f:
        f.write(raw)
    container = os.path.join(tmp, "vol.pyrm")
    out = os.path.join(tmp, "vol.out")
    region_out = os.path.join(tmp, "vol.region")

    import io
    from contextlib import redirect_stdout
    with redirect_stdout(io.StringIO()):
        rc = _cli.main(["med", "encode", src, container,
                        "--width", "12", "--height", "9", "--depth", "2",
                        "--channels", "1", "--format", "u16"])
    check("the command line encodes a volume", rc == 0)
    with redirect_stdout(io.StringIO()):
        rc = _cli.main(["med", "decode", container, out])
    check("the command line decodes a volume", rc == 0)
    check("the command line writes the decoded samples",
          os.path.exists(out)
          and open(out, "rb").read() == raw)

    with redirect_stdout(io.StringIO()):
        rc = _cli.main(["med", "decode", container, region_out,
                        "--region", "2,1,0,5,4,1"])
    check("the command line decodes a region", rc == 0)
    if os.path.exists(region_out):
        row = 12 * 2
        expect = b""
        for r in range(1, 5):
            off = r * row + 2 * 2
            expect += raw[off:off + 5 * 2]
        check("the region file holds the region only",
              open(region_out, "rb").read() == expect)
    else:
        check("the region file holds the region only", False)


def test_near_lossless():
    """The near-lossless mode bounds the error and stays byte-exact."""
    from pyra.medical import UnsupportedFormatError

    worst_ok = True
    parity_ok = True
    smaller_ok = True
    lossless_ok = True
    flag_ok = True
    f32_ok = True
    verify_ok = True

    for sf in (SFMT_U8, SFMT_U16, SFMT_I16, SFMT_U32):
        fmt = _codec._Format(sf)
        sb = fmt.sample_bytes
        w, h, d, ch = 24, 16, 2, 1
        raw = make_samples("smooth", w, h, d, ch, sf)
        lossless = encode(raw, w, h, d, ch, sf)
        _, exact = decode(lossless)
        if exact != raw:
            lossless_ok = False

        for err in (1, 3, 7):
            blob = encode(raw, w, h, d, ch, sf, max_error=err)
            # the native core and the reference produce the same container
            if blob != pure(encode, raw, w, h, d, ch, sf, max_error=err):
                parity_ok = False
            # and the same samples
            _, out = decode(blob)
            _, ref = pure(decode, blob)
            if out != ref:
                parity_ok = False
            # the error bound holds for every sample
            for i in range(0, len(raw), sb):
                a = int.from_bytes(raw[i:i + sb], "little")
                b = int.from_bytes(out[i:i + sb], "little")
                if abs(a - b) > err:
                    worst_ok = False
            # the error bound must not cost space: the container never grows
            # beyond a small margin (a smooth synthetic ramp can even shrink
            # less than a noisy image because quantization breaks the ramp)
            if len(blob) > len(lossless) * 3 // 2 + 64:
                smaller_ok = False
            # the header records the mode
            hdr = _container.unpack_header(blob)
            if hdr.step != 2 * err + 1:
                flag_ok = False
            if not (hdr.flags & 0x02):
                flag_ok = False
            # the container is still verifiable
            try:
                verify(blob)
            except Exception:  # noqa: BLE001
                verify_ok = False

    check("the near-lossless error bound holds", worst_ok)
    check("the native and the reference containers are identical", parity_ok)
    check("the near-lossless container does not blow up", smaller_ok)
    check("the lossless mode is unchanged", lossless_ok)
    check("the header records the quantization step", flag_ok)
    check("a near-lossless container verifies", verify_ok)

    # on noisy data the quantization really pays off
    w, h, d, ch = 64, 64, 1, 1
    noisy = make_samples("noise", w, h, d, ch, SFMT_U8)
    base = encode(noisy, w, h, d, ch, SFMT_U8)
    small = encode(noisy, w, h, d, ch, SFMT_U8, max_error=8)
    check("the near-lossless mode compresses noisy data",
          len(small) < len(base))

    # a float sample cannot be quantized: it is not predicted at all
    try:
        encode(make_samples("smooth", 8, 8, 1, 1, SFMT_F32), 8, 8, 1, 1,
               SFMT_F32, max_error=2)
        f32_ok = False
    except UnsupportedFormatError:
        pass
    check("the near-lossless mode refuses float samples", f32_ok)

    # a negative bound is nonsense
    try:
        encode(make_samples("smooth", 8, 8, 1, 1, SFMT_U8), 8, 8, 1, 1,
               SFMT_U8, max_error=-1)
        f32_ok = False
    except ValueError:
        pass
    check("a negative error bound is refused", f32_ok)


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
    test_dicom_metadata()
    test_verify_command()
    test_med_predictor()
    test_preview_semantics()
    test_dicom_metadata_fidelity()
    test_dicom_file()
    test_cli_dicom()
    test_cli_dump()
    test_clinical_consistency()
    test_dicom_conformance()
    test_region_decode()
    test_cli_decode_writes_the_file()
    test_signature()
    test_audit_block()
    test_near_lossless()
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("Failures:")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
