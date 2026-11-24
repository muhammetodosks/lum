import argparse
import json
import sys
import time
import tempfile

from .api import encode_file, decode_file, _parse_pnm
from .container import LumenContainer
from .constants import (CS_GRAY, CS_RGB, CS_RGBA,
                        MAGIC,
                        PRED_NONE, PRED_LEFT, PRED_UP, PRED_AVERAGE,
                        PRED_PAETH, PRED_GRADIENT, PRED_MEDIAN,
                        METHOD_RAW, METHOD_RLE, METHOD_LZ, METHOD_MIX,
                        TRANSFORM_YCOCG, TRANSFORM_NONE,
                        FILTER_GLOBAL, FILTER_ADAPTIVE,
                        LumenError, CorruptedFileError)
from .pngbench import encode_png
from . import cloud as _cloud
from .medical import codec as _med_codec
from .medical import dicom as _med_dicom
from .medical import dicomfile as _med_dicomfile
from .medical import preview as _med_preview_mod
from .medical.constants import (SFMT_U16, METHOD_LZ as MED_METHOD_LZ,
                                METHOD_RAW as MED_METHOD_RAW,
                                PRED_SLICE, MedicalError)

_METHOD_NAMES = {METHOD_RAW: "raw", METHOD_RLE: "rle", METHOD_LZ: "lz", METHOD_MIX: "mix"}
_PRED_NAMES = {
    PRED_NONE: "none", PRED_LEFT: "left", PRED_UP: "up",
    PRED_AVERAGE: "average", PRED_PAETH: "paeth",
    PRED_GRADIENT: "gradient", PRED_MEDIAN: "median",
}
_CS_NAMES = {CS_GRAY: "gray", CS_RGB: "rgb", CS_RGBA: "rgba"}
_TRANSFORM_NAMES = {TRANSFORM_YCOCG: "ycocg", TRANSFORM_NONE: "none"}
_FILTER_NAMES = {FILTER_GLOBAL: "global", FILTER_ADAPTIVE: "adaptive"}

_PRED_BY_NAME = {v: k for k, v in _PRED_NAMES.items()}
_METHOD_BY_NAME = {v: k for k, v in _METHOD_NAMES.items()}
_CS_BY_NAME = {v: k for k, v in _CS_NAMES.items()}
_TRANSFORM_BY_NAME = {v: k for k, v in _TRANSFORM_NAMES.items()}
_FILTER_BY_NAME = {v: k for k, v in _FILTER_NAMES.items()}

# ------------------------------------------------------------ medical (LUMM)
_MED_FORMAT_NAMES = {0: "u8", 1: "u16", 2: "u32", 3: "i16", 4: "i32", 5: "f32"}
_MED_PRED_NAMES = {0: "none", 1: "left", 2: "up", 3: "average", 4: "paeth",
                   5: "gradient", 6: "median", 7: "slice"}
_MED_METHOD_NAMES = {MED_METHOD_RAW: "raw", MED_METHOD_LZ: "lz"}
_MED_FORMAT_BY_NAME = {v: k for k, v in _MED_FORMAT_NAMES.items()}
_MED_PRED_BY_NAME = {v: k for k, v in _MED_PRED_NAMES.items()}
_MED_METHOD_BY_NAME = {v: k for k, v in _MED_METHOD_NAMES.items()}

# ------------------------------------------------------------- cloud (LUMC)
_CLOUD_FORMAT_NAMES = {0: "u8", 1: "u16", 2: "u32", 3: "i16", 4: "i32",
                       5: "f32"}
_CLOUD_PRED_NAMES = {0: "none", 1: "left", 2: "up", 3: "average", 4: "paeth",
                     5: "gradient", 6: "median", 7: "time", 8: "band"}
_CLOUD_METHOD_NAMES = {0: "raw", 1: "lz"}
_CLOUD_FORMAT_BY_NAME = {v: k for k, v in _CLOUD_FORMAT_NAMES.items()}
_CLOUD_PRED_BY_NAME = {v: k for k, v in _CLOUD_PRED_NAMES.items()}
_CLOUD_METHOD_BY_NAME = {v: k for k, v in _CLOUD_METHOD_NAMES.items()}
_CLOUD_BAND_TYPES = ("reflectance", "radiance", "brightness_temperature",
                     "quality_flag", "classification", "precipitation",
                     "albedo", "elevation")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lum", description="LUMEN image format tool")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("encode", help="encode a PNM image into a .lum file")
    e.add_argument("input")
    e.add_argument("output")
    e.add_argument("--method", choices=["auto", "raw", "rle", "lz", "mix"], default="auto")
    e.add_argument("--predictor", choices=["auto"] + list(_PRED_BY_NAME), default="auto")
    e.add_argument("--transform", choices=["auto"] + list(_TRANSFORM_BY_NAME), default="auto")
    e.add_argument("--filter", choices=["auto"] + list(_FILTER_BY_NAME), default="auto")
    e.add_argument("--tile", type=int, default=256)
    e.add_argument("--colorspace", choices=["auto", "gray", "rgb", "rgba"], default="auto")
    e.add_argument("--quality", type=int, default=0, metavar="Q",
                   help="encode lossy with the v6 codec at quality 1..100 "
                        "(0 keeps the lossless v3 codec)")
    e.add_argument("--subsampling", choices=["auto", "444", "420"], default="auto",
                   help="chroma subsampling for the lossy codec "
                        "(auto = 4:4:4, which measures smaller at equal PSNR)")

    d = sub.add_parser("decode", help="decode a .lum file into a PNM image")
    d.add_argument("input")
    d.add_argument("output")

    i = sub.add_parser("inspect", help="print container metadata and tile count")
    i.add_argument("input")

    v = sub.add_parser("verify", help="verify structural integrity (magic/CRC)")
    v.add_argument("input")

    b = sub.add_parser("benchmark", help="encode/decode timings and PNG size comparison")
    b.add_argument("input")
    b.add_argument("--method", choices=["auto", "raw", "rle", "lz", "mix"], default="auto")
    b.add_argument("--predictor", choices=["auto"] + list(_PRED_BY_NAME), default="auto")
    b.add_argument("--transform", choices=["auto"] + list(_TRANSFORM_BY_NAME), default="auto")
    b.add_argument("--filter", choices=["auto"] + list(_FILTER_BY_NAME), default="auto")
    b.add_argument("--tile", type=int, default=256)

    med = sub.add_parser("med", help="LUMM medical container tools")
    med_sub = med.add_subparsers(dest="med_cmd", required=True)

    me = med_sub.add_parser("encode", help="encode raw samples into a .lumm file")
    me.add_argument("input", help="raw sample file (little-endian)")
    me.add_argument("output")
    me.add_argument("--width", type=int, required=True)
    me.add_argument("--height", type=int, required=True)
    me.add_argument("--depth", type=int, default=1)
    me.add_argument("--channels", type=int, default=1)
    me.add_argument("--format", choices=list(_MED_FORMAT_BY_NAME), default="u16")
    me.add_argument("--tile", type=int, default=256)
    me.add_argument("--predictor", choices=["auto"] + list(_MED_PRED_BY_NAME),
                    default="auto")
    me.add_argument("--method", choices=["auto", "raw", "lz"], default="auto")
    me.add_argument("--max-error", type=int, default=0, metavar="N",
                    help="switch to the near-lossless mode: every sample is "
                         "reconstructed with an absolute error of at most N "
                         "(0 = lossless)")
    me.add_argument("--meta", action="append", default=[],
                    metavar="KEY=VALUE", help="metadata entry (repeatable)")
    me.add_argument("--dicom", action="store_true",
                    help="build a complete DICOM compatible metadata block "
                         "from the geometry and the clinical options below")
    me.add_argument("--patient-id", default=None)
    me.add_argument("--patient-name", default=None)
    me.add_argument("--modality", default=None)
    me.add_argument("--study-uid", default=None)
    me.add_argument("--series-uid", default=None)
    me.add_argument("--sop-uid", default=None)
    me.add_argument("--study-date", default=None)
    me.add_argument("--slice-thickness", type=float, default=None)
    me.add_argument("--rescale-slope", type=float, default=1.0)
    me.add_argument("--rescale-intercept", type=float, default=0.0)
    me.add_argument("--window-center", type=float, default=None)
    me.add_argument("--window-width", type=float, default=None)
    me.add_argument("--photometric", choices=["mono1", "mono2", "rgb"],
                    default=None)
    me.add_argument("--require-dicom", action="store_true",
                    help="refuse to write a container whose metadata does not "
                         "pass the clinical validation")
    me.add_argument("--sign-key", default=None, metavar="FILE",
                    help="sign the container with the HMAC-SHA256 key in FILE "
                         "(raw bytes)")
    me.add_argument("--audit", action="store_true",
                    help="add a deterministic provenance block to the metadata")
    me.add_argument("--audit-timestamp", default=None, metavar="ISO8601",
                    help="timestamp for the provenance block (breaks the "
                         "byte-exact reproducibility)")

    md = med_sub.add_parser("decode", help="decode a .lumm file into raw samples")
    md.add_argument("input")
    md.add_argument("output")
    md.add_argument("--verify-key", default=None, metavar="FILE",
                    help="verify the HMAC-SHA256 signature with the key in "
                         "FILE (raw bytes); a wrong key is refused")
    md.add_argument("--region", default=None, metavar="X,Y,Z,W,H,D",
                    help="decode only this region of the volume "
                         "(column,row,slice,width,height,depth); the tiles "
                         "outside are never touched")

    mi = med_sub.add_parser("info", help="print container metadata")
    mi.add_argument("input")

    mdc = med_sub.add_parser("to-dicom",
                             help="convert a .lumm container into a DICOM file")
    mdc.add_argument("input")
    mdc.add_argument("output")

    mdf = med_sub.add_parser("from-dicom",
                             help="convert a DICOM file into a .lumm container")
    mdf.add_argument("input")
    mdf.add_argument("output")
    mdf.add_argument("--tile", type=int, default=256)

    mvf = med_sub.add_parser("verify", help="check every integrity guarantee")
    mvf.add_argument("input")
    mvf.add_argument("--key", default=None, metavar="FILE",
                     help="also verify the HMAC-SHA256 signature with the key "
                          "in FILE (raw bytes)")

    mdu = med_sub.add_parser("dump",
                             help="print the container as JSON (geometry, "
                                  "integrity and metadata)")
    mdu.add_argument("input")
    mdu.add_argument("--metadata-only", action="store_true",
                     help="print just the metadata block")

    mv = med_sub.add_parser("preview", help="render one slice as an 8-bit PGM")
    mv.add_argument("input")
    mv.add_argument("output")
    mv.add_argument("--slice", type=int, default=0)
    mv.add_argument("--window", type=float, default=None,
                    help="display window (default: the WindowWidth of the metadata)")
    mv.add_argument("--level", type=float, default=None,
                    help="display level (default: the WindowCenter of the metadata)")
    cloud = sub.add_parser("cloud", help="LUMC cloud/EO container tools")
    cloud_sub = cloud.add_subparsers(dest="cloud_cmd", required=True)

    ce = cloud_sub.add_parser("encode",
                              help="encode a raw cube into a .lumc file")
    ce.add_argument("input", help="raw cube file (LUMX)")
    ce.add_argument("output")
    ce.add_argument("--width", type=int, required=True)
    ce.add_argument("--height", type=int, required=True)
    ce.add_argument("--bands", type=int, required=True)
    ce.add_argument("--scenes", type=int, default=1)
    ce.add_argument("--format", choices=list(_CLOUD_FORMAT_BY_NAME),
                    default="u16")
    ce.add_argument("--tile", type=int, default=256)
    ce.add_argument("--predictor", choices=["auto"] + list(_CLOUD_PRED_BY_NAME),
                    default="auto")
    ce.add_argument("--method", choices=["auto", "raw", "lz"], default="auto")
    ce.add_argument("--band-name", action="append", default=[],
                    metavar="NAME",
                    help="name of a band (repeatable, in band order)")
    ce.add_argument("--band-wavelength", action="append", default=[],
                    metavar="NM", type=float,
                    help="wavelength of a band in nm (repeatable)")
    ce.add_argument("--band-type", action="append", default=[],
                    metavar="TYPE", choices=list(_CLOUD_BAND_TYPES),
                    help="type of a band (repeatable)")
    ce.add_argument("--scene-time", action="append", default=[],
                    metavar="UNIX_TS", type=int,
                    help="timestamp of a scene (repeatable)")
    ce.add_argument("--lon0", action="append", default=[], metavar="DEG",
                    type=float, help="longitude of a scene origin (repeatable)")
    ce.add_argument("--lat0", action="append", default=[], metavar="DEG",
                    type=float, help="latitude of a scene origin (repeatable)")
    ce.add_argument("--meta", action="append", default=[], metavar="KEY=VALUE",
                    help="metadata entry (repeatable)")

    cd = cloud_sub.add_parser("decode",
                              help="decode a .lumc file into raw samples")
    cd.add_argument("input")
    cd.add_argument("output")
    cd.add_argument("--scene", type=int, default=None,
                    help="decode only this scene")
    cd.add_argument("--band", type=int, default=None,
                    help="decode only this band (with --scene)")

    ci = cloud_sub.add_parser("info", help="print the container metadata")
    ci.add_argument("input")

    cs = cloud_sub.add_parser("stats",
                               help="per band statistics of a cube")
    cs.add_argument("input", help="a .lumc file or a raw cube (LUMX)")
    cs.add_argument("--band", type=int, default=None,
                    help="only this band (default: every band)")
    cs.add_argument("--histogram", action="store_true",
                    help="also print the 256 bin histogram")

    cr = cloud_sub.add_parser("render",
                               help="render a false colour composite as a PPM")
    cr.add_argument("input")
    cr.add_argument("output")
    cr.add_argument("--scene", type=int, default=0)
    cr.add_argument("--red", type=int, default=0)
    cr.add_argument("--green", type=int, default=1)
    cr.add_argument("--blue", type=int, default=2)
    cr.add_argument("--window", type=float, default=0.0)
    cr.add_argument("--level", type=float, default=0.0)
    return p


def _inspect(input_path: str) -> None:
    with open(input_path, "rb") as f:
        data = f.read()
    if len(data) >= 12 and data[:len(MAGIC)] == MAGIC \
            and int.from_bytes(data[8:12], "big") == 5:
        from . import lossy
        meta = lossy.info(data)
        if meta is None:
            print(f"VERIFICATION FAILED: malformed LUMEN v6 container")
            return
        print(f"=== LUMEN Image Inspection: {input_path} ===")
        print(f"  version: 5 (lossy DCT)")
        print(f"  dimensions: {meta['width']}x{meta['height']} @ "
              f"{meta['bit_depth']}-bit")
        print(f"  color_space: {_CS_NAMES.get(meta['color_space'], meta['color_space'])}")
        print(f"  quality: {meta['quality']}")
        print(f"  chroma: {'4:2:0' if meta['subsampling'] == 2 else '4:4:4'}")
        print(f"  planes: {meta['num_planes']}")
        print(f"  file_size: {len(data)} bytes")
        return
    meta, tiles = LumenContainer.unpack_file(data)
    print(f"=== LUMEN Image Inspection: {input_path} ===")
    print(f"  version: {meta['version']}")
    print(f"  dimensions: {meta['width']}x{meta['height']} @ {meta['bit_depth']}-bit")
    cs_name = _CS_NAMES.get(meta["color_space"], str(meta["color_space"]))
    pred_name = _PRED_NAMES.get(meta["pred_type"], str(meta["pred_type"]))
    method_name = _METHOD_NAMES.get(meta["method"], str(meta["method"]))
    print(f"  color_space: {cs_name}")
    print(f"  predictor: {pred_name}")
    print(f"  transform: {_TRANSFORM_NAMES.get(meta.get('transform'), meta.get('transform'))}")
    print(f"  filter: {_FILTER_NAMES.get(meta.get('filter_mode'), meta.get('filter_mode'))}")
    print(f"  method: {method_name}")
    print(f"  tile_size: {meta['tile_size']}")
    print(f"  num_tiles: {len(tiles)}")
    print(f"  file_size: {len(data)} bytes")


def _benchmark(args) -> int:
    magic, width, height, pixels, bit_depth, max_val = _parse_pnm(args.input)
    color_space = CS_GRAY if magic == b"P5" else CS_RGB
    channels = 1 if color_space == CS_GRAY else 3
    raw_size = width * height * channels * (bit_depth // 8)
    print(f"Benchmarking {args.input}: {width}x{height} {bit_depth}-bit "
          f"{_CS_NAMES[color_space]} ({raw_size} raw bytes)")

    # PNG reference for the same pixel data (applied Paeth/adaptive filter +
    # zlib deflate, same as a practical PNG encoder)
    t0 = time.perf_counter()
    png_bytes = encode_png(pixels, width, height, bit_depth, color_space)
    t_png = time.perf_counter() - t0

    method = _METHOD_BY_NAME.get(args.method, -1)  # "auto" -> -1
    pred = _PRED_BY_NAME.get(args.predictor, -1)  # "auto" -> -1
    transform = _TRANSFORM_BY_NAME.get(args.transform, -1)
    filter_mode = _FILTER_BY_NAME.get(args.filter, -1)

    with tempfile.NamedTemporaryFile(suffix=".lum") as tmp_lum, \
         tempfile.NamedTemporaryFile(suffix=".ppm" if magic == b"P6" else ".pgm") as tmp_out:
        t0 = time.perf_counter()
        encode_file(args.input, tmp_lum.name, color_space=color_space,
                    pred_type=pred, tile_size=args.tile, method=method,
                    transform=transform, filter_mode=filter_mode)
        t_enc = time.perf_counter() - t0

        with open(tmp_lum.name, "rb") as f:
            lum_bytes = f.read()
        meta, _tiles = LumenContainer.unpack_file(lum_bytes)

        t0 = time.perf_counter()
        decode_file(tmp_lum.name, tmp_out.name)
        t_dec = time.perf_counter() - t0

        # Roundtrip check (byte-exact only when maxval is the full range)
        with open(args.input, "rb") as f:
            src = f.read()
        with open(tmp_out.name, "rb") as f:
            dst = f.read()
        full_range = max_val == (1 << bit_depth) - 1
        roundtrip = "OK" if (full_range and src == dst) else ("SKIP (maxval normalization)" if not full_range else "MISMATCH")

        raw_cmp = raw_size
        print(f"  encode: {t_enc:.3f} s ({raw_size / t_enc / 1024:.0f} KiB/s)")
        print(f"  decode: {t_dec:.3f} s ({raw_size / t_dec / 1024:.0f} KiB/s)")
        print(f"  raw size:   {raw_cmp:>9} bytes")
        print(f"  LUMEN size:  {len(lum_bytes):>9} bytes  "
              f"(method {_METHOD_NAMES.get(meta['method'], meta['method'])}, "
              f"ratio {raw_cmp / max(1, len(lum_bytes)):.2f}x)")
        print(f"  PNG size:   {len(png_bytes):>9} bytes  (ratio {raw_cmp / max(1, len(png_bytes)):.2f}x, "
              f"png encode {t_png:.3f} s, C zlib deflate)")
        if full_range:
            delta = len(lum_bytes) - len(png_bytes)
            pct = 100.0 * delta / max(1, len(png_bytes))
            print(f"  LUMEN vs PNG: {delta:+d} bytes ({pct:+.1f}%)")
        print(f"  roundtrip: {roundtrip}")
    return 0


# ------------------------------------------------------------- medical (LUMM)

def _med_meta(pairs: list[str]) -> dict:
    """Parse `KEY=VALUE` metadata arguments."""
    meta = {}
    for item in pairs:
        if "=" not in item:
            raise ValueError(f"metadata entry must be KEY=VALUE: {item!r}")
        key, value = item.split("=", 1)
        meta[key.strip()] = value.strip()
    return meta


def _med_key(path):
    """Read a signing key from a file (raw bytes)."""
    with open(path, "rb") as f:
        key = f.read()
    if not key:
        raise ValueError(f"the key file {path} is empty")
    return key


def _med_encode(args) -> int:
    with open(args.input, "rb") as f:
        samples = f.read()
    fmt = _MED_FORMAT_BY_NAME[args.format]
    predictor = _MED_PRED_BY_NAME.get(args.predictor, -1)
    method = _MED_METHOD_BY_NAME.get(args.method, -1)
    sign_key = _med_key(args.sign_key) if args.sign_key else None

    metadata = _med_meta(args.meta)
    if args.dicom:
        # The technical identifiers are generated when they are not given:
        # they carry no patient identity, and a study/series/instance
        # hierarchy that is consistent by construction is worth far more
        # than three empty strings. Patient identity is never invented.
        study_uid = args.study_uid or _med_dicom.generate_uid()
        series_uid = args.series_uid or _med_dicom.generate_uid(
            "series:" + study_uid)
        sop_uid = args.sop_uid or _med_dicom.generate_uid(
            "instance:" + series_uid)
        # The geometry dependent attributes are derived, so the result is
        # always self consistent; the explicit --meta entries win.
        clinical = _med_dicom.build_dicom_metadata(
            args.width, args.height, args.depth, args.channels, fmt,
            patient_id=args.patient_id or "",
            patient_name=args.patient_name or "",
            modality=args.modality or "",
            study_uid=study_uid,
            series_uid=series_uid,
            sop_uid=sop_uid,
            study_date=args.study_date or "",
            slice_thickness=args.slice_thickness,
            rescale_slope=args.rescale_slope,
            rescale_intercept=args.rescale_intercept,
            window_center=args.window_center or 0.0,
            window_width=args.window_width or 0.0,
            photometric={"mono1": "MONOCHROME1", "mono2": "MONOCHROME2",
                         "rgb": "RGB"}.get(args.photometric or "", ""))
        clinical.update(metadata)
        metadata = clinical

    if args.require_dicom:
        problems = _med_dicom.validate_dicom_metadata(
            metadata, args.width, args.height, args.depth, args.channels,
            fmt, strict=True)
        if problems:
            for problem in problems:
                print(f"lum: error: metadata: {problem}", file=sys.stderr)
            return 1

    blob = _med_codec.encode(samples, args.width, args.height, args.depth,
                              args.channels, fmt, metadata=metadata,
                              tile_size=args.tile, predictor=predictor,
                              method=method,
                              require_dicom=args.require_dicom,
                              sign_key=sign_key, audit=args.audit,
                              audit_timestamp=args.audit_timestamp,
                              max_error=args.max_error)
    with open(args.output, "wb") as f:
        f.write(blob)
    info = _med_codec.info(blob)
    print(f"Encoded {args.input} -> {args.output}")
    print(f"  geometry: {info['width']}x{info['height']}x{info['depth']} "
          f"ch={info['channels']} {_MED_FORMAT_NAMES.get(info['sample_format'])}")
    print(f"  size: {len(blob)} bytes, tiles: {info['num_tiles']}")
    step = info.get("step", 1)
    if step > 1:
        print(f"  near-lossless: maximum absolute error "
              f"{(step - 1) // 2} (quantization step {step})")
    if args.dicom or args.require_dicom:
        print(f"  dicom compatible: "
              f"{_med_dicom.is_dicom_compatible(metadata)}")
    return 0


def _med_decode(args) -> int:
    with open(args.input, "rb") as f:
        blob = f.read()
    key = _med_key(args.verify_key) if args.verify_key else None
    info = _med_codec.info(blob)
    if args.region:
        try:
            parts = [int(p) for p in args.region.split(",")]
        except ValueError:
            print("lum: error: --region needs six integers "
                  "X,Y,Z,W,H,D", file=sys.stderr)
            return 1
        if len(parts) != 6:
            print("lum: error: --region needs six integers "
                  "X,Y,Z,W,H,D", file=sys.stderr)
            return 1
        rx, ry, rz, rw, rh, rd = parts
        metadata, samples = _med_codec.decode_region(
            blob, rx, ry, rz, rw, rh, rd, verify_key=key)
        print(f"Decoded region of {args.input} -> {args.output}")
        print(f"  region: {rw}x{rh}x{rd} at ({rx}, {ry}, {rz})")
    else:
        metadata, samples = _med_codec.decode(blob, verify_key=key)
        print(f"Decoded {args.input} -> {args.output}")
        print(f"  geometry: {info['width']}x{info['height']}x{info['depth']} "
              f"ch={info['channels']} "
              f"{_MED_FORMAT_NAMES.get(info['sample_format'])}")
    with open(args.output, "wb") as f:
        f.write(samples)
    print(f"  samples: {len(samples)} bytes")
    if metadata:
        print(f"  metadata: {metadata}")
    return 0


def _med_info(args) -> int:
    with open(args.input, "rb") as f:
        blob = f.read()
    info = _med_codec.info(blob)
    print(f"=== LUMM Medical Inspection: {args.input} ===")
    for key in ("width", "height", "depth", "channels", "sample_format",
                "bits", "tile_size", "num_tiles"):
        print(f"  {key}: {info[key]}")
    print(f"  format: {_MED_FORMAT_NAMES.get(info['sample_format'])}")
    step = info.get("step", 1)
    print(f"  mode: {'lossless' if step <= 1 else 'near-lossless'}"
          + ("" if step <= 1 else
             f" (maximum absolute error {(step - 1) // 2})"))
    print(f"  file_size: {len(blob)} bytes")
    metadata = _med_codec.decode(blob)[0]
    summary = _med_dicom.describe(metadata)
    if summary:
        print("  --- clinical attributes ---")
        for key, value in summary.items():
            print(f"  {key}: {value}")
        if _med_dicom.is_dicom_compatible(
                metadata, info["width"], info["height"], info["depth"],
                info["channels"], info["sample_format"]):
            print("  dicom_compatible: yes")
        else:
            print("  dicom_compatible: no (see the missing attributes above)")
    return 0


def _med_to_dicom(args) -> int:
    with open(args.input, "rb") as f:
        blob = f.read()
    metadata, samples = _med_codec.decode(blob)
    info = _med_codec.info(blob)
    if not metadata.get("SOPInstanceUID"):
        raise MedicalError(
            "the container has no SOPInstanceUID; a DICOM file needs one "
            "(use the CLI options of 'med encode --dicom')")
    out = _med_dicomfile.write_dicom(
        samples, info["width"], info["height"], info["depth"],
        info["channels"], info["sample_format"], metadata)
    with open(args.output, "wb") as f:
        f.write(out)
    print(f"Converted {args.input} -> {args.output}")
    print(f"  transfer syntax: {_med_dicomfile.EXPLICIT_VR_LITTLE_ENDIAN}")
    print(f"  size: {len(out)} bytes")
    return 0


def _med_from_dicom(args) -> int:
    with open(args.input, "rb") as f:
        blob = f.read()
    samples, metadata = _med_dicomfile.read_dicom(blob)
    width = int(metadata.get("Columns", 0))
    height = int(metadata.get("Rows", 0))
    depth = int(metadata.get("NumberOfFrames", 1))
    channels = int(metadata.get("SamplesPerPixel", 1))
    signed = int(metadata.get("PixelRepresentation", 0)) == 1
    bits = int(metadata.get("BitsAllocated", 16))
    sample_format = {8: 0, 16: 3 if signed else 1, 32: 4 if signed else 2}[bits]
    out = _med_codec.encode(samples, width, height, depth, channels,
                             sample_format, metadata=metadata,
                             tile_size=args.tile)
    with open(args.output, "wb") as f:
        f.write(out)
    print(f"Converted {args.input} -> {args.output}")
    print(f"  geometry: {width}x{height}x{depth} ch={channels}")
    print(f"  size: {len(out)} bytes")
    return 0


def _med_verify(args) -> int:
    with open(args.input, "rb") as f:
        blob = f.read()
    key = _med_key(args.key) if args.key else None
    report = _med_codec.verify(blob, key)
    print(f"VERIFICATION SUCCESSFUL: {args.input} is valid.")
    print(f"  payload: {report['payload_bytes']} bytes in "
          f"{report['num_tiles']} tiles")
    print(f"  sha256 content hash: verified")
    print(f"  signature: {report['signature']}")
    print(f"  dicom_compatible: {'yes' if report['dicom_compatible'] else 'no'}")
    return 0


def _med_dump(args) -> int:
    """Print the container as a JSON document.

    The document holds the geometry, the integrity state and the clinical
    metadata, so it can be piped into any JSON tool. With
    `--metadata-only` just the metadata block is printed, which is what
    `jq` users usually want.
    """
    with open(args.input, "rb") as f:
        blob = f.read()
    metadata = _med_codec.decode(blob)[0]
    if args.metadata_only:
        print(json.dumps(metadata, indent=2, sort_keys=True, default=str))
        return 0
    info = _med_codec.info(blob)
    report = _med_codec.verify(blob)
    document = {
        "container": {
            "magic": "LUMM",
            "version": report.get("version"),
            "width": info["width"],
            "height": info["height"],
            "depth": info["depth"],
            "channels": info["channels"],
            "sample_format": info["sample_format"],
            "bits": info["bits"],
            "tile_size": info["tile_size"],
            "num_tiles": info["num_tiles"],
            "file_bytes": len(blob),
            "payload_bytes": report.get("payload_bytes"),
            "metadata_bytes": report.get("metadata_bytes"),
        },
        "integrity": {
            "ok": report.get("ok"),
            "dicom_compatible": report.get("dicom_compatible"),
        },
        "metadata": metadata,
    }
    print(json.dumps(document, indent=2, sort_keys=True, default=str))
    return 0


def _med_preview(args) -> int:
    with open(args.input, "rb") as f:
        blob = f.read()
    info = _med_codec.info(blob)
    metadata, samples = _med_codec.decode(blob)
    # The window, the calibration and the photometric interpretation come
    # from the metadata; the command line overrides the window.
    window = args.window if args.window is not None else None
    level = args.level if args.level is not None else None
    pixels = _med_preview_mod.preview_from_metadata(
        samples, info["width"], info["height"], info["depth"],
        info["channels"], info["sample_format"], metadata, args.slice,
        window, level)
    if pixels is None:
        print("lum: error: cannot render this slice", file=sys.stderr)
        return 1
    header = f"P5\n{info['width']} {info['height']}\n255\n".encode()
    with open(args.output, "wb") as f:
        f.write(header)
        f.write(pixels)
    meta_center, meta_width = _med_dicom.window(metadata)
    eff_level = args.level if args.level is not None else meta_center
    eff_window = args.window if args.window is not None else meta_width
    print(f"Preview {args.input} slice {args.slice} -> {args.output}")
    print(f"  window: {eff_window}, level: {eff_level}")
    print(f"  photometric: {_med_dicom.photometric(metadata)}")
    return 0


# ------------------------------------------------------------- cloud (LUMC)

def _cloud_load_cube(path: str):
    """Load a cube either from a LUMC container or from a raw LUMX file.

    Returns (samples, width, height, bands, scenes, sample_format).
    """
    with open(path, "rb") as f:
        blob = f.read()
    if blob[:8] == _cloud.MAGIC:
        info, samples = _cloud.decode(blob)
        return (samples, info["width"], info["height"], info["bands"],
                info["scenes"], info["sample_format"])
    samples, width, height, bands, scenes, fmt = _cloud.raw_read(path)
    return samples, width, height, bands, scenes, fmt


def _cloud_encode(args) -> int:
    samples, width, height, bands, scenes, fmt = _cloud_load_cube(args.input)
    if (width, height, bands, scenes) != (args.width, args.height, args.bands,
                                         args.scenes):
        print(f"lum: error: the file holds {width}x{height} x{bands} bands "
              f"x{scenes} scenes, not {args.width}x{args.height} x{args.bands} "
              f"x{args.scenes}", file=sys.stderr)
        return 1
    if _CLOUD_FORMAT_NAMES.get(fmt, "?") != args.format:
        print(f"lum: error: the file holds "
              f"{_CLOUD_FORMAT_NAMES.get(fmt, '?')} samples, not {args.format}",
              file=sys.stderr)
        return 1
    band_meta = []
    for i in range(bands):
        entry = {"name": (args.band_name[i] if i < len(args.band_name) else ""),
                 "type": 0}
        if i < len(args.band_wavelength):
            entry["wavelength_nm"] = args.band_wavelength[i]
        if i < len(args.band_type):
            entry["type"] = _CLOUD_BAND_TYPES.index(args.band_type[i])
        band_meta.append(entry)
    scene_meta = []
    for i in range(scenes):
        entry = {}
        if i < len(args.scene_time):
            entry["timestamp"] = args.scene_time[i]
        if i < len(args.lon0):
            entry["lon0"] = args.lon0[i]
        if i < len(args.lat0):
            entry["lat0"] = args.lat0[i]
        scene_meta.append(entry)
    blob = _cloud.encode(samples, width, height, bands, scenes, fmt,
                         band_meta=band_meta, scene_meta=scene_meta,
                         metadata=_med_meta(args.meta), tile_size=args.tile,
                         predictor=_CLOUD_PRED_BY_NAME.get(args.predictor, -1),
                         method=_CLOUD_METHOD_BY_NAME.get(args.method, -1))
    with open(args.output, "wb") as f:
        f.write(blob)
    print(f"Encoded {args.input} -> {args.output}")
    print(f"  geometry: {width}x{height} x{bands} bands x{scenes} scenes "
          f"{_CLOUD_FORMAT_NAMES.get(fmt, '?')}")
    print(f"  size: {len(blob)} bytes")
    return 0


def _cloud_decode(args) -> int:
    with open(args.input, "rb") as f:
        blob = f.read()
    if args.scene is not None and args.band is not None:
        samples = _cloud.read_band(blob, args.scene, args.band)
        info = _cloud.info(blob)
        _cloud.raw_write(args.output, samples, info["width"], info["height"], 1,
                         1, info["sample_format"])
        print(f"Decoded band {args.band} of scene {args.scene} -> {args.output}")
        print(f"  samples: {len(samples)} bytes")
        return 0
    info, samples = _cloud.decode(blob)
    _cloud.raw_write(args.output, samples, info["width"], info["height"],
                     info["bands"], info["scenes"], info["sample_format"])
    print(f"Decoded {args.input} -> {args.output}")
    print(f"  geometry: {info['width']}x{info['height']} x{info['bands']} "
          f"bands x{info['scenes']} scenes")
    print(f"  samples: {len(samples)} bytes")
    return 0


def _cloud_info(args) -> int:
    with open(args.input, "rb") as f:
        blob = f.read()
    info = _cloud.info(blob)
    print(f"=== LUMC Cloud Inspection: {args.input} ===")
    for key in ("version", "width", "height", "bands", "scenes",
                "sample_format", "bits", "tile_size", "tiles_x", "tiles_y",
                "num_entries", "predictor"):
        print(f"  {key}: {info[key]}")
    print(f"  format: {_CLOUD_FORMAT_NAMES.get(info['sample_format'], '?')}")
    print(f"  file_size: {len(blob)} bytes")
    if info.get("band_table"):
        print("  bands:")
        for i, band in enumerate(info["band_table"]):
            print(f"    [{i}] {band['name'] or '-'} "
                  f"type={band['type']} units={band['units']} "
                  f"wavelength={band['wavelength_nm']} nm")
    if info.get("scene_table"):
        print("  scenes:")
        for i, scene in enumerate(info["scene_table"]):
            print(f"    [{i}] t={scene['timestamp']} "
                  f"lon0={scene['lon0']} lat0={scene['lat0']} "
                  f"pixel={scene['pixel_size_x']}x{scene['pixel_size_y']}")
    if info.get("metadata"):
        print(f"  metadata: {info['metadata']}")
    return 0


def _cloud_stats(args) -> int:
    samples, width, height, bands, scenes, fmt = _cloud_load_cube(args.input)
    wanted = range(bands) if args.band is None else [args.band]
    print(f"=== cube statistics: {args.input} ===")
    print(f"  {width}x{height} x{bands} bands x{scenes} scenes "
          f"{_CLOUD_FORMAT_NAMES.get(fmt, '?')}")
    for band in wanted:
        stats = _cloud.band_stats(samples, width, height, bands, scenes, fmt,
                                  band)
        print(f"  band {band}: min={stats['minimum']:.4g} "
              f"max={stats['maximum']:.4g} mean={stats['mean']:.4g} "
              f"stddev={stats['stddev']:.4g} n={stats['count']}")
        if args.histogram:
            hist = stats["histogram"]
            peak = max(hist) if hist else 0
            for start in range(0, 256, 32):
                chunk = hist[start:start + 32]
                print("    %3d: %s" % (start, " ".join(
                    f"{c:>7d}" for c in chunk)))
            print(f"    peak bin count: {peak}")
    return 0


def _cloud_render(args) -> int:
    samples, width, height, bands, scenes, fmt = _cloud_load_cube(args.input)
    if args.scene >= scenes:
        print(f"lum: error: scene {args.scene} is outside the cube "
              f"({scenes} scenes)", file=sys.stderr)
        return 1
    rgb = _cloud.render_rgb(samples, width, height, bands, scenes, fmt,
                            args.scene, args.red, args.green, args.blue,
                            args.window, args.level, args.window, args.level,
                            args.window, args.level)
    with open(args.output, "wb") as f:
        f.write(f"P6\n{width} {height}\n255\n".encode())
        f.write(rgb)
    print(f"Rendered {args.input} scene {args.scene} -> {args.output}")
    print(f"  bands: r={args.red} g={args.green} b={args.blue} "
          f"window={args.window} level={args.level}")
    return 0


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.cmd == "encode":
            method = _METHOD_BY_NAME.get(args.method, -1)  # "auto" -> -1
            pred = _PRED_BY_NAME.get(args.predictor, -1)  # "auto" -> -1
            transform = _TRANSFORM_BY_NAME.get(args.transform, -1)
            filter_mode = _FILTER_BY_NAME.get(args.filter, -1)
            cs = _CS_BY_NAME.get(args.colorspace, -1)
            sub = {"auto": 0, "444": 1, "420": 2}[args.subsampling]
            print(f"Encoding {args.input} -> {args.output}...")
            if args.quality:
                from .api import encode_file as _encode_file
                _encode_file(args.input, args.output, color_space=cs,
                             quality=args.quality, subsampling=sub)
                with open(args.output, "rb") as f:
                    out_bytes = f.read()
                from . import lossy
                stored = lossy.info(out_bytes) or {}
                sub_name = "4:2:0" if stored.get("subsampling") == 2 else "4:4:4"
                print(f"  size: {len(out_bytes)} bytes, codec: DCT lossy v6, "
                      f"quality: {stored.get('quality')}, chroma: {sub_name}")
                print("Done.")
                return 0
            encode_file(args.input, args.output, color_space=cs,
                        pred_type=pred, tile_size=args.tile, method=method,
                        transform=transform, filter_mode=filter_mode)
            with open(args.output, "rb") as f:
                out_bytes = f.read()
            stored, _ = LumenContainer.unpack_file(out_bytes)
            print(f"  size: {len(out_bytes)} bytes, "
                  f"method: {_METHOD_NAMES.get(stored['method'], stored['method'])}, "
                  f"transform: {_TRANSFORM_NAMES.get(stored.get('transform'), stored.get('transform'))}, "
                  f"filter: {_FILTER_NAMES.get(stored.get('filter_mode'), stored.get('filter_mode'))}, "
                  f"tiles: {stored['num_tiles']}")
            print("Done.")
            return 0

        if args.cmd == "decode":
            print(f"Decoding {args.input} -> {args.output}...")
            decode_file(args.input, args.output)
            print("Done.")
            return 0

        if args.cmd == "inspect":
            _inspect(args.input)
            return 0

        if args.cmd == "verify":
            with open(args.input, "rb") as f:
                data = f.read()
            try:
                LumenContainer.unpack_file(data)
            except LumenError as e:
                print(f"VERIFICATION FAILED: {e}")
                return 1
            print(f"VERIFICATION SUCCESSFUL: {args.input} is valid.")
            return 0

        if args.cmd == "benchmark":
            return _benchmark(args)

        if args.cmd == "med":
            if args.med_cmd == "encode":
                return _med_encode(args)
            if args.med_cmd == "decode":
                return _med_decode(args)
            if args.med_cmd == "info":
                return _med_info(args)
            if args.med_cmd == "verify":
                return _med_verify(args)
            if args.med_cmd == "to-dicom":
                return _med_to_dicom(args)
            if args.med_cmd == "from-dicom":
                return _med_from_dicom(args)
            if args.med_cmd == "dump":
                return _med_dump(args)
            if args.med_cmd == "preview":
                return _med_preview(args)
            print(f"Unknown medical command: {args.med_cmd}", file=sys.stderr)
            return 2

        if args.cmd == "cloud":
            if args.cloud_cmd == "encode":
                return _cloud_encode(args)
            if args.cloud_cmd == "decode":
                return _cloud_decode(args)
            if args.cloud_cmd == "info":
                return _cloud_info(args)
            if args.cloud_cmd == "stats":
                return _cloud_stats(args)
            if args.cloud_cmd == "render":
                return _cloud_render(args)
            print(f"Unknown cloud command: {args.cloud_cmd}", file=sys.stderr)
            return 2

        print(f"Unknown command: {args.cmd}", file=sys.stderr)
        return 2
    except (LumenError, MedicalError, OSError, ValueError) as e:
        print(f"lum: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("lum: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
