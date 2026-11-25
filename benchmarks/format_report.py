#!/usr/bin/env python3
"""Turn the raw format measurements into a visual comparison.

`bench_formats.py` measures; this module only aggregates and draws, so it
stays importable without Pillow or an encoder on the machine.  Everything
here is standard library only (``math``, ``html``, ``json``).

The output is deliberately two pictures rather than one:

* a pair of bar panels (lossless and lossy at the same nominal quality) that
  answers "how many bytes does each format need?";
* a size/PSNR scatter that answers the fairer question "how many bytes does
  each format need for the fidelity it actually achieved?".

Nominal quality is not a common currency -- WebP `-quality 90` and AVIF
`-quality 90` land on very different distortions -- so every lossy bar is
labelled with the PSNR it really reached.
"""

import html
import math


# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------

def psnr(reference: bytes, decoded: bytes) -> float:
    """Peak signal-to-noise ratio in dB, or 99.0 for an identical pair."""
    if len(reference) != len(decoded):
        raise ValueError("reference and decoded differ in length")
    se = 0
    for a, b in zip(reference, decoded):
        d = a - b
        se += d * d
    if se == 0:
        return 99.0
    return 10.0 * math.log10(255.0 * 255.0 / (se / len(reference)))


def format_size(value: float) -> str:
    """Bytes as a short human string (B, KB or MB)."""
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.2f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{int(value)} B"


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def summarise(rows: list[dict], images: list[dict], meta: dict) -> dict:
    """Aggregate per-image/per-format rows into the document the renderer
    consumes.  `rows` entries are
    ``{"key", "image", "bytes", "psnr", "encode_ms"}``.
    """
    pixels = [image["width"] * image["height"] for image in images]
    mean_mp = _mean(pixels) / 1_000_000.0

    formats: dict[str, dict] = {}
    for row in rows:
        entry = formats.setdefault(row["key"], {
            "key": row["key"],
            "label": row["label"],
            "family": row["family"],
            "lossless": row["lossless"],
            "per_image": [],
        })
        entry["per_image"].append({
            "image": row["image"],
            "bytes": row["bytes"],
            "psnr": row["psnr"],
            "encode_ms": row["encode_ms"],
            "decode_ms": row.get("decode_ms", 0.0),
        })

    out = []
    for entry in formats.values():
        files = entry["per_image"]
        mean_bytes = _mean(f["bytes"] for f in files)
        entry["mean_bytes"] = mean_bytes
        entry["kb_per_mp"] = (mean_bytes / 1024.0 / mean_mp
                              if mean_mp else 0.0)
        entry["mean_encode_ms"] = _mean(f["encode_ms"] for f in files)
        entry["mean_psnr"] = (None if entry["lossless"]
                              else _mean(f["psnr"] for f in files))
        entry["min_bytes"] = min(f["bytes"] for f in files)
        entry["max_bytes"] = max(f["bytes"] for f in files)
        out.append(entry)
    out.sort(key=lambda e: (e["family"], e["mean_bytes"]))

    return {
        "generated": meta.get("generated", ""),
        "corpus": meta.get("corpus", "corpus"),
        "images": images,
        "quality": meta.get("quality", 90),
        "mean_megapixels": mean_mp,
        "notes": meta.get("notes", []),
        "formats": out,
    }


def best_by(data: dict, family: str) -> dict:
    """The smallest format of a family (ties: the first in sorted order)."""
    pool = [f for f in data["formats"] if f["family"] == family]
    return min(pool, key=lambda f: f["mean_bytes"]) if pool else {}


# ---------------------------------------------------------------------------
# colour and geometry helpers
# ---------------------------------------------------------------------------

_ACCENT = "#5b4bdb"
_GREY = "#8d99ae"
_GRID = "#e3e6ee"
_TEXT = "#23252b"
_MUTED = "#666b78"

# Format keys that name the format under test are drawn in the accent colour.
SUBJECT_PREFIX = "lum"


def _bar_panel(formats, title, subtitle, x, width, top):
    """One horizontal bar panel; returns (svg fragment, height)."""
    label_w = 150
    value_pad = 15
    bar_max = width - label_w - 120
    rows = []
    peak = max((f["mean_bytes"] for f in formats), default=0) or 1
    y = top + 62
    for f in formats:
        bar = max(3, f["mean_bytes"] / peak * bar_max)
        color = _ACCENT if f["key"].startswith(SUBJECT_PREFIX) else _GREY
        fidelity = ("lossless" if f["lossless"]
                    else f"{f['mean_psnr']:.1f} dB")
        value = f"{format_size(f['mean_bytes'])} \u00b7 {fidelity}"
        rows.append(
            f'<text x="{x}" y="{y + 11:.1f}" font-size="12" fill="{_TEXT}" '
            f'text-anchor="end">{html.escape(f["label"])}</text>'
            f'<rect x="{x + 8}" y="{y}" width="{bar:.1f}" height="16" '
            f'rx="3" fill="{color}" opacity="{0.95 if color == _ACCENT else 0.7}"/>'
            f'<text x="{x + 14 + bar:.1f}" y="{y + 11:.1f}" font-size="11" '
            f'fill="{_MUTED}">{html.escape(value)}</text>'
        )
        y += 25
    head = (
        f'<text x="{x}" y="{top + 14}" font-size="15" font-weight="600" '
        f'fill="{_TEXT}">{html.escape(title)}</text>'
        f'<text x="{x}" y="{top + 34}" font-size="11.5" fill="{_MUTED}">'
        f'{html.escape(subtitle)}</text>'
    )
    return head + "".join(rows), y - top


def render_bars_svg(data: dict) -> str:
    """A two panel bar chart: lossless, then lossy at nominal quality."""
    groups = {}
    for f in data["formats"]:
        groups.setdefault(f["family"], []).append(f)
    order = ["lossless", "lossy"]
    groups = {name: sorted(groups.get(name, []),
                           key=lambda f: f["mean_bytes"]) for name in order}

    panel_w = 540
    width = panel_w * 2 + 60
    tallest = max(len(groups[n]) for n in order)
    height = 126 + max(1, tallest) * 25 + 26
    title = (f"Compression size by format \u2014 {data['corpus']}")
    sub = (f"{len(data['images'])} images, mean "
           f"{data['mean_megapixels']:.2f} MP. Lossy formats at nominal "
           f"quality {data['quality']}. Smaller is better.")

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{html.escape(title)}">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="24" y="34" font-size="19" font-weight="700" '
        f'fill="{_TEXT}">{html.escape(title)}</text>',
        f'<text x="24" y="56" font-size="12" fill="{_MUTED}">'
        f'{html.escape(sub)}</text>',
    ]
    left, _ = _bar_panel(
        groups["lossless"], "Lossless: bytes are the whole story",
        "decoded pixels are identical to the source",
        28, panel_w - 20, 78)
    right, _ = _bar_panel(
        groups["lossy"], f"Lossy: nominal quality {data['quality']}",
        "PSNR is shown because the quality knobs are not comparable",
        28 + panel_w, panel_w - 20, 78)
    parts.extend([left, right])
    parts.append(
        f'<text x="24" y="{height - 10}" font-size="11" fill="{_MUTED}">'
        f'Measured {html.escape(data["generated"])} \u00b7 '
        f'LUMEN bars are highlighted \u00b7 lower is better</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def render_scatter_svg(data: dict) -> str:
    """Size against achieved fidelity: the only fair lossy comparison."""
    lossy = [f for f in data["formats"] if not f["lossless"]
             and f["mean_psnr"] is not None]
    width, height = 660, 430
    ml, mr, mt, mb = 74, 24, 62, 56
    pw, ph = width - ml - mr, height - mt - mb
    xs = [f["mean_psnr"] for f in lossy] or [30, 40]
    ys = [f["kb_per_mp"] for f in lossy] or [0, 1]
    x0, x1 = math.floor(min(xs) - 1), math.ceil(max(xs) + 1)
    y0, y1 = 0, math.ceil(max(ys) * 1.12) or 1

    def sx(v):
        return ml + (v - x0) / (x1 - x0) * pw

    def sy(v):
        return mt + ph - (v - y0) / (y1 - y0) * ph

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="compression size against achieved PSNR">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{ml}" y="30" font-size="17" font-weight="700" '
        f'fill="{_TEXT}">Bytes per megapixel vs achieved PSNR</text>',
        f'<text x="{ml}" y="49" font-size="11.5" fill="{_MUTED}">'
        f'Down and to the right is better: fewer bytes, more fidelity.</text>',
    ]
    # grid
    for i in range(5):
        v = y0 + (y1 - y0) * i / 4
        y = sy(v)
        parts.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{ml + pw}" y2="{y:.1f}" '
            f'stroke="{_GRID}" stroke-width="1"/>'
            f'<text x="{ml - 8}" y="{y + 4:.1f}" font-size="10.5" '
            f'fill="{_MUTED}" text-anchor="end">{v:.0f}</text>')
    for i in range(5):
        v = x0 + (x1 - x0) * i / 4
        x = sx(v)
        parts.append(
            f'<text x="{x:.1f}" y="{mt + ph + 18}" font-size="10.5" '
            f'fill="{_MUTED}" text-anchor="middle">{v:.0f}</text>')
    parts.append(
        f'<text x="{ml + pw / 2}" y="{height - 12}" font-size="11" '
        f'fill="{_MUTED}" text-anchor="middle">RGB PSNR (dB)</text>'
        f'<text x="18" y="{mt + ph / 2}" font-size="11" fill="{_MUTED}" '
        f'transform="rotate(-90 18 {mt + ph / 2})" text-anchor="middle">'
        f'KB per megapixel</text>')

    for f in sorted(lossy, key=lambda e: -e["kb_per_mp"]):
        cx, cy = sx(f["mean_psnr"]), sy(f["kb_per_mp"])
        accent = f["key"].startswith(SUBJECT_PREFIX)
        color = _ACCENT if accent else _GREY
        r = 6 if accent else 4.5
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{color}" '
            f'opacity="{0.95 if accent else 0.75}"/>'
            f'<text x="{cx + 9:.1f}" y="{cy - 7:.1f}" font-size="11" '
            f'fill="{_TEXT}" font-weight="{600 if accent else 400}">'
            f'{html.escape(f["label"])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def render_html(data: dict, previews: dict | None = None) -> str:
    """A self contained gallery: the chart, the crops, the table."""
    previews = previews or {}
    bars = render_bars_svg(data)
    scatter = render_scatter_svg(data)

    cards = []
    for f in data["formats"]:
        uri = previews.get(f["key"])
        fidelity = ("lossless" if f["lossless"]
                    else f"{f['mean_psnr']:.1f} dB PSNR")
        image = (f'<img alt="decoded crop: {html.escape(f["label"])}" '
                 f'src="{uri}">') if uri else \
            '<div class="preview-missing">No previews were captured</div>'
        cards.append(
            f'<figure class="card">{image}'
            f'<figcaption><strong>{html.escape(f["label"])}</strong><br>'
            f'{format_size(f["mean_bytes"])} \u00b7 {fidelity}</figcaption>'
            f'</figure>')
    gallery = "".join(cards) or "<p>No previews were captured.</p>"

    rows = []
    for f in data["formats"]:
        psnr_text = ("\u2014 (lossless)" if f["lossless"]
                     else f"{f['mean_psnr']:.2f} dB")
        rows.append(
            f'<tr><td>{html.escape(f["label"])}</td>'
            f'<td>{html.escape(f["family"])}</td>'
            f'<td>{format_size(f["mean_bytes"])}</td>'
            f'<td>{f["kb_per_mp"]:.1f}</td>'
            f'<td>{psnr_text}</td>'
            f'<td>{f["mean_encode_ms"]:.0f} ms</td></tr>')
    table = ("<table><thead><tr><th>format</th><th>family</th>"
             "<th>mean size</th><th>KB/MP</th><th>PSNR</th>"
             "<th>encode</th></tr></thead><tbody>"
             + "".join(rows) + "</tbody></table>")

    notes = "".join(f"<li>{html.escape(n)}</li>" for n in data.get("notes", []))
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>LUMEN format comparison</title>
<style>
 body {{ margin: 0; font: 15px/1.55 -apple-system, "Segoe UI", Roboto, sans-serif;
        color: {_TEXT}; background: #fbfbfd; }}
 main {{ max-width: 1180px; margin: 0 auto; padding: 28px 20px 60px; }}
 h1 {{ font-size: 26px; margin: 0 0 4px; }}
 .sub {{ color: {_MUTED}; margin: 0 0 26px; }}
 svg {{ max-width: 100%; height: auto; display: block; margin: 18px 0; }}
 .gallery {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
             gap: 14px; margin: 22px 0 30px; }}
 .card {{ margin: 0; background: #fff; border: 1px solid {_GRID}; border-radius: 8px;
          overflow: hidden; }}
 .card img {{ width: 100%; display: block; image-rendering: pixelated; }}
 .card figcaption {{ padding: 8px 10px; font-size: 12.5px; }}
 table {{ border-collapse: collapse; width: 100%; background: #fff;
          font-size: 13px; }}
 th, td {{ border-bottom: 1px solid {_GRID}; padding: 7px 9px; text-align: right; }}
 th:first-child, td:first-child {{ text-align: left; }}
 th {{ background: #f3f4f9; font-weight: 600; }}
 ul {{ font-size: 13px; color: {_MUTED}; }}
</style>
<main>
 <h1>LUMEN vs the image formats that ship everywhere</h1>
 <p class="sub">{html.escape(data['corpus'])} &middot; {len(data['images'])}
 images &middot; mean {data['mean_megapixels']:.2f} MP &middot; measured
 {html.escape(data['generated'])}</p>
 {bars}
 {scatter}
 <h2>What the bytes actually look like</h2>
 <p class="sub">The same centre crop, decoded from each format. Zoom in:
 the artifacts are the point.</p>
 <div class="gallery">{gallery}</div>
 <h2>All measurements</h2>
 {table}
 <h2>Method notes</h2>
 <ul>{notes}</ul>
</main>
</html>
"""
