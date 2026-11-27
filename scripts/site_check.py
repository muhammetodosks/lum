#!/usr/bin/env python3
"""Drive headless Chrome over CDP to verify the LUMEN site for real.

No virtual time: performance.now() advances normally, so the timings the page
reports are the timings the work actually took.
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.request

import websockets.sync.client as ws


def start_chrome(port=9333):
    # A fresh profile per run: Chrome caches lum.wasm in the old shared profile,
    # and a stale codec makes the check test yesterday's binary.
    profile = f"/tmp/opencode/chrome-profile-{port}"
    shutil.rmtree(profile, ignore_errors=True)
    return subprocess.Popen(
        [
            "google-chrome",
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disk-cache-size=1",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def ws_url(port=9333):
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
        self.ws = ws.connect(url, max_size=64 * 1024 * 1024)
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

    def drain(self):
        while True:
            try:
                msg = self.ws.recv(timeout=0.05)
            except TimeoutError:
                return
            else:
                self._event(json.loads(msg))

    def eval(self, expr, await_promise=True):
        r = self.call(
            "Runtime.evaluate",
            expression=expr,
            awaitPromise=await_promise,
            returnByValue=True,
            userGesture=True,
        )
        if r.get("exceptionDetails"):
            raise RuntimeError(json.dumps(r["exceptionDetails"])[:800])
        return r["result"].get("value")


def main(url):
    chrome = start_chrome()
    try:
        cdp = CDP(ws_url())
        cdp.call("Runtime.enable")
        cdp.call("Log.enable")
        cdp.call("Page.enable")
        cdp.call("Page.navigate", url=url)
        time.sleep(0.5)
        print("location:", cdp.eval("location.href", await_promise=False), flush=True)
        deadline = time.time() + 180
        result = None
        while time.time() < deadline:
            cdp.drain()
            result = cdp.eval(
                "(() => { const n = document.getElementById('selftest-result');"
                " return n ? n.textContent : null; })()",
                await_promise=False,
            )
            if result:
                break
            time.sleep(0.3)
        print("selftest:", result or "TIMEOUT", flush=True)
        # Counters animate for 520 ms; read the settled values.
        time.sleep(0.8)
        print(
            "status:",
            cdp.eval("document.getElementById('status').textContent", await_promise=False),
        )
        print("rows:", cdp.eval("document.getElementById('tbody').innerText", await_promise=False))
        print("note:", cdp.eval("document.getElementById('table-note').textContent", await_promise=False))
        print(
            "counters:",
            cdp.eval(
                "JSON.stringify({save: document.querySelector('#c-save .v').innerText,"
                " saveFinal: document.querySelector('#c-save .v [data-v]')?.dataset.final ?? null,"
                " ratio: document.querySelector('#c-ratio .v').innerText,"
                " ratioFinal: document.querySelector('#c-ratio .v [data-v]')?.dataset.final ?? null,"
                " psnr: document.querySelector('#c-psnr .v').innerText,"
                " psnrFinal: document.querySelector('#c-psnr .v [data-v]')?.dataset.final ?? null,"
                " time: document.querySelector('#c-time .v').innerText,"
                " timeFinal: document.querySelector('#c-time .v [data-v]')?.dataset.final ?? null})",
                await_promise=False,
            ),
        )
        print("meta:", cdp.eval("document.getElementById('meta').innerText", await_promise=False))
        print("console:", cdp.console[:20], flush=True)
        print("exceptions:", cdp.exceptions[:5], flush=True)
    finally:
        chrome.terminate()
        chrome.wait(timeout=15)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8199/index.html?selftest")
