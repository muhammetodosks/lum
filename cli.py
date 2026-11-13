import argparse
import json
import sys
import time
import tempfile

from .api import encode_file, decode_file, _parse_pnm
from .container import PyraContainer
from .constants import (CS_GRAY, CS_RGB, CS_RGBA,
                        PRED_NONE, PRED_LEFT, PRED_UP, PRED_AVERAGE,
                        PRED_PAETH, PRED_GRADIENT, PRED_MEDIAN,
                        METHOD_RAW, METHOD_RLE, METHOD_LZ, METHOD_MIX,
                        TRANSFORM_YCOCG, TRANSFORM_NONE,
                        FILTER_GLOBAL, FILTER_ADAPTIVE,
                        PyraError, CorruptedFileError)
from .pngbench import encode_png
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

# ------------------------------------------------------------ medical (PYRM)
_MED_FORMAT_NAMES = {0: "u8", 1: "u16", 2: "u32", 3: "i16", 4: "i32", 5: "f32"}
_MED_PRED_NAMES = {0: "none", 1: "left", 2: "up", 3: "average", 4: "paeth",
                   5: "gradient", 6: "median", 7: "slice"}
_MED_METHOD_NAMES = {MED_METHOD_RAW: "raw", MED_METHOD_LZ: "lz"}
_MED_FORMAT_BY_NAME = {v: k for k, v in _MED_FORMAT_NAMES.items()}
_MED_PRED_BY_NAME = {v: k for k, v in _MED_PRED_NAMES.items()}
_MED_METHOD_BY_NAME = {v: k for k, v in _MED_METHOD_NAMES.items()}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pyra", description="PYRA image format tool")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("encode", help="encode a PNM image into a .pyra file")
    e.add_argument("input")
    e.add_argument("output")
    e.add_argument("--method", choices=["auto", "raw", "rle", "lz", "mix"], default="auto")
    e.add_argument("--predictor", choices=["auto"] + list(_PRED_BY_NAME), default="auto")
    e.add_argument("--transform", choices=["auto"] + list(_TRANSFORM_BY_NAME), default="auto")
    e.add_argument("--filter", choices=["auto"] + list(_FILTER_BY_NAME), default="auto")
    e.add_argument("--tile", type=int, default=256)
    e.add_argument("--colorspace", choices=["auto", "gray", "rgb", "rgba"], default="auto")

    d = sub.add_parser("decode", help="decode a .pyra file into a PNM image")
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

    med = sub.add_parser("med", help="PYRM medical container tools")
    med_sub = med.add_subparsers(dest="med_cmd", required=True)

    me = med_sub.add_parser("encode", help="encode raw samples into a .pyrm file")
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

    md = med_sub.add_parser("decode", help="decode a .pyrm file into raw samples")
    md.add_argument("input")
    md.add_argument("output")

    mi = med_sub.add_parser("info", help="print container metadata")
    mi.add_argument("input")

    mdc = med_sub.add_parser("to-dicom",
                             help="convert a .pyrm container into a DICOM file")
    mdc.add_argument("input")
    mdc.add_argument("output")

    mdf = med_sub.add_parser("from-dicom",
                             help="convert a DICOM file into a .pyrm container")
    mdf.add_argument("input")
    mdf.add_argument("output")
    mdf.add_argument("--tile", type=int, default=256)

    mvf = med_sub.add_parser("verify", help="check every integrity guarantee")
    mvf.add_argument("input")

    mdu = med_sub.add_parser("dump", help="print the metadata block as JSON")
    mdu.add_argument("input")

    mv = med_sub.add_parser("preview", help="render one slice as an 8-bit PGM")
    mv.add_argument("input")
    mv.add_argument("output")
    mv.add_argument("--slice", type=int, default=0)
    mv.add_argument("--window", type=float, default=None,
                    help="display window (default: the WindowWidth of the metadata)")
    mv.add_argument("--level", type=float, default=None,
                    help="display level (default: the WindowCenter of the metadata)")
    return p


def _inspect(input_path: str) -> None:
    with open(input_path, "rb") as f:
        data = f.read()
    meta, tiles = PyraContainer.unpack_file(data)
    print(f"=== PYRA Image Inspection: {input_path} ===")
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

    with tempfile.NamedTemporaryFile(suffix=".pyra") as tmp_pyra, \
         tempfile.NamedTemporaryFile(suffix=".ppm" if magic == b"P6" else ".pgm") as tmp_out:
        t0 = time.perf_counter()
        encode_file(args.input, tmp_pyra.name, color_space=color_space,
                    pred_type=pred, tile_size=args.tile, method=method,
                    transform=transform, filter_mode=filter_mode)
        t_enc = time.perf_counter() - t0

        with open(tmp_pyra.name, "rb") as f:
            pyra_bytes = f.read()
        meta, _tiles = PyraContainer.unpack_file(pyra_bytes)

        t0 = time.perf_counter()
        decode_file(tmp_pyra.name, tmp_out.name)
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
        print(f"  PYRA size:  {len(pyra_bytes):>9} bytes  "
              f"(method {_METHOD_NAMES.get(meta['method'], meta['method'])}, "
              f"ratio {raw_cmp / max(1, len(pyra_bytes)):.2f}x)")
        print(f"  PNG size:   {len(png_bytes):>9} bytes  (ratio {raw_cmp / max(1, len(png_bytes)):.2f}x, "
              f"png encode {t_png:.3f} s, C zlib deflate)")
        if full_range:
            delta = len(pyra_bytes) - len(png_bytes)
            pct = 100.0 * delta / max(1, len(png_bytes))
            print(f"  PYRA vs PNG: {delta:+d} bytes ({pct:+.1f}%)")
        print(f"  roundtrip: {roundtrip}")
    return 0


# ------------------------------------------------------------- medical (PYRM)

def _med_meta(pairs: list[str]) -> dict:
    """Parse `KEY=VALUE` metadata arguments."""
    meta = {}
    for item in pairs:
        if "=" not in item:
            raise ValueError(f"metadata entry must be KEY=VALUE: {item!r}")
        key, value = item.split("=", 1)
        meta[key.strip()] = value.strip()
    return meta


def _med_encode(args) -> int:
    with open(args.input, "rb") as f:
        samples = f.read()
    fmt = _MED_FORMAT_BY_NAME[args.format]
    predictor = _MED_PRED_BY_NAME.get(args.predictor, -1)
    method = _MED_METHOD_BY_NAME.get(args.method, -1)

    metadata = _med_meta(args.meta)
    if args.dicom:
        # The geometry dependent attributes are derived, so the result is
        # always self consistent; the explicit --meta entries win.
        clinical = _med_dicom.build_dicom_metadata(
            args.width, args.height, args.depth, args.channels, fmt,
            patient_id=args.patient_id or "",
            patient_name=args.patient_name or "",
            modality=args.modality or "",
            study_uid=args.study_uid or "",
            series_uid=args.series_uid or "",
            sop_uid=args.sop_uid or "",
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
                print(f"pyra: error: metadata: {problem}", file=sys.stderr)
            return 1

    blob = _med_codec.encode(samples, args.width, args.height, args.depth,
                              args.channels, fmt, metadata=metadata,
                              tile_size=args.tile, predictor=predictor,
                              method=method,
                              require_dicom=args.require_dicom)
    with open(args.output, "wb") as f:
        f.write(blob)
    info = _med_codec.info(blob)
    print(f"Encoded {args.input} -> {args.output}")
    print(f"  geometry: {info['width']}x{info['height']}x{info['depth']} "
          f"ch={info['channels']} {_MED_FORMAT_NAMES.get(info['sample_format'])}")
    print(f"  size: {len(blob)} bytes, tiles: {info['num_tiles']}")
    if args.dicom or args.require_dicom:
        print(f"  dicom compatible: "
              f"{_med_dicom.is_dicom_compatible(metadata)}")
    return 0


def _med_decode(args) -> int:
    with open(args.input, "rb") as f:
        blob = f.read()
    metadata, samples = _med_codec.decode(blob)
    with open(args.output, "wb") as f:
        f.write(samples)
    info = _med_codec.info(blob)
    print(f"Decoded {args.input} -> {args.output}")
    print(f"  geometry: {info['width']}x{info['height']}x{info['depth']} "
          f"ch={info['channels']} {_MED_FORMAT_NAMES.get(info['sample_format'])}")
    print(f"  samples: {len(samples)} bytes")
    if metadata:
        print(f"  metadata: {metadata}")
    return 0


def _med_info(args) -> int:
    with open(args.input, "rb") as f:
        blob = f.read()
    info = _med_codec.info(blob)
    print(f"=== PYRM Medical Inspection: {args.input} ===")
    for key in ("width", "height", "depth", "channels", "sample_format",
                "bits", "tile_size", "num_tiles"):
        print(f"  {key}: {info[key]}")
    print(f"  format: {_MED_FORMAT_NAMES.get(info['sample_format'])}")
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
    report = _med_codec.verify(blob)
    print(f"VERIFICATION SUCCESSFUL: {args.input} is valid.")
    print(f"  payload: {report['payload_bytes']} bytes in "
          f"{report['num_tiles']} tiles")
    print(f"  sha256 content hash: verified")
    print(f"  dicom_compatible: {'yes' if report['dicom_compatible'] else 'no'}")
    return 0


def _med_dump(args) -> int:
    with open(args.input, "rb") as f:
        blob = f.read()
    metadata = _med_codec.decode(blob)[0]
    print(json.dumps(metadata, indent=2, sort_keys=True, default=str))
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
        print("pyra: error: cannot render this slice", file=sys.stderr)
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
            print(f"Encoding {args.input} -> {args.output}...")
            encode_file(args.input, args.output, color_space=cs,
                        pred_type=pred, tile_size=args.tile, method=method,
                        transform=transform, filter_mode=filter_mode)
            with open(args.output, "rb") as f:
                out_bytes = f.read()
            stored, _ = PyraContainer.unpack_file(out_bytes)
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
                PyraContainer.unpack_file(data)
            except PyraError as e:
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

        print(f"Unknown command: {args.cmd}", file=sys.stderr)
        return 2
    except (PyraError, MedicalError, OSError, ValueError) as e:
        print(f"pyra: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("pyra: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
