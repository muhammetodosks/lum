#!/usr/bin/env python3
"""Fetch and verify the AV1 conformance streams LUMEN is measured on.

There is no public `AOMediaCodec/av1-cts` repository: the URL returns 404 from
both github.com and the REST API, and the AOMediaCodec organisation does not
list such a repo. The conformance vectors live in the public Google Cloud
Storage bucket `aom-test-data`, and the reference decoder's own pinned manifest
(`test/test-data.sha1` in the libaom tree) names exactly these files. This
script uses that bucket and checks the download against both digests:

  1. the bucket object SHA-1 against libaom's `test-data.sha1` manifest, and
  2. every decoded frame's MD5 against the sibling `.ivf.md5` file — the actual
     conformance contract, which says the decoder must reproduce those digests
     bit for bit.

Then it writes one frame per stream as a PNG so the rate benchmark can run on
it:

    python3 benchmarks/fetch_av1_cts.py --out /tmp/av1cts
    python3 benchmarks/bench_kodak.py --corpus /tmp/av1cts/png

Requires `ffmpeg`/`ffprobe` built with a libaom or dav1d AV1 decoder, and
Pillow for the benchmark (not for this script).
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BUCKET = "https://storage.googleapis.com/aom-test-data"
LIST = ("https://storage.googleapis.com/storage/v1/b/aom-test-data/o"
        "?prefix={prefix}&maxResults=1000&fields=items(name,size),nextPageToken")
MANIFEST = ("https://aomedia.googlesource.com/aom/+/refs/heads/main/"
            "test/test-data.sha1?format=TEXT")

# libaom's CTS digests are always over a 4:2:0 frame buffer. Monochrome streams
# decode to neutral chroma, so their samples are hashed as yuv420p, not gray.
DECODE_PIX = {"gray": "yuv420p", "gray10le": "yuv420p10le",
              "gray12le": "yuv420p12le"}
BYTES_PER_SAMPLE = {"yuv420p": 1.5, "yuv422p": 2.0, "yuv444p": 3.0,
                    "yuv420p10le": 3.0, "yuv420p12le": 3.0,
                    "yuv420p16le": 3.0, "yuv422p10le": 4.0,
                    "yuv422p12le": 4.0, "yuv444p10le": 6.0,
                    "yuv444p12le": 6.0}
DECODERS = ["libaom-av1", "libdav1d"]


def list_objects(prefix="av1-"):
    out, token = [], None
    while True:
        url = (LIST.format(prefix=prefix)
               + (f"&pageToken={token}" if token else ""))
        with urllib.request.urlopen(url, timeout=90) as r:
            doc = json.load(r)
        out += doc.get("items", [])
        token = doc.get("nextPageToken")
        if not token:
            return out


def fetch(obj, dest):
    name, size = obj["name"], int(obj["size"])
    path = os.path.join(dest, name)
    if os.path.exists(path) and os.path.getsize(path) == size:
        return name, True
    for _ in range(3):
        try:
            with urllib.request.urlopen(f"{BUCKET}/{name}", timeout=180) as r:
                data = r.read()
            if len(data) == size:
                with open(path, "wb") as f:
                    f.write(data)
                return name, True
        except Exception:
            pass
    return name, False


def manifest_digests():
    try:
        with urllib.request.urlopen(MANIFEST, timeout=90) as r:
            text = base64.b64decode(r.read()).decode()
    except Exception:
        return {}
    out = {}
    for line in text.splitlines():
        if " *" in line:
            digest, name = line.strip().split(" *", 1)
            out[name] = digest
    return out


def probe(path):
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,pix_fmt", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    parts = res.stdout.strip().split(",")
    if len(parts) != 3:
        return None
    return int(parts[0]), int(parts[1]), parts[2]


def fetch_natural(outdir, jobs):
    """The natural SDR sequences in the same bucket, as PNG frames.

    Three clips, ten frames each: 608x1080, 720x1280 and 1080x1920, all
    yuv420p. The 88 MB sports clip is deliberately excluded.
    """
    objs = list_objects("SDR_")
    # .y4m carries its own geometry; the 88 MB sports clip is raw .yuv with no
    # header, so it is skipped rather than special-cased.
    natural = sorted((o for o in objs if o["name"].endswith(".y4m")),
                     key=lambda o: o["name"])
    if not natural:
        print("no natural sequences listed", file=sys.stderr)
        return 1
    os.makedirs(outdir, exist_ok=True)
    print(f"natural: {len(natural)} sequences, "
          f"{sum(int(o['size']) for o in natural) / 1e6:.1f} MB")
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        results = list(ex.map(lambda o: fetch(o, outdir), natural))
    failed = [n for n, ok in results if not ok]
    if failed:
        print(f"download failures: {failed}", file=sys.stderr)
        return 1

    png = os.path.join(outdir, "png")
    os.makedirs(png, exist_ok=True)
    total = 0
    for o in natural:
        name = o["name"]
        stem = os.path.splitext(name)[0]
        res = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", os.path.join(outdir, name),
             "-vsync", "0", os.path.join(png, stem + "_f%03d.png")],
            capture_output=True)
        made = len([f for f in os.listdir(png) if f.startswith(stem + "_f")])
        total += made
        if res.returncode != 0 or not made:
            print(f"  {name}: extraction failed: "
                  f"{res.stderr.decode()[:160]}", file=sys.stderr)
            return 1
        print(f"  {name:34s} -> {made} PNGs")
    print(f"natural frames: {total} -> {png}")
    print(f"next: python3 benchmarks/bench_kodak.py --corpus {png}")
    return 0


def verify(path, ref, w, h, pix):
    """Frame MD5s of the decoded stream against the .ivf.md5 reference."""
    frame_size = int(w * h * BYTES_PER_SAMPLE[pix])
    best = (0, 0)
    for dec in DECODERS:
        res = subprocess.run(
            ["ffmpeg", "-v", "error", "-c:v", dec, "-i", path,
             "-f", "rawvideo", "-pix_fmt", pix, "-"], capture_output=True)
        data = res.stdout
        if not data:
            continue
        n = len(data) // frame_size
        ok = sum(1 for i in range(min(n, len(ref)))
                 if hashlib.md5(data[i * frame_size:(i + 1) * frame_size]
                                ).hexdigest() == ref[i])
        best = max(best, (ok, n))
        if ok == len(ref) and n == len(ref):
            break
    return best


def extract_png(path, ref, w, h, pix, out_png, decoder="libaom-av1"):
    index = max(0, (len(ref) - 1) // 2)
    png = os.path.join(
        out_png, os.path.basename(path).replace(".ivf", "") + f"_f{index + 1:04d}.png")
    res = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-c:v", decoder, "-i", path,
         "-vf", f"select=eq(n\\,{index})", "-vsync", "0", "-frames:v", "1", png],
        capture_output=True)
    if os.path.exists(png) and os.path.getsize(png) > 0:
        return png
    if res.returncode != 0:
        print(f"  png failed: {os.path.basename(path)}: "
              f"{res.stderr.decode()[:120]}", file=sys.stderr)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="download directory for the CTS")
    ap.add_argument("--natural", default="",
                    help="also fetch the natural SDR sequences into this directory")
    ap.add_argument("--jobs", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0, help="only the first N streams")
    ap.add_argument("--no-png", action="store_true")
    ap.add_argument("--json", default="", help="write the verification report here")
    args = ap.parse_args()

    if not args.out and not args.natural:
        ap.error("give --out, --natural, or both")
    if args.natural:
        rc = fetch_natural(args.natural, args.jobs)
        if rc or not args.out:
            return rc

    os.makedirs(args.out, exist_ok=True)
    objs = [o for o in list_objects("av1-")
            if o["name"].endswith((".ivf", ".ivf.md5"))]
    objs.sort(key=lambda o: o["name"])
    if args.limit:
        first = sorted(o["name"] for o in objs if o["name"].endswith(".ivf"))
        first = first[:args.limit]
        keep = set(first) | {n + ".md5" for n in first}
        objs = [o for o in objs if o["name"] in keep]
    print(f"bucket: {len(objs)} objects to fetch "
          f"({sum(int(o['size']) for o in objs) / 1e6:.1f} MB)")

    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        results = list(ex.map(lambda o: fetch(o, args.out), objs))
    failed = [n for n, ok in results if not ok]
    if failed:
        print(f"download failures: {len(failed)}: {failed[:5]}", file=sys.stderr)
        return 1

    manifest = manifest_digests()
    streams = sorted(f for f in os.listdir(args.out)
                     if f.endswith(".ivf") and not f.endswith(".ivf.md5"))
    if args.limit:
        streams = streams[:args.limit]

    out_png = os.path.join(args.out, "png")
    if not args.no_png:
        os.makedirs(out_png, exist_ok=True)

    rows, bad = [], []
    for i, name in enumerate(streams, 1):
        path = os.path.join(args.out, name)
        ref = [ln.split()[0] for ln in
               open(path + ".md5", encoding="utf-8") if ln.strip()]
        info = probe(path)
        if info is None:
            bad.append(name)
            continue
        w, h, pix = info
        decode_pix = DECODE_PIX.get(pix, pix)
        if decode_pix not in BYTES_PER_SAMPLE:
            print(f"  unknown pixel format {pix} in {name}", file=sys.stderr)
            bad.append(name)
            continue
        ok, frames = verify(path, ref, w, h, decode_pix)
        sha1_ok = None
        if name in manifest:
            sha1_ok = (hashlib.sha1(open(path, "rb").read()).hexdigest()
                       == manifest[name])
        png = None
        if not args.no_png and ok == len(ref) and frames == len(ref):
            png = extract_png(path, ref, w, h, decode_pix, out_png)
        rows.append({"stream": name, "w": w, "h": h, "pix_fmt": pix,
                     "decode_pix": decode_pix, "frames": len(ref),
                     "md5_ok": ok, "manifest_sha1_ok": sha1_ok, "png": png})
        if ok != len(ref) or frames != len(ref):
            bad.append(name)
        print(f"[{i:3d}/{len(streams)}] {name:48s} {w}x{h} "
              f"{ok}/{len(ref)} frames", flush=True)

    verified = sum(1 for r in rows if r["md5_ok"] == r["frames"])
    frames_ok = sum(r["md5_ok"] for r in rows)
    frames_all = sum(r["frames"] for r in rows)
    in_manifest = [r for r in rows if r["manifest_sha1_ok"] is not None]
    print(f"\nstreams with every frame digest matching: {verified}/{len(rows)}")
    print(f"frames matching:                          {frames_ok}/{frames_all}")
    if in_manifest:
        print(f"streams matching libaom test-data.sha1:   "
              f"{sum(1 for r in in_manifest if r['manifest_sha1_ok'])}/"
              f"{len(in_manifest)} (not pinned: {len(rows) - len(in_manifest)})")
    if bad:
        print(f"FAILED: {bad}")
    else:
        print("FAILED: none")
    if not args.no_png:
        print(f"PNG frames: {sum(1 for r in rows if r['png'])} -> {out_png}")
        print(f"next: python3 benchmarks/bench_kodak.py --corpus {out_png}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rows, f, indent=1)
        print(f"wrote {args.json}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
