# Format comparison renderer test suite.
# Run:  python3 -m pyra.tests.test_format_report
#
# The renderer turns measured rows into the SVGs and the HTML gallery that the
# README embeds. It is standard library only on purpose, so these checks can
# run anywhere -- no Pillow, no ImageMagick, no corpus. What is asserted here
# is the arithmetic (means, KB/MP, PSNR), the ordering, and that every output
# is well formed XML/HTML with untrusted labels escaped.

import os
import sys
import xml.dom.minidom

TESTS = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(TESTS)          # the package / repository root
ROOT = os.path.dirname(PKG)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(PKG, "benchmarks"))

from format_report import (best_by, format_size, psnr, render_bars_svg,
                           render_html, render_scatter_svg, summarise)

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, ok: bool):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL {name}")


def assert_raises(name: str, exc, fn):
    try:
        fn()
    except exc:
        check(name, True)
    except Exception as other:                        # noqa: BLE001
        check(f"{name} (raised {type(other).__name__})", False)
    else:
        check(f"{name} (nothing raised)", False)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

IMAGES = [
    {"name": "a.png", "width": 1000, "height": 1000, "bytes": 10},
    {"name": "b.png", "width": 1000, "height": 1000, "bytes": 20},
]


def rows():
    return [
        {"key": "lum-lossless", "label": "LUMEN lossless", "family": "lossless",
         "lossless": True, "image": "a.png", "bytes": 400, "psnr": 99.0,
         "encode_ms": 10.0, "decode_ms": 5.0},
        {"key": "lum-lossless", "label": "LUMEN lossless", "family": "lossless",
         "lossless": True, "image": "b.png", "bytes": 600, "psnr": 99.0,
         "encode_ms": 20.0, "decode_ms": 7.0},
        {"key": "png", "label": "PNG", "family": "lossless", "lossless": True,
         "image": "a.png", "bytes": 800, "psnr": 99.0, "encode_ms": 30.0},
        {"key": "png", "label": "PNG", "family": "lossless", "lossless": True,
         "image": "b.png", "bytes": 1200, "psnr": 99.0, "encode_ms": 50.0},
        {"key": "jpeg", "label": "JPEG", "family": "lossy", "lossless": False,
         "image": "a.png", "bytes": 100, "psnr": 40.0, "encode_ms": 4.0},
        {"key": "jpeg", "label": "JPEG", "family": "lossy", "lossless": False,
         "image": "b.png", "bytes": 200, "psnr": 42.0, "encode_ms": 6.0},
        {"key": "lum-lossy", "label": "LUMEN q90", "family": "lossy",
         "lossless": False, "image": "a.png", "bytes": 150, "psnr": 44.0,
         "encode_ms": 8.0},
        {"key": "lum-lossy", "label": "LUMEN q90", "family": "lossy",
         "lossless": False, "image": "b.png", "bytes": 250, "psnr": 46.0,
         "encode_ms": 12.0},
    ]


def document():
    return summarise(rows(), IMAGES, {"generated": "2026-01-01",
                                      "corpus": "test corpus",
                                      "quality": 90,
                                      "notes": ["a note <with> & symbols"]})


# ---------------------------------------------------------------------------
# PSNR
# ---------------------------------------------------------------------------

def test_psnr():
    check("identical pixels score 99.0 dB",
          psnr(bytes(64), bytes(64)) == 99.0)

    # Every sample off by one: MSE = 1 -> 10*log10(255^2)
    ref = bytes(64)
    off = bytes([1]) * 64
    expect = 10.0 * __import__("math").log10(255.0 * 255.0)
    check("off-by-one everywhere is 48.13 dB", abs(psnr(ref, off) - expect) < 1e-9)

    # A single large error must dominate: PSNR is not an average of ratios.
    loud = bytearray(64)
    loud[0] = 255
    check("one 255 delta scores below the off-by-one",
          psnr(ref, bytes(loud)) < psnr(ref, off))

    assert_raises("length mismatch is rejected", ValueError,
                  lambda: psnr(bytes(4), bytes(5)))
    check("empty buffers are identical", psnr(b"", b"") == 99.0)


# ---------------------------------------------------------------------------
# formatting and aggregation
# ---------------------------------------------------------------------------

def test_format_size():
    check("bytes stay bytes", format_size(0) == "0 B")
    check("512 B", format_size(512) == "512 B")
    check("1023 B stays bytes", format_size(1023) == "1023 B")
    check("1024 B is 1.0 KB", format_size(1024) == "1.0 KB")
    check("1536 B is 1.5 KB", format_size(1536) == "1.5 KB")
    check("1048576 B is 1.00 MB", format_size(1024 * 1024) == "1.00 MB")
    check("fractions never round up a unit",
          format_size(1024 * 1024 - 1).endswith("KB"))


def test_summarise():
    data = document()
    by_key = {f["key"]: f for f in data["formats"]}

    check("mean of the two images", by_key["png"]["mean_bytes"] == 1000.0)
    check("mean megapixels is 1.0", data["mean_megapixels"] == 1.0)
    check("KB/MP equals mean bytes for a 1 MP corpus",
          by_key["png"]["kb_per_mp"] == 1000.0 / 1024.0)
    check("lossless formats carry no PSNR",
          by_key["png"]["mean_psnr"] is None)
    check("lossy PSNR is averaged",
          by_key["jpeg"]["mean_psnr"] == 41.0)
    check("mean encode time is averaged",
          by_key["png"]["mean_encode_ms"] == 40.0)
    check("min/max bytes are tracked",
          (by_key["png"]["min_bytes"], by_key["png"]["max_bytes"]) == (800, 1200))
    check("per-image rows survive aggregation",
          len(by_key["png"]["per_image"]) == 2)
    check("decode time is carried through",
          by_key["lum-lossless"]["per_image"][1]["decode_ms"] == 7.0)
    check("families are grouped, sizes ascending",
          [f["key"] for f in data["formats"]] ==
          ["lum-lossless", "png", "jpeg", "lum-lossy"])
    check("metadata is copied", data["corpus"] == "test corpus"
          and data["quality"] == 90 and data["generated"] == "2026-01-01")

    check("best lossless is the smallest", best_by(data, "lossless")["key"] ==
          "lum-lossless")
    check("best lossy is the smallest", best_by(data, "lossy")["key"] == "jpeg")
    check("missing family yields an empty dict", best_by(data, "nope") == {})


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def parse(svg: str):
    xml.dom.minidom.parseString(svg.encode("utf-8"))


def test_bars_svg():
    svg = render_bars_svg(document())
    parse(svg)
    check("bars SVG is valid XML", True)
    check("bars SVG names both families",
          "Lossless" in svg and "Lossy" in svg)
    check("bars SVG states the corpus", "test corpus" in svg)
    check("lossy bars show achieved dB", "41.0 dB" in svg)
    check("lossless bars say lossless", "lossless" in svg)
    check("LUMEN is highlighted as the subject", "#5b4bdb" in svg)

    hostile = document()
    hostile["formats"][0]["label"] = 'A<b>&"</b>'
    hostile_svg = render_bars_svg(hostile)
    parse(hostile_svg)
    check("a hostile label cannot break the SVG",
          "<b>&" not in hostile_svg and "&lt;b&gt;" in hostile_svg)

    empty = summarise([], [], {})
    parse(render_bars_svg(empty))
    check("an empty measurement still renders valid XML", True)


def test_scatter_svg():
    svg = render_scatter_svg(document())
    parse(svg)
    check("scatter SVG is valid XML", True)
    check("scatter uses KB/MP", "KB per megapixel" in svg)
    check("scatter plots only lossy formats", "PNG" not in svg)
    check("scatter plots both lossy formats",
          "JPEG" in svg and "LUMEN q90" in svg)

    check("a purely lossless corpus still renders",
          parse(render_scatter_svg(summarise(
              [r for r in rows() if r["lossless"]], IMAGES, {}))) is None)


def test_html():
    html = render_html(document(), {"jpeg": "data:image/png;base64,AAAA"})
    check("gallery HTML has a doctype", html.startswith("<!doctype html>"))
    check("gallery HTML embeds the chart inline",
          "<svg" in html and "</svg>" in html)
    check("gallery HTML embeds the preview", "data:image/png;base64,AAAA" in html)
    check("gallery HTML has the table", "<table>" in html
          and "KB/MP" in html)
    check("gallery HTML lists notes escaped", "&lt;with&gt;" in html)
    check("gallery HTML closes", html.rstrip().endswith("</html>"))
    check("missing previews render a placeholder",
          "No previews were captured" in render_html(document()))


def main():
    test_psnr()
    test_format_size()
    test_summarise()
    test_bars_svg()
    test_scatter_svg()
    test_html()
    print(f"\n{PASS} passed, {FAIL} failed")
    if FAILURES:
        print("Failures:")
        for name in FAILURES:
            print("  -", name)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
