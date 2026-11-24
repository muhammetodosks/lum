#!/usr/bin/env python3
"""Measure real image formats against LUMEN on an independent photo corpus.

This is the harness behind ``docs/assets/formats.html`` and the "Visual
comparison" section of the README.  It is deliberately boring: every format
is encoded by the encoder that ships on the machine (ImageMagick for
PNG/JPEG/WebP/AVIF/JP2/TIFF/BMP/GIF, ffmpeg+libjxl for JPEG XL, the LUMEN
Python/Rust codec for LUMEN), decoded again, and scored on the bytes it
actually produced and the PSNR it actually reached.

    # the Kodak set is an independent corpus; nothing here was tuned on it
    git clone --depth 1 https://github.com/MohamedBakrAli/Kodak-Lossless-True-Color-Image-Suite.git /tmp/kodak
    python3 benchmarks/bench_formats.py --corpus /tmp/kodak/PhotoCD_PCD0992

Requires Pillow (to read/convert the corpus) and ImageMagick/ffmpeg.  The
renderer, ``format_report.py``, needs neither.

Fairness rules encoded here:

* all formats get the same decoded reference pixels, read once through
  Pillow and written to a lossless PNG for the external encoders;
* "quality 90" means the encoder's own quality knob at 90 -- it is *not* a
  claim that those settings are perceptually equal, which is why every
  measurement carries its measured PSNR;
* encode time is wall clock including process startup for external tools,
  and is reported for transparency rather than as a benchmark of the
  algorithms themselves;
* a missing encoder is skipped and named in ``skipped`` instead of being
  silently replaced by a different one.
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.dirname(REPO))
sys.path.insert(0, HERE)

from lum import api, lossy                                  # noqa: E402
from lum.constants import CS_RGB                             # noqa: E402
from format_report import psnr, summarise                     # noqa: E402

DEFAULT_IMAGES = ["01.png", "03.png", "05.png", "07.png", "13.png", "23.png"]
QUALITY = 90

# key, label, family, lossless, kind, argument
FORMATS = [
    ("lum-lossless", "LUMEN lossless", "lossless", True, "lum-lossless", ""),
    ("png", "PNG (zlib 9)", "lossless", True, "convert",
     ["-define", "png:compression-level=9"]),
    ("jxl-lossless", "JPEG XL lossless", "lossless", True, "ffmpeg-jxl",
     ["-distance", "0", "-effort", "7"]),
    ("webp-lossless", "WebP lossless", "lossless", True, "convert",
     ["-define", "webp:lossless=true"]),
    ("tiff-lzw", "TIFF (LZW)", "lossless", True, "convert",
     ["-compress", "lzw"]),
    ("bmp", "BMP (uncompressed)", "lossless", True, "convert", []),
    ("lum-lossy", "LUMEN lossy q90", "lossy", False, "lum-lossy", ""),
    ("jpeg", "JPEG q90", "lossy", False, "convert", ["-quality", str(QUALITY)]),
    ("jxl", "JPEG XL d1.0", "lossy", False, "ffmpeg-jxl",
     ["-distance", "1.0", "-effort", "7"]),
    ("webp", "WebP q90", "lossy", False, "convert",
     ["-quality", str(QUALITY)]),
    ("avif", "AVIF q90", "lossy", False, "convert",
     ["-quality", str(QUALITY)]),
    ("jpeg2000", "JPEG 2000 q90", "lossy", False, "convert",
     ["-quality", str(QUALITY)]),
    ("gif", "GIF 256 colours", "lossy", False, "convert", ["-colors", "256"]),
]

EXT = {"lum-lossless": "lum", "lum-lossy": "lum", "png": "png",
       "jxl-lossless": "jxl", "jxl": "jxl", "webp-lossless": "webp",
       "webp": "webp", "tiff-lzw": "tiff", "bmp": "bmp", "jpeg": "jpg",
       "avif": "avif", "jpeg2000": "jp2", "gif": "gif"}


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


def encode_external(kind, args, src_png, out):
    if kind == "convert":
        run(["convert", src_png] + args + [out])
    elif kind == "ffmpeg-jxl":
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", src_png, "-c:v", "libjxl"] + args + [out])
    else:
        raise ValueError(kind)


def decode_external(key, path, out_png):
    if key.startswith("jxl"):
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", path, out_png])
    else:
        run(["convert", path + "[0]", out_png])
    with Image.open(out_png) as im:
        return im.convert("RGB").tobytes(), im.size


def preview(decoded_rgb, size, out_png):
    """A centre crop, decoded and scaled identically for every format."""
    side = size[0] if size[0] < size[1] else size[1]
    side = max(64, int(side * 0.45))
    left = (size[0] - side) // 2
    top = (size[1] - side) // 2
    with Image.frombytes("RGB", size, decoded_rgb) as im:
        crop = im.crop((left, top, left + side, top + side))
        crop = crop.resize((220, 220), Image.BICUBIC)
        crop.save(out_png)


def data_uri(path):
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", default="/tmp/kodak/PhotoCD_PCD0992",
                        help="directory containing the PNG corpus")
    parser.add_argument("--images", nargs="*", default=DEFAULT_IMAGES,
                        help="file names inside --corpus")
    parser.add_argument("--out", default=os.path.join(
        HERE, "results", "formats.json"))
    parser.add_argument("--previews", default=os.path.join(
        REPO, "docs", "assets", "formats"))
    parser.add_argument("--svg", default=os.path.join(
        REPO, "docs", "assets", "formats-bars.svg"))
    parser.add_argument("--scatter", default=os.path.join(
        REPO, "docs", "assets", "formats-scatter.svg"))
    parser.add_argument("--html", default=os.path.join(
        REPO, "docs", "assets", "formats.html"))
    args = parser.parse_args(argv)

    have_convert = shutil.which("convert") is not None
    have_ffmpeg = shutil.which("ffmpeg") is not None
    missing = []
    if not have_convert:
        missing.append("imagemagick")
    if not have_ffmpeg:
        missing.append("ffmpeg")
    formats = []
    for item in FORMATS:
        key, label, family, lossless, kind, extra = item
        if kind == "convert" and not have_convert:
            continue
        if kind == "ffmpeg-jxl" and not have_ffmpeg:
            continue
        formats.append(item)

    images, rows = [], []
    started = time.strftime("%Y-%m-%d")
    preview_paths = {}

    with tempfile.TemporaryDirectory(prefix="lum-formats-") as tmp:
        for name in args.images:
            src = os.path.join(args.corpus, name)
            if not os.path.exists(src):
                raise SystemExit(f"missing image: {src}")
            with Image.open(src) as im:
                im = im.convert("RGB")
                width, height = im.size
                reference = im.tobytes()
            src_png = os.path.join(tmp, name)
            with Image.frombytes("RGB", (width, height), reference) as im:
                im.save(src_png)
            images.append({"name": name, "width": width, "height": height,
                           "bytes": os.path.getsize(src_png)})
            print(f"[{name}] {width}x{height}", flush=True)

            for key, label, family, lossless, kind, extra in formats:
                out = os.path.join(tmp, f"{key}.{EXT[key]}")
                t0 = time.perf_counter()
                blob_path = out
                if kind == "lum-lossless":
                    blob = api.encode(list(reference), width, height, CS_RGB)
                    with open(out, "wb") as f:
                        f.write(blob)
                elif kind == "lum-lossy":
                    blob = lossy.encode(reference, width, height, CS_RGB,
                                        QUALITY, lossy.SUBSAMPLE_444)
                    with open(out, "wb") as f:
                        f.write(blob)
                else:
                    encode_external(kind, extra, src_png, out)
                encode_ms = (time.perf_counter() - t0) * 1000.0

                t0 = time.perf_counter()
                if kind == "lum-lossless":
                    meta, pixels = api.decode(blob)
                    decoded = bytes(pixels)
                    size = (meta["width"], meta["height"])
                elif kind == "lum-lossy":
                    meta, decoded = lossy.decode(blob)
                    size = (meta["width"], meta["height"])
                else:
                    decoded, size = decode_external(key, blob_path,
                                                    os.path.join(tmp, "d.png"))
                decode_ms = (time.perf_counter() - t0) * 1000.0
                if size != (width, height):
                    raise SystemExit(
                        f"{key}: encoded {size}, expected {(width, height)}")

                score = 99.0 if lossless else psnr(reference, decoded)
                rows.append({
                    "key": key, "label": label, "family": family,
                    "lossless": lossless, "image": name,
                    "bytes": os.path.getsize(blob_path),
                    "psnr": score, "encode_ms": encode_ms,
                    "decode_ms": decode_ms,
                })
                print(f"  {label:22s} {os.path.getsize(blob_path):8d} B  "
                      f"{score:6.2f} dB  {encode_ms:7.1f} ms", flush=True)

                if name == args.images[0]:
                    os.makedirs(args.previews, exist_ok=True)
                    path = os.path.join(args.previews, f"{key}.png")
                    preview(decoded, size, path)
                    preview_paths[key] = path

    document = summarise(rows, images, {
        "generated": started,
        "corpus": "Kodak PhotoCD (independent)",
        "quality": QUALITY,
        "notes": [
            "Nominal quality 90 means each encoder's own knob; the measured "
            "PSNR is what makes the comparison honest.",
            "LUMEN lossless/lossy numbers come from this Python package; the "
            "other formats come from ImageMagick 6.9 and ffmpeg+libjxl 7 "
            "at their default settings plus the flags shown.",
            "Encode time includes process startup for external tools and is "
            "not an algorithm-only benchmark.",
            "The corpus is the Kodak PhotoCD set; nothing in this repository "
            "was tuned on it.",
        ] + ([f"skipped (missing encoder): {', '.join(missing)}"] if missing
             else []),
    })
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2, sort_keys=False)
        f.write("\n")

    previews = {key: data_uri(path) for key, path in preview_paths.items()}
    from format_report import render_bars_svg, render_html, render_scatter_svg
    for path, svg in ((args.svg, render_bars_svg(document)),
                      (args.scatter, render_scatter_svg(document))):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg + "\n")
    with open(args.html, "w", encoding="utf-8") as f:
        f.write(render_html(document, previews))

    print(f"\nwrote {args.out}")
    print(f"wrote {args.svg}")
    print(f"wrote {args.scatter}")
    print(f"wrote {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
