#!/usr/bin/env python3
"""Build web/lum-viewer.html: a single-file, offline LUMEN viewer.

The viewer embeds web/lum.wasm as base64, so it works when opened straight from
disk (file://) on Windows, Ubuntu, Android and iOS. No server, no install, no
network request: pick a .lum file and it decodes with the same wasm codec the
site uses.

Run after rebuilding the codec:

    python3 scripts/build_viewer.py
"""
from __future__ import annotations

import base64
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WASM = ROOT / "web" / "lum.wasm"
OUT = ROOT / "web" / "lum-viewer.html"

TEMPLATE = r"""<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LUMEN Viewer — .lum dosyasını aç</title>
<meta name="description" content=".lum dosyalarını tarayıcıda açan tek dosyalık, çevrimdışı görüntüleyici. Windows, Ubuntu, Android ve iOS'ta kurulum gerektirmez.">
<meta name="theme-color" content="#f7f8fa">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='16' fill='%234338ca'/><circle cx='32' cy='32' r='13' fill='none' stroke='white' stroke-width='7'/></svg>">
<style>
  :root {
    --bg: #f7f8fa;
    --surface: #ffffff;
    --ink: #101426;
    --muted: #5c6478;
    --dim: #8a92a6;
    --line: #e2e5ee;
    --line-strong: #ccd2e0;
    --accent: #4338ca;
    --accent-soft: #eceeff;
    --good: #0f766e;
    --bad: #b42318;
    --mono: ui-monospace, "SFMono-Regular", "JetBrains Mono", Menlo, Consolas, monospace;
    color-scheme: light;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font: 16px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  header {
    position: sticky; top: 0; z-index: 10;
    background: rgba(255, 255, 255, .88);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--line);
  }
  .bar {
    max-width: 1180px; margin: 0 auto; padding: 12px 20px;
    display: flex; align-items: center; gap: 12px;
  }
  .brand {
    display: flex; align-items: center; gap: 10px;
    font-weight: 700; letter-spacing: -.01em; color: var(--ink);
    text-decoration: none;
  }
  .mark {
    width: 26px; height: 26px; border-radius: 8px; background: var(--accent);
    display: grid; place-items: center; flex: none;
  }
  .mark::after {
    content: ""; width: 11px; height: 11px; border-radius: 50%;
    border: 3px solid #fff;
  }
  .bar .tag {
    font: 600 10.5px/1 var(--mono); letter-spacing: .13em; text-transform: uppercase;
    color: var(--accent); background: var(--accent-soft);
    border-radius: 999px; padding: 6px 9px;
  }
  .bar .spacer { margin-left: auto; }
  .bar .link {
    color: var(--muted); text-decoration: none; font-size: 13.5px;
  }
  .bar .link:hover { color: var(--ink); }
  main { max-width: 1180px; margin: 0 auto; padding: 26px 20px 56px; }
  .intro h1 { margin: 0; font-size: clamp(26px, 3.6vw, 38px); letter-spacing: -.03em; line-height: 1.12; }
  .intro p { margin: 10px 0 0; color: var(--muted); max-width: 72ch; font-size: 15px; }
  .intro b { color: var(--ink); font-weight: 600; }

  .stage {
    margin-top: 22px;
    display: grid;
    grid-template-columns: minmax(0, 1.7fr) minmax(280px, .8fr);
    gap: 18px;
    align-items: start;
  }
  @media (max-width: 880px) { .stage { grid-template-columns: 1fr; } }

  .viewer {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 16px;
    overflow: hidden;
    min-height: 320px;
  }
  .drop {
    min-height: 320px;
    display: grid; place-items: center; gap: 12px;
    padding: 38px 22px; text-align: center;
    border: 1.5px dashed var(--line-strong);
    border-radius: 16px;
    background: #fbfcfe;
    cursor: pointer;
    transition: border-color .15s, background .15s;
  }
  .drop:hover, .drop:focus-visible, .drop.over {
    border-color: var(--accent); background: var(--accent-soft); outline: none;
  }
  .drop .icon {
    width: 52px; height: 52px; border-radius: 14px;
    background: var(--accent-soft); color: var(--accent);
    display: grid; place-items: center;
  }
  .drop .big { font-size: 18px; font-weight: 650; letter-spacing: -.01em; }
  .drop .small { color: var(--muted); font-size: 14px; max-width: 48ch; }
  .drop .formats { font: 500 11px/1 var(--mono); letter-spacing: .08em; color: var(--dim); }

  .canvas-wrap {
    position: relative;
    background:
      linear-gradient(45deg, #eef0f5 25%, transparent 25%),
      linear-gradient(-45deg, #eef0f5 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, #eef0f5 75%),
      linear-gradient(-45deg, transparent 75%, #eef0f5 75%);
    background-size: 18px 18px;
    background-position: 0 0, 0 9px, 9px -9px, -9px 0;
    display: none;
    place-items: center;
    padding: 16px;
    min-height: 320px;
  }
  .canvas-wrap.on { display: grid; }
  .canvas-wrap canvas {
    display: block;
    max-width: 100%;
    max-height: 68vh;
    width: auto; height: auto;
    box-shadow: 0 10px 40px rgba(16, 20, 38, .16);
    border-radius: 4px;
    background: #fff;
  }

  .side { display: grid; gap: 14px; }
  .card {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 16px;
    padding: 18px;
  }
  .card h2 {
    margin: 0 0 12px;
    font: 600 10.5px/1 var(--mono);
    letter-spacing: .15em; text-transform: uppercase; color: var(--dim);
  }
  .meta { display: grid; gap: 9px; }
  .meta div { display: flex; justify-content: space-between; gap: 14px; font-size: 13.5px; }
  .meta dt { color: var(--muted); margin: 0; }
  .meta dd { margin: 0; font-family: var(--mono); font-size: 12.5px; text-align: right; color: var(--ink); }
  .meta .good { color: var(--good); }
  .actions { display: grid; gap: 9px; }
  button {
    font: inherit; font-size: 14px; font-weight: 500;
    color: var(--ink); background: #fff;
    border: 1px solid var(--line-strong);
    border-radius: 10px; padding: 10px 14px; cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    text-decoration: none;
    transition: background .15s, border-color .15s, transform .06s;
  }
  button:hover { background: #f4f6fa; }
  button:active { transform: translateY(1px); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover { background: #3a30b8; }
  button:disabled { opacity: .45; cursor: not-allowed; }
  .status { margin-top: 12px; font-size: 13px; color: var(--muted); display: flex; gap: 9px; align-items: center; min-height: 20px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--dim); flex: none; }
  .dot.ok { background: var(--good); }
  .dot.err { background: var(--bad); }
  .dot.busy { background: var(--accent); animation: pulse 1s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: .25; } }
  .note { margin: 12px 0 0; color: var(--dim); font-size: 12px; line-height: 1.55; }
  .note b { color: var(--muted); }
  .sr-only {
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
  }
  footer {
    border-top: 1px solid var(--line);
    margin-top: 40px;
    padding: 22px 20px 34px;
    color: var(--dim); font-size: 12.5px;
  }
  footer .inner { max-width: 1180px; margin: 0 auto; display: flex; flex-wrap: wrap; gap: 14px; align-items: center; }
  footer a { color: var(--muted); text-decoration: none; }
  footer a:hover { color: var(--ink); }
</style>
</head>
<body>
<header>
  <div class="bar">
    <a class="brand" href="https://muhammetodosks.github.io/lum/"><span class="mark"></span>LUMEN</a>
    <span class="tag">Viewer</span>
    <span class="spacer"></span>
    <a class="link" href="https://muhammetodosks.github.io/lum/">Siteye dön</a>
  </div>
</header>

<main>
  <div class="intro">
    <h1>.lum dosyanızı burada açın.</h1>
    <p>
      Bu sayfa <b>tek bir HTML dosyası</b>: LUMEN codec'i içine gömülü.
      İnternet bağlantısı, kurulum veya sunucu gerekmez. Dosyayı seçin ya da
      pencereye bırakın; görüntü cihazınızda çözülür.
    </p>
  </div>

  <div class="stage">
    <div class="viewer">
      <div class="drop" id="drop" tabindex="0" role="button" aria-label=".lum dosyası seç">
        <div class="icon" aria-hidden="true">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>
        </div>
        <div class="big">.lum dosyasını bırakın</div>
        <div class="small">ya da tıklayıp seçin. Dosya cihazınızdan çıkmaz; hiçbir yere yüklenmez.</div>
        <div class="formats">LUM · LUMA\r\n\x1A\n · image/lum</div>
      </div>
      <div class="canvas-wrap" id="canvas-wrap">
        <canvas id="canvas" width="4" height="4"></canvas>
      </div>
      <input id="file" type="file" accept=".lum,image/lum" class="sr-only">
    </div>

    <aside class="side">
      <div class="card">
        <h2>Dosya</h2>
        <dl class="meta" id="meta">
          <div><dt>Durum</dt><dd id="meta-state">Dosya bekleniyor</dd></div>
          <div><dt>Boyut</dt><dd id="meta-size">—</dd></div>
          <div><dt>Görüntü</dt><dd id="meta-dims">—</dd></div>
          <div><dt>Renk</dt><dd id="meta-color">—</dd></div>
          <div><dt>Mod</dt><dd id="meta-mode">—</dd></div>
          <div><dt>Ham veri</dt><dd id="meta-raw">—</dd></div>
          <div><dt>Küçülme</dt><dd id="meta-ratio" class="good">—</dd></div>
          <div><dt>Çözme</dt><dd id="meta-ms">—</dd></div>
        </dl>
        <div class="status"><span class="dot" id="dot"></span><span id="status">Codec hazır.</span></div>
      </div>

      <div class="card">
        <h2>İşlem</h2>
        <div class="actions">
          <button id="pick" class="primary" type="button">Dosya seç</button>
          <button id="png" type="button" disabled>PNG olarak indir</button>
          <button id="reset" type="button" disabled>Başka dosya aç</button>
        </div>
        <p class="note">
          <b>Windows / Ubuntu:</b> dosyayı indirin, çift tıklayın, tarayıcıda açılır.
          <b>Android / iOS:</b> indirilen dosyayı tarayıcıda açın ve .lum dosyasını seçin.
        </p>
      </div>

      <div class="card">
        <h2>Not</h2>
        <p class="note" style="margin-top:0">
          .lum standart bir işletim sistemi formatı değildir; bu yüzden çift tıklayınca
          doğrudan açılmaz. Bu görüntüleyici o eksiği kapatır. Aynı dosyayı
          <a href="https://muhammetodosks.github.io/lum/" style="color:var(--accent)">LUMEN sitesinde</a>
          de açabilirsiniz.
        </p>
      </div>
    </aside>
  </div>
</main>

<footer>
  <div class="inner">
    <span>LUMEN Viewer · MIT · çevrimdışı</span>
    <a href="https://github.com/muhammetodosks/lum">github.com/muhammetodosks/lum</a>
    <span style="margin-left:auto">Codec bu dosyanın içinde gömülü.</span>
  </div>
</footer>

<script id="wasm-b64" type="text/plain">__WASM_BASE64__</script>
<script>
(function () {
  "use strict";

  var MAX_BYTES = 100 * 1000 * 1000;
  var MAGIC = [0x4c, 0x55, 0x4d, 0x41, 0x0d, 0x0a, 0x1a, 0x0a];
  var api = null;
  var memory = null;
  var scratch = 0;
  var last = null;

  var $ = function (id) { return document.getElementById(id); };
  var el = {
    drop: $("drop"), file: $("file"), pick: $("pick"), reset: $("reset"),
    png: $("png"), canvas: $("canvas"), wrap: $("canvas-wrap"),
    dot: $("dot"), status: $("status"),
    state: $("meta-state"), size: $("meta-size"), dims: $("meta-dims"),
    color: $("meta-color"), mode: $("meta-mode"), raw: $("meta-raw"),
    ratio: $("meta-ratio"), ms: $("meta-ms"),
  };

  function bytesText(n) {
    var a = Math.abs(n);
    if (a < 1000) return Math.round(n) + " B";
    if (a < 1e6) return (n / 1000).toLocaleString("tr-TR", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + " KB";
    return (n / 1e6).toLocaleString("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " MB";
  }

  function say(text, kind) {
    el.status.textContent = text;
    el.dot.className = "dot" + (kind ? " " + kind : "");
  }

  function fromBase64(text) {
    var bin = atob(text.trim());
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
    return out;
  }

  function view(Type, ptr, length) {
    return new Type(memory.buffer, ptr, length);
  }

  function copyIn(bytes) {
    var ptr = api.lum_alloc(bytes.byteLength);
    if (!ptr) throw new Error("wasm belleği ayrılamadı");
    view(Uint8Array, ptr, bytes.byteLength).set(bytes);
    return ptr;
  }

  function ensureScratch() {
    if (!scratch) scratch = api.lum_alloc(64);
    return scratch;
  }

  function info(bytes) {
    var ptr = copyIn(bytes);
    var meta = ensureScratch();
    var rc;
    try {
      rc = api.lum_wasm_info(ptr, bytes.byteLength, meta);
    } finally {
      api.lum_alloc_free(ptr, bytes.byteLength);
    }
    if (rc < 0) throw new Error(errorText(rc));
    var m = view(Int32Array, meta, 6).slice();
    return {
      width: m[0], height: m[1], channels: m[2],
      lossy: m[3] === 1, quality: m[4], version: m[5],
    };
  }

  function errorText(code) {
    var map = {
      "-100": "LUMEN dosyası değil (magic hatalı)",
      "-101": "desteklenmeyen container sürümü",
      "-102": "dosya eksik / bozuk",
      "-103": "geçersiz piksel verisi",
      "-104": "desteklenmeyen kanal sayısı",
    };
    return map[String(code)] || ("çözme hatası (" + code + ")");
  }

  function decode(bytes) {
    var ptr = copyIn(bytes);
    var meta = ensureScratch();
    var out = 0;
    try {
      out = api.lum_wasm_decode(ptr, bytes.byteLength, meta);
    } finally {
      api.lum_alloc_free(ptr, bytes.byteLength);
    }
    var m = view(Int32Array, meta, 5).slice();
    var status = m[0], width = m[1], height = m[2], channels = m[3], kind = m[4];
    if (!out) throw new Error(errorText(status));
    var size = width * height * channels;
    var data = view(Uint8Array, out, size).slice();
    api.lum_free_buf(out, size);
    return {
      width: width, height: height, channels: channels,
      lossy: kind === 1, data: data,
    };
  }

  function toRGBA(image) {
    if (image.channels === 4) return new Uint8ClampedArray(image.data);
    var width = image.width, height = image.height, channels = image.channels, data = image.data;
    var out = new Uint8ClampedArray(width * height * 4);
    for (var i = 0, j = 0; i < width * height; i += 1, j += channels) {
      if (channels === 1) {
        var v = data[j];
        out[i * 4] = v; out[i * 4 + 1] = v; out[i * 4 + 2] = v;
      } else {
        out[i * 4] = data[j];
        out[i * 4 + 1] = data[j + 1];
        out[i * 4 + 2] = data[j + 2];
      }
      out[i * 4 + 3] = channels === 2 ? data[j + 1] : 255;
    }
    return out;
  }

  function show(image, meta, bytes, decodeMs) {
    last = image;
    var rgba = toRGBA(image);
    var canvas = el.canvas;
    canvas.width = image.width;
    canvas.height = image.height;
    canvas.getContext("2d").putImageData(new ImageData(rgba, image.width, image.height), 0, 0);
    el.wrap.classList.add("on");
    el.drop.style.display = "none";

    var raw = image.width * image.height * image.channels;
    el.state.textContent = "Açıldı";
    el.state.style.color = "var(--good)";
    el.size.textContent = bytesText(bytes);
    el.dims.textContent = image.width + " × " + image.height;
    el.color.textContent = ["gri", "gri + alfa", "RGB", "RGBA"][image.channels - 1] || (image.channels + " kanal");
    el.mode.textContent = (instanceMeta.lossy ? "kayıplı q" + instanceMeta.quality : "kayıpsız") + " · v" + instanceMeta.version;
    el.raw.textContent = bytesText(raw);
    el.ratio.textContent = (1 - bytes / raw) * 100 >= 0
      ? "%" + ((1 - bytes / raw) * 100).toLocaleString("tr-TR", { minimumFractionDigits: 1, maximumFractionDigits: 1 })
      : "—";
    el.ms.textContent = decodeMs.toLocaleString("tr-TR", { maximumFractionDigits: 1 }) + " ms";
    el.png.disabled = false;
    el.reset.disabled = false;
    say("Dosya çözüldü. Görüntü cihazınızda işlendi.", "ok");
  }

  var instanceMeta = null;

  function openFile(file) {
    if (!file) return;
    if (file.size > MAX_BYTES) {
      say("Dosya 100 MB sınırını aşıyor (" + bytesText(file.size) + ").", "err");
      return;
    }
    say("Dosya okunuyor…", "busy");
    file.arrayBuffer().then(function (buf) {
      var bytes = new Uint8Array(buf);
      var ok = bytes.length >= 8 && MAGIC.every(function (b, i) { return bytes[i] === b; });
      if (!ok) throw new Error("Bu dosya bir .lum container'ı değil.");
      instanceMeta = info(bytes);
      var t0 = performance.now();
      var image = decode(bytes);
      var ms = performance.now() - t0;
      show(image, instanceMeta, bytes.length, ms);
    }).catch(function (err) {
      say("Açılamadı: " + err.message, "err");
      el.state.textContent = "Hata";
      el.state.style.color = "var(--bad)";
    });
  }

  function reset() {
    last = null;
    instanceMeta = null;
    el.wrap.classList.remove("on");
    el.drop.style.display = "";
    el.canvas.width = 4;
    el.canvas.height = 4;
    el.file.value = "";
    el.state.textContent = "Dosya bekleniyor";
    el.state.style.color = "";
    el.size.textContent = "—";
    el.dims.textContent = "—";
    el.color.textContent = "—";
    el.mode.textContent = "—";
    el.raw.textContent = "—";
    el.ratio.textContent = "—";
    el.ms.textContent = "—";
    el.png.disabled = true;
    el.reset.disabled = true;
    say("Codec hazır.", "");
  }

  function downloadPng() {
    if (!last) return;
    var rgba = toRGBA(last);
    var c = document.createElement("canvas");
    c.width = last.width;
    c.height = last.height;
    c.getContext("2d").putImageData(new ImageData(rgba, last.width, last.height), 0, 0);
    c.toBlob(function (blob) {
      if (!blob) { say("PNG oluşturulamadı.", "err"); return; }
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "lumen-goruntu.png";
      a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
    }, "image/png");
  }

  el.drop.addEventListener("click", function () { el.file.click(); });
  el.drop.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); el.file.click(); }
  });
  el.pick.addEventListener("click", function (e) { e.stopPropagation(); el.file.click(); });
  el.file.addEventListener("change", function () { openFile(el.file.files[0]); });
  el.reset.addEventListener("click", reset);
  el.png.addEventListener("click", downloadPng);

  ["dragenter", "dragover"].forEach(function (type) {
    el.drop.addEventListener(type, function (e) { e.preventDefault(); el.drop.classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (type) {
    el.drop.addEventListener(type, function () { el.drop.classList.remove("over"); });
  });
  el.drop.addEventListener("drop", function (e) {
    e.preventDefault();
    openFile(e.dataTransfer.files[0]);
  });

  // Boot: decode the embedded wasm, then enable the picker.
  try {
    var wasm = fromBase64(document.getElementById("wasm-b64").textContent);
    WebAssembly.instantiate(wasm, {}).then(function (result) {
      api = result.instance.exports;
      memory = api.memory;
      ensureScratch();
      say("Codec hazır. Bir .lum dosyası seçin.", "ok");
      el.state.textContent = "Hazır";
      el.state.style.color = "";
    }).catch(function (err) {
      say("Codec yüklenemedi: " + err.message, "err");
      el.state.textContent = "Codec hatası";
      el.state.style.color = "var(--bad)";
    });
  } catch (err) {
    say("Codec yüklenemedi: " + err.message, "err");
  }
})();
</script>
</body>
</html>
"""


def main() -> int:
    if not WASM.exists():
        print(f"missing {WASM}; run scripts/build_wasm.sh first", file=sys.stderr)
        return 1
    raw = WASM.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    html = TEMPLATE.replace("__WASM_BASE64__", encoded)
    OUT.write_text(html, encoding="utf-8")
    kib = len(raw) / 1024
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes) with {kib:.0f} KiB wasm embedded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
