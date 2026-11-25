"""Build the fixtures that tests/test_wasm.mjs cross-checks against Python.

Run as `python3 -m lum.tests.wasm_fixture <output-dir>` from the parent of the
package. It writes a deterministic RGB image, encodes it both ways with the
reference codec, and decodes it back to PPM so the JavaScript side can compare
pixel for pixel with the wasm build.
"""

import json
import os
import sys


WIDTH = 64
HEIGHT = 48


def pixels():
    """A smooth gradient plus deterministic detail, so both codecs have work."""
    out = []
    for y in range(HEIGHT):
        for x in range(WIDTH):
            r = (x * 255) // (WIDTH - 1)
            g = (y * 255) // (HEIGHT - 1)
            b = ((x * 3 + y * 5) ^ (x * y)) & 0xFF
            if (x // 8 + y // 8) % 2:
                r = min(255, r + 24)
                b = max(0, b - 24)
            out.extend((r, g, b))
    return out


def write_ppm(path, samples):
    header = f"P6\n{WIDTH} {HEIGHT}\n255\n".encode("ascii")
    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(bytes(samples))


def read_ppm(path):
    with open(path, "rb") as fh:
        data = fh.read()
    parts = data.split(b"\n", 3)
    if parts[0] != b"P6":
        raise SystemExit("unexpected PPM magic")
    body = parts[3]
    if len(body) != WIDTH * HEIGHT * 3:
        raise SystemExit(f"unexpected PPM body: {len(body)} bytes")
    return list(body)


def main(outdir):
    from lum import api

    os.makedirs(outdir, exist_ok=True)
    src = pixels()
    src_path = os.path.join(outdir, "src.ppm")
    write_ppm(src_path, src)

    fixtures = {}
    for name, quality in (("lossless", 0), ("lossy", 90)):
        lum_path = os.path.join(outdir, f"{name}.lum")
        ppm_path = os.path.join(outdir, f"{name}.ppm")
        meta = api.encode_file(src_path, lum_path, quality=quality, subsampling=1,
                               pred_type=-1)
        api.decode_file(lum_path, ppm_path)
        with open(lum_path, "rb") as fh:
            blob = fh.read()
        decoded = read_ppm(ppm_path)
        if quality == 0 and decoded != src:
            raise SystemExit("the lossless reference decode is not identical")
        fixtures[name] = {
            "lum": lum_path,
            "ppm": ppm_path,
            "bytes": len(blob),
            "decoded": decoded,
            "meta": meta,
        }

    meta_path = os.path.join(outdir, "fixtures.json")
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "width": WIDTH,
                "height": HEIGHT,
                "src": src,
                "lossless": {k: v for k, v in fixtures["lossless"].items() if k != "decoded"},
                "lossy": {k: v for k, v in fixtures["lossy"].items() if k != "decoded"},
            },
            fh,
        )
    print(json.dumps({"src_bytes": len(src), "meta": meta_path}))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/lum-wasm")
