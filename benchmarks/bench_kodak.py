#!/usr/bin/env python3
"""Matched-PSNR comparison of PYRA against JPEG XL on an independent corpus.

The Kodak PhotoCD set (24 images, 768x512 / 512x768) is the classic codec
benchmark and is used here as an *independent* corpus: nothing in this
repository was tuned on it.

    # fetch the corpus (about 20 MB)
    git clone --depth 1 https://github.com/MohamedBakrAli/Kodak-Lossless-True-Color-Image-Suite.git /tmp/kodak
    python3 benchmarks/bench_kodak.py --corpus /tmp/kodak/PhotoCD_PCD0992

Requires Pillow for reading the corpus and `ffmpeg` built with `libjxl`.

Both codecs are sampled on their own curve and the size at each target PSNR is
interpolated between the two nearest measured points. A target that a curve
never reaches is reported as unbracketed (n/a) instead of being snapped to the
closest point, because snapping quietly invents wins: an earlier version of
this harness clamped and produced an 11 % gain that did not exist.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from pyra import lossy                                    # noqa: E402
from pyra.constants import CS_RGB                         # noqa: E402

# Distances hand-picked to cover the useful range of libjxl's quality knob;
# -1.0 is near-lossless and 15.0 is the bottom of the curve.
JXL_DISTANCES = [-1.0, -0.5, 0.0, 0.4, 0.8, 1.2, 1.6, 2.0, 2.5, 3.0,
                 3.8, 4.8, 6.0, 7.5, 9.5, 12.0, 15.0]
PYRA_QUALITIES = list(range(10, 101, 5))


def psnr(a, b):
    if len(a) != len(b):
        return -1.0
    se = 0
    for x, y in zip(a, b):
        d = x - y
        se += d * d
    mse = se / len(a)
    return 99.0 if mse == 0 else 10.0 * math.log10(255.0 * 255.0 / mse)


def read_ppm(path):
    with open(path, "rb") as f:
        data = f.read()
    parts = data.split(b"\n", 3)
    if parts[0].strip() != b"P6":
        raise ValueError(f"{path}: not a binary PPM")
    return parts[3]


def size_at(points, target):
    """Bytes at `target` dB, or None when the curve does not bracket it."""
    pts = sorted(points)
    for (p0, s0), (p1, s1) in zip(pts, pts[1:]):
        if p0 <= target <= p1:
            return s0 if p1 == p0 else s0 + (target - p0) / (p1 - p0) * (s1 - s0)
    return None


def pyra_curve(raw, w, h):
    out = []
    for q in PYRA_QUALITIES:
        blob = lossy.encode(raw, w, h, CS_RGB, q, lossy.SUBSAMPLE_444)
        out.append((psnr(raw, lossy.decode(blob)[1]), len(blob)))
    return out


def jxl_curve(ppm, tag, work, source_raw):
    out = []
    for d in JXL_DISTANCES:
        enc = os.path.join(work, f"{tag}_d{d}.jxl")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", ppm,
                        "-c:v", "libjxl", "-distance", str(d), "-effort", "7",
                        enc], check=True)
        dec = os.path.join(work, f"{tag}_dec.ppm")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", enc, dec],
                       check=True)
        out.append((psnr(source_raw, read_ppm(dec)), os.path.getsize(enc)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="directory of PNG files")
    ap.add_argument("--targets", default="34,38,42",
                    help="comma separated PSNR targets (default 34,38,42)")
    ap.add_argument("--limit", type=int, default=0, help="only the first N images")
    ap.add_argument("--out", default="", help="write the raw JSON here")
    args = ap.parse_args()

    targets = [float(t) for t in args.targets.split(",")]
    names = sorted(f for f in os.listdir(args.corpus) if f.endswith(".png"))
    if args.limit:
        names = names[:args.limit]
    if not names:
        print(f"no PNG files in {args.corpus}", file=sys.stderr)
        return 1

    work = tempfile.mkdtemp(prefix="pyra_kodak_")
    print(f"corpus: {len(names)} images from {args.corpus}", flush=True)

    rows = []
    started = time.time()
    for i, name in enumerate(names, 1):
        im = Image.open(os.path.join(args.corpus, name)).convert("RGB")
        w, h = im.size
        raw = im.tobytes()
        ppm = os.path.join(work, name + ".ppm")
        im.save(ppm)

        t0 = time.time()
        pyra = pyra_curve(raw, w, h)
        t_pyra = time.time() - t0
        t0 = time.time()
        jxl = jxl_curve(ppm, os.path.splitext(name)[0], work, raw)
        t_jxl = time.time() - t0

        row = {"image": name, "w": w, "h": h, "pyra": pyra, "jxl": jxl,
               "pyra_sec": t_pyra, "jxl_sec": t_jxl}
        cells = ""
        for t in targets:
            ps, js = size_at(pyra, t), size_at(jxl, t)
            row[f"pyra_at_{t:g}"] = ps
            row[f"jxl_at_{t:g}"] = js
            cells += ("   n/a  " if ps is None or js is None
                      else f" {100.0 * ps / js:6.1f}%")
        rows.append(row)
        print(f"[{i:2d}/{len(names)}] {name:9s} {w}x{h}" + cells
              + f"   ({t_pyra:.1f}s pyra, {t_jxl:.1f}s jxl)", flush=True)

    summary = []
    for t in targets:
        covered = [r for r in rows
                   if r[f"pyra_at_{t:g}"] is not None
                   and r[f"jxl_at_{t:g}"] is not None]
        if not covered:
            summary.append({"target": t, "n": 0, "missing": len(rows)})
            continue
        kbmp = lambda r, key: 1000.0 * r[key] / (r["w"] * r["h"])  # noqa: E731
        summary.append({
            "target": t,
            "n": len(covered),
            "missing": len(rows) - len(covered),
            "pyra_kbmp": sum(kbmp(r, f"pyra_at_{t:g}") for r in covered) / len(covered),
            "jxl_kbmp": sum(kbmp(r, f"jxl_at_{t:g}") for r in covered) / len(covered),
            "mean_ratio": sum(100.0 * r[f"pyra_at_{t:g}"] / r[f"jxl_at_{t:g}"]
                              for r in covered) / len(covered),
            "wins": sum(1 for r in covered
                        if r[f"pyra_at_{t:g}"] < r[f"jxl_at_{t:g}"]),
            "losses": sum(1 for r in covered
                          if r[f"pyra_at_{t:g}"] >= r[f"jxl_at_{t:g}"]),
        })

    print("\ntarget |    n | PYRA KB/MP | JXL KB/MP | PYRA/JXL | wins/losses")
    for s in summary:
        if not s["n"]:
            print(f"{s['target']:6.0f} |    0 | unbracketed by every curve")
            continue
        print(f"{s['target']:6.0f} | {s['n']:4d} | {s['pyra_kbmp']:10.2f} |"
              f" {s['jxl_kbmp']:9.2f} | {s['mean_ratio']:7.1f}% |"
              f" {s['wins']}/{s['losses']}")

    result = {"corpus": os.path.basename(args.corpus),
              "jxl": "ffmpeg libjxl, -effort 7, distance swept",
              "targets": targets, "rows": rows, "summary": summary,
              "elapsed_sec": time.time() - started}
    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=1)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
