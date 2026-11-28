#!/usr/bin/env python3
"""Extra end-to-end checks for the LUMEN site beyond the selftest.

Covers the three complaints that motivated the rewrite:

  1. files larger than 100 MB are rejected before any work starts;
  2. a multi-megabyte photo no longer freezes the tab (the codec runs in a
     worker, so animation frames keep ticking) and Cancel really stops it;
  3. a downloaded .lum file opens both on the site and in the standalone
     viewer, offline.

It also writes mobile and desktop screenshots so the layout can be eyeballed.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import websockets.sync.client as ws

SHOTS = "/home/mami/site-shots"


def start_chrome(port=9334):
    profile = f"/tmp/opencode/chrome-extra-{port}"
    shutil.rmtree(profile, ignore_errors=True)
    return subprocess.Popen(
        [
            "google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
            "--disk-cache-size=1", f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--window-size=1440,900", "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def ws_url(port=9334):
    for _ in range(80):
        try:
            data = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
            for page in data:
                if page.get("type") == "page":
                    return page["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.25)
    raise SystemExit("chrome did not expose a page target")


class CDP:
    def __init__(self, url):
        self.ws = ws.connect(url, max_size=128 * 1024 * 1024)
        self.n = 0
        self.console = []
        self.exceptions = []

    def call(self, method, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self._event(msg)

    def _event(self, msg):
        m = msg.get("method")
        p = msg.get("params", {})
        if m == "Runtime.consoleAPICalled":
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", []))
            self.console.append((p.get("type"), text))
        elif m == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            self.exceptions.append(d.get("exception", {}).get("description") or d.get("text"))
        elif m == "Log.entryAdded":
            e = p.get("entry", {})
            if e.get("level") in ("error", "warning"):
                self.console.append((e["level"], e.get("text", "")))

    def eval(self, expr, await_promise=True):
        r = self.call("Runtime.evaluate", expression=expr, awaitPromise=await_promise,
                      returnByValue=True, userGesture=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(json.dumps(r["exceptionDetails"])[:800])
        return r["result"].get("value")

    def wait(self, expr, timeout=60, interval=0.15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = self.eval(expr, await_promise=False)
            if value:
                return value
            time.sleep(interval)
        return None

    def shot(self, name, width=None, height=None):
        if width:
            self.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                      deviceScaleFactor=2, mobile=True)
        data = self.call("Page.captureScreenshot", format="png")["data"]
        os.makedirs(SHOTS, exist_ok=True)
        path = os.path.join(SHOTS, name)
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return path


DROP_JS = """(file) => {
  const dt = new DataTransfer();
  dt.items.add(file);
  document.getElementById('drop').dispatchEvent(
    new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
}"""


def drop_expr(make_file_js):
    return f"""(() => {{
  const file = {make_file_js};
  const dt = new DataTransfer();
  dt.items.add(file);
  document.getElementById('drop').dispatchEvent(
    new DragEvent('drop', {{ dataTransfer: dt, bubbles: true, cancelable: true }}));
  return {{ name: file.name, size: file.size }};
}})()"""


def main(base):
    results = {}
    chrome = start_chrome()
    try:
        cdp = CDP(ws_url())
        for method in ("Runtime.enable", "Log.enable", "Page.enable"):
            cdp.call(method)

        cdp.call("Page.navigate", url=f"{base}/index.html")
        ready = cdp.wait("document.getElementById('status')?.textContent.includes('Codec hazır')", 60)
        results["codec_ready"] = bool(ready)
        print("codec ready:", results["codec_ready"], flush=True)

        # -- 1. 100 MB guard ---------------------------------------------------
        r = cdp.eval(drop_expr(
            "(function(){ const f = new File([new Uint8Array(16)], 'buyuk.png', {type:'image/png'});"
            " Object.defineProperty(f, 'size', {value: 100000001}); return f; })()"
        ), await_promise=False)
        print("big file drop:", r, flush=True)
        guard = cdp.wait("document.getElementById('status').textContent.includes('100 MB')", 5)
        results["oversize_rejected"] = bool(guard)
        results["oversize_status"] = cdp.eval("document.getElementById('status').textContent", False)
        results["drop_shows_limit"] = "100 MB" in (cdp.eval(
            "document.getElementById('drop').innerText", False) or "")
        print("guard:", results["oversize_status"], flush=True)

        # -- 2. multi-megabyte photo, full size, worker stays responsive -------
        size = cdp.eval("""(() => {
          document.getElementById('maxedge').value = '0';
          const c = document.createElement('canvas');
          c.width = 1200; c.height = 900;
          const ctx = c.getContext('2d');
          const img = ctx.createImageData(1200, 900);
          let seed = 1234567;
          for (let i = 0; i < img.data.length; i += 4) {
            seed = (seed * 1103515245 + 12345) & 0x7fffffff;
            img.data[i] = seed & 255;
            img.data[i+1] = (seed >> 8) & 255;
            img.data[i+2] = (seed >> 16) & 255;
            img.data[i+3] = 255;
          }
          ctx.putImageData(img, 0, 0);
          return new Promise((resolve) => c.toBlob((b) => {
            window.__photoBlob = b;
            resolve(b.size);
          }, 'image/png'));
        })()""")
        results["photo_png_bytes"] = size
        print("synthetic photo PNG bytes:", size, flush=True)
        cdp.eval("""(() => {
          window.__ticks = 0;
          (function loop(){ window.__ticks++; requestAnimationFrame(loop); })();
          const dt = new DataTransfer();
          dt.items.add(new File([window.__photoBlob], 'buyuk-foto.png', {type:'image/png'}));
          document.getElementById('drop').dispatchEvent(
            new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
        })()""", await_promise=False)
        time.sleep(2.0)
        ticks = cdp.eval("window.__ticks", False)
        busy = cdp.eval("!document.getElementById('cancel').hidden", False)
        results["worker_ticks_2s"] = ticks
        results["cancel_visible"] = bool(busy)
        print("ticks while encoding:", ticks, "cancel visible:", busy, flush=True)

        cdp.eval("document.getElementById('cancel').click()", await_promise=False)
        cancelled = cdp.wait("document.getElementById('status').textContent.includes('İptal edildi')", 15)
        results["cancel_works"] = bool(cancelled)
        results["cancel_status"] = cdp.eval("document.getElementById('status').textContent", False)
        print("after cancel:", results["cancel_status"], flush=True)

        # -- 3. downloaded sample .lum opens on the site -----------------------
        cdp.eval("document.getElementById('maxedge').value = '512'", await_promise=False)
        cdp.eval("""(() => {
          fetch('docs/assets/formats/ornek.lum')
            .then((r) => r.blob())
            .then((b) => {
              const dt = new DataTransfer();
              dt.items.add(new File([b], 'ornek.lum', { type: 'image/lum' }));
              document.getElementById('drop').dispatchEvent(
                new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
            });
        })()""", await_promise=False)
        opened = cdp.wait("document.getElementById('meta').innerText.includes('ornek.lum')", 30)
        time.sleep(0.9)
        results["lum_open"] = bool(opened)
        results["lum_meta"] = cdp.eval("document.getElementById('meta').innerText", False)
        results["lum_counters"] = cdp.eval("document.querySelector('.counters').innerText", False)
        results["lum_psnr_is_dash"] = cdp.eval(
            "document.querySelector('#c-psnr .v').innerText.trim()", False)
        print("lum open:", results["lum_meta"].replace("\n", " | "), flush=True)

        # -- screenshots -------------------------------------------------------
        cdp.call("Emulation.clearDeviceMetricsOverride")
        cdp.call("Page.navigate", url=f"{base}/index.html")
        cdp.wait("document.getElementById('status')?.textContent.includes('Codec hazır')", 60)
        results["shot_desktop"] = cdp.shot("desktop-hero.png")
        cdp.eval("document.getElementById('indir').scrollIntoView()", False)
        time.sleep(0.4)
        results["shot_desktop_downloads"] = cdp.shot("desktop-downloads.png")
        cdp.call("Page.navigate", url=f"{base}/index.html")
        cdp.wait("document.getElementById('status')?.textContent.includes('Codec hazır')", 60)
        results["shot_mobile"] = cdp.shot("mobile-hero.png", 390, 844)
        cdp.eval("document.getElementById('lab').scrollIntoView()", False)
        time.sleep(0.4)
        results["shot_mobile_lab"] = cdp.shot("mobile-lab.png", 390, 844)
        cdp.eval("document.getElementById('indir').scrollIntoView()", False)
        time.sleep(0.4)
        results["shot_mobile_downloads"] = cdp.shot("mobile-downloads.png", 390, 844)

        # -- no horizontal overflow on a narrow phone --------------------------
        results["mobile_overflow"] = cdp.eval(
            "({client: document.documentElement.clientWidth,"
            "  scroll: document.documentElement.scrollWidth})", False)
        print("mobile width:", results["mobile_overflow"], flush=True)

        # -- 4. standalone viewer opens the same .lum offline ------------------
        cdp.call("Emulation.clearDeviceMetricsOverride")
        cdp.call("Page.navigate", url=f"{base}/web/lum-viewer.html")
        viewer_ready = cdp.wait(
            "document.getElementById('status')?.textContent.toLowerCase().includes('hazır')", 30)
        results["viewer_ready"] = bool(viewer_ready)
        cdp.eval("""(() => {
          fetch('../docs/assets/formats/ornek.lum')
            .then((r) => r.blob())
            .then((b) => {
              const dt = new DataTransfer();
              dt.items.add(new File([b], 'ornek.lum', { type: 'image/lum' }));
              document.getElementById('drop').dispatchEvent(
                new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
            });
        })()""", await_promise=False)
        shown = cdp.wait("!!document.querySelector('canvas')", 20)
        time.sleep(0.5)
        results["viewer_shows_canvas"] = bool(shown)
        results["viewer_status"] = cdp.eval("document.getElementById('status').textContent", False)
        results["viewer_meta"] = cdp.eval(
            "(document.getElementById('meta') || {}).innerText || ''", False)
        print("viewer:", results["viewer_status"], flush=True)

        results["console"] = cdp.console[:20]
        results["exceptions"] = cdp.exceptions[:5]
        print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
    finally:
        chrome.terminate()
        chrome.wait(timeout=15)

    ok = (
        results.get("codec_ready")
        and results.get("oversize_rejected")
        and results.get("drop_shows_limit")
        and (results.get("photo_png_bytes") or 0) > 1_000_000
        and (results.get("worker_ticks_2s") or 0) > 20
        and results.get("cancel_visible")
        and results.get("cancel_works")
        and results.get("lum_open")
        and results.get("viewer_ready")
        and results.get("viewer_shows_canvas")
        and (results.get("mobile_overflow") or {}).get("scroll", 9999)
            <= (results.get("mobile_overflow") or {}).get("client", 0) + 1
        and not results.get("exceptions")
    )
    print("RESULT:", "PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8199"))
