import argparse
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

        print(f"Unknown command: {args.cmd}", file=sys.stderr)
        return 2
    except (PyraError, OSError, ValueError) as e:
        print(f"pyra: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("pyra: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
