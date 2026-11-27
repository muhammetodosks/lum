#!/usr/bin/env python3
"""Measure how the wasm codec scales with image size, in a real browser.

The number this prints decides the site's default downscale: lossless encoding
has no threads under wasm, and a page that freezes for ten seconds is not a
product page.
"""
import json
import sys
import time

from site_check import CDP, start_chrome, ws_url

PROBE = r"""
(async () => {
  const { Lumen } = window.__lumenSite;
  const res = await fetch("docs/assets/formats/png.png");
  const bmp = await createImageBitmap(await res.blob());
  const out = [];
  for (const edge of [220, 440, 660, 880, 1320]) {
    const scale = edge / Math.max(bmp.width, bmp.height);
    const w = Math.round(bmp.width * scale);
    const h = Math.round(bmp.height * scale);
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(bmp, 0, 0, w, h);
    const img = ctx.getImageData(0, 0, w, h);
    const n = w * h;
    const data = new Uint8Array(n * 3);
    for (let i = 0, j = 0, k = 0; i < n; i++, j += 4, k += 3) {
      data[k] = img.data[j]; data[k + 1] = img.data[j + 1]; data[k + 2] = img.data[j + 2];
    }
    const px = { width: w, height: h, channels: 3, data };
    const t0 = performance.now();
    const lossless = Lumen.encode(px, { quality: 0 });
    const losslessMs = performance.now() - t0;
    const t1 = performance.now();
    const lossy = Lumen.encode(px, { quality: 90 });
    const lossyMs = performance.now() - t1;
    const t2 = performance.now();
    Lumen.decode(lossless);
    const decodeMs = performance.now() - t2;
    out.push({
      edge, width: w, height: h, mp: +(n / 1e6).toFixed(3),
      losslessBytes: lossless.length, lossyBytes: lossy.length,
      losslessMs: +losslessMs.toFixed(0), lossyMs: +lossyMs.toFixed(0),
      decodeMs: +decodeMs.toFixed(0),
      losslessSecPerMp: +(losslessMs / 1000 / (n / 1e6)).toFixed(2),
    });
  }
  return out;
})()
"""


def main(url="http://127.0.0.1:8199/index.html"):
    chrome = start_chrome(port=9334)
    try:
        cdp = CDP(ws_url(9334))
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")
        cdp.call("Page.navigate", url=url)
        for _ in range(200):
            ready = cdp.eval("!!window.__lumenSite && window.__lumenSite.state.ready", await_promise=False)
            if ready:
                break
            time.sleep(0.25)
        rows = cdp.eval(PROBE)
        print(json.dumps(rows, indent=1))
        print("console:", cdp.console[:10])
        print("exceptions:", cdp.exceptions[:3])
    finally:
        chrome.terminate()
        chrome.wait(timeout=15)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8199/index.html")
