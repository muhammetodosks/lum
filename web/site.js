// LUMEN product page — the real codec, off the main thread.
//
// The wasm encoder is single-threaded and lossless mode costs roughly 6.5 s per
// megapixel. A 2 MB phone photo used to lock the tab while it ran. Everything
// that touches the codec now lives in ./lum-worker.js: the page only reads the
// file, hands the worker one RGBA buffer, and paints the result. A cancelled or
// superseded run is stopped by terminating the worker, so stale numbers can
// never overwrite a newer file.

import { Lumen } from "./lum.js";

const $ = (id) => document.getElementById(id);

const el = {
  navToggle: $("nav-toggle"), navLinks: $("nav-links"),
  drop: $("drop"), file: $("file"), pick: $("pick"), sample: $("sample"),
  dot: $("dot"), status: $("status"), cancel: $("cancel"),
  results: $("results"), meta: $("meta"), tbody: $("tbody"), tableNote: $("table-note"),
  segs: $("segs"), maxedge: $("maxedge"), scaleNote: $("scale-note"),
  before: $("before"), after: $("cv-after"), split: $("split"),
  capBefore: $("cap-before"), capAfter: $("cap-after"),
  dlLum: $("dl-lum"), dlPng: $("dl-png"), copy: $("copy"),
};

const MAX_BYTES = 100 * 1000 * 1000;
const MAX_EDGE_DEFAULT = 512;
const WORKER_URL = new URL("./lum-worker.js", import.meta.url);

const state = {
  ready: false,
  quality: 90,          // 0 = lossless
  source: null,         // { image, name, originalBytes, width, height, ... }
  browserRows: [],      // PNG / JPEG / WebP measurements, reused on quality change
  lum: null,            // { bytes, info, rgba, encodeMs, decodeMs, ... }
  after: null,          // last decoded raster, for the PNG button and previews
  file: null,
  name: "goruntu",
  run: 0,               // bumped whenever a newer file/action supersedes a run
  busy: false,
};

// ---------------------------------------------------------------- utilities

const tr = (x, d = 1) =>
  x.toLocaleString("tr-TR", { minimumFractionDigits: d, maximumFractionDigits: d });

function bytesText(n) {
  const a = Math.abs(n);
  if (a < 1000) return `${Math.round(n)} B`;
  if (a < 1e6) return `${tr(n / 1000, 1)} KB`;
  return `${tr(n / 1e6, 2)} MB`;
}

function bytesShort(n) {
  const a = Math.abs(n);
  if (a < 1000) return `${Math.round(n)} B`;
  if (a < 1e6) return `${Math.round(n / 1000)} KB`;
  return `${tr(n / 1e6, 1)} MB`;
}

function say(text, kind = "") {
  el.status.textContent = text;
  el.dot.className = `dot ${kind}`;
}

const nextFrame = () => new Promise((r) => requestAnimationFrame(() => r()));
const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** Count a real measured value up on screen. The final frame is the measurement. */
function animate(node, to, format) {
  if (node._raf) cancelAnimationFrame(node._raf);
  node.dataset.final = String(to);
  if (reduceMotion || !isFinite(to) || to === 0) {
    node.textContent = format(to);
    return;
  }
  const from = 0;
  const t0 = performance.now();
  const dur = 520;
  const step = (t) => {
    const k = Math.min(1, (t - t0) / dur);
    const eased = 1 - Math.pow(1 - k, 3);
    node.textContent = format(from + (to - from) * eased);
    if (k < 1) node._raf = requestAnimationFrame(step);
    else node.textContent = format(to);
  };
  node._raf = requestAnimationFrame(step);
}

function setCounter(id, value, format = bytesText) {
  const node = document.querySelector(`#${id} [data-v]`);
  if (!node) return;
  if (value === null || !isFinite(value)) {
    if (node._raf) cancelAnimationFrame(node._raf);
    node._raf = 0;
    delete node.dataset.final;
    node.textContent = value === null ? "—" : "∞";
    return;
  }
  animate(node, value, format);
}

const LUM_MAGIC = [0x4c, 0x55, 0x4d, 0x41, 0x0d, 0x0a, 0x1a, 0x0a];
function isLum(bytes) {
  return bytes.length >= 8 && LUM_MAGIC.every((b, i) => bytes[i] === b);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function samplesOf(buf) {
  return buf && buf.data ? buf.data : buf;
}

function imageDataOf(buf) {
  const data = samplesOf(buf);
  return new ImageData(
    data instanceof Uint8ClampedArray ? data : new Uint8ClampedArray(data),
    buf.width,
    buf.height,
  );
}

function drawTo(image) {
  const canvas = document.createElement("canvas");
  canvas.width = image.width;
  canvas.height = image.height;
  canvas.getContext("2d", { willReadFrequently: true }).putImageData(imageDataOf(image), 0, 0);
  return canvas;
}

// ------------------------------------------------------------------ metrics

function psnrRgb(a, b) {
  const da = samplesOf(a);
  const db = samplesOf(b);
  const n = Math.min(da.length, db.length);
  let sum = 0;
  let count = 0;
  for (let i = 0; i < n; i += 4) {
    for (let c = 0; c < 3; c += 1) {
      const d = da[i + c] - db[i + c];
      sum += d * d;
      count += 1;
    }
  }
  if (!count) return 0;
  const mse = sum / count;
  return mse === 0 ? Infinity : 10 * Math.log10(65025 / mse);
}

function alphaError(a, b) {
  const da = samplesOf(a);
  const db = samplesOf(b);
  let worst = 0;
  let differing = 0;
  const n = Math.min(da.length, db.length);
  for (let i = 3; i < n; i += 4) {
    const d = Math.abs(da[i] - db[i]);
    if (d) {
      differing += 1;
      if (d > worst) worst = d;
    }
  }
  return { worst, differing };
}

function hasTransparency(image) {
  const d = image.data;
  for (let i = 3; i < d.length; i += 4) if (d[i] !== 255) return true;
  return false;
}

// ------------------------------------------------------------------- worker

let worker = null;
let seq = 0;
let waiting = new Map();

function rejectWaiting(reason) {
  for (const { reject } of waiting.values()) reject(reason);
  waiting.clear();
}

function spawnWorker() {
  if (worker) worker.terminate();
  rejectWaiting(new Error("worker yeniden başlatıldı"));
  worker = new Worker(WORKER_URL, { type: "module" });
  worker.addEventListener("message", (event) => {
    const { id, ok, result, error, code } = event.data || {};
    const pending = waiting.get(id);
    if (!pending) return;                 // a cancelled run's reply
    waiting.delete(id);
    if (ok) pending.resolve(result);
    else pending.reject(Object.assign(new Error(error || "worker hatası"), { code }));
  });
  worker.addEventListener("error", (event) => {
    rejectWaiting(new Error(event.message || "worker başlatılamadı"));
    state.ready = false;
    say(`Codec çalıştırılamadı: ${event.message || "worker hatası"}. Sayfayı bir HTTP sunucusundan açın.`, "err");
  });
}

function call(type, payload, transfer = []) {
  if (!worker) spawnWorker();
  const id = ++seq;
  return new Promise((resolve, reject) => {
    waiting.set(id, { resolve, reject });
    try {
      worker.postMessage({ id, type, payload }, transfer);
    } catch (err) {
      waiting.delete(id);
      reject(err);
    }
  });
}

async function restartWorker() {
  spawnWorker();
  const t0 = performance.now();
  const info = await call("ping");
  state.ready = true;
  return { ms: performance.now() - t0, ...info };
}

async function ensureWorker({ force = false } = {}) {
  if (!worker || force || !state.ready) return restartWorker();
  return null;
}

// ------------------------------------------------------------------- encode

async function pixelsOfBlob(blob) {
  const bitmap = await createImageBitmap(blob);
  const canvas = document.createElement("canvas");
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(bitmap, 0, 0);
  if (bitmap.close) bitmap.close();
  return ctx.getImageData(0, 0, canvas.width, canvas.height);
}

async function browserEncode(source, type, quality) {
  const canvas = drawTo(source);
  const t0 = performance.now();
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, type, quality));
  const ms = performance.now() - t0;
  if (!blob || blob.type !== type) return null;
  const decoded = await pixelsOfBlob(blob);
  return {
    bytes: blob.size,
    ms,
    psnr: psnrRgb(source, decoded),
    alpha: alphaError(source, decoded),
    decoded,
  };
}

async function prepareSource(s) {
  // One copy crosses the boundary; the worker derives RGB or RGBA itself.
  const rgba = new Uint8ClampedArray(s.image.data);
  await call("prepare", {
    width: s.width,
    height: s.height,
    hasAlpha: s.hasAlpha,
    rgba,
  }, [rgba.buffer]);
}

function applyChosen(m) {
  const c = m.chosen;
  state.lum = {
    bytes: c.bytes,
    info: c.info,
    rgba: c.rgba,
    width: c.width,
    height: c.height,
    quality: c.quality,
    psnr: c.psnr,
    encodeMs: c.encodeMs,
    decodeMs: c.decodeMs,
  };
  state.after = { width: c.width, height: c.height, data: c.rgba };
  el.dlLum.disabled = false;
  el.dlPng.disabled = false;
}

function lumRows(m, s) {
  const rows = [
    rowOf("LUMEN", "kayıpsız", true, {
      bytes: m.lossless.bytes.length,
      encodeMs: m.lossless.encodeMs,
      decodeMs: m.lossless.decodeMs,
      psnr: m.lossless.psnr,
      alpha: m.lossless.alpha,
    }, s.originalBytes, "wasm"),
  ];
  if (m.lossy) {
    rows.push(rowOf("LUMEN", `q${m.lossy.quality}`, true, {
      bytes: m.lossy.bytes.length,
      encodeMs: m.lossy.encodeMs,
      decodeMs: m.lossy.decodeMs,
      psnr: m.lossy.psnr,
      alpha: m.lossy.alpha,
    }, s.originalBytes, "wasm"));
  }
  return rows;
}

// --------------------------------------------------------------------- rows

function rowOf(label, kind, isSubject, m, baseline, source, note) {
  return {
    label,
    kind,
    subject: isSubject,
    bytes: m.bytes,
    meanMs: m.ms ?? m.encodeMs,
    decodeMs: m.decodeMs ?? null,
    psnr: m.psnr,
    alpha: m.alpha ?? null,
    delta: baseline ? baseline - m.bytes : 0,
    note: note || "",
    source,
  };
}

// --------------------------------------------------------------------- flow

function resetView() {
  el.results.classList.remove("on");
  el.tbody.innerHTML = "";
  el.tableNote.textContent = "";
}

function resetDownloadButtons() {
  el.dlLum.disabled = true;
  el.dlPng.disabled = true;
}

function startJob() {
  state.busy = true;
  el.cancel.hidden = false;
}

function endJob(run) {
  if (run !== state.run) return;
  state.busy = false;
  el.cancel.hidden = true;
}

async function cancelJob() {
  state.run += 1;
  state.busy = false;
  el.cancel.hidden = true;
  resetDownloadButtons();
  try {
    await restartWorker();
    say("İptal edildi. Yeni bir dosya bırakabilirsiniz.", "ok");
  } catch (err) {
    say(`İptal sonrası codec yüklenemedi: ${err.message}`, "err");
  }
}

async function load(file) {
  if (!file) return;
  if (!state.ready && !worker) {
    say("Codec henüz hazır değil, bir saniye…", "busy");
  }

  if (file.size > MAX_BYTES) {
    say(
      `Dosya 100 MB sınırını aşıyor (${bytesText(file.size)}). Daha küçük bir görüntü seçin; ` +
      "daha büyük dosyalar için CLI'yi kullanın.",
      "err",
    );
    el.dot.className = "dot err";
    return;
  }

  const wasBusy = state.busy;
  state.run += 1;
  const run = state.run;
  state.file = file;
  state.name = (file.name || "goruntu").replace(/\.[^.]+$/, "");
  state.source = null;
  state.browserRows = [];
  state.lum = null;
  state.after = null;
  resetView();
  resetDownloadButtons();
  startJob();

  try {
    say("Codec hazırlanıyor…", "busy");
    await ensureWorker({ force: wasBusy });
    if (run !== state.run) return;

    const head = new Uint8Array(await file.slice(0, 8).arrayBuffer());
    if (run !== state.run) return;

    if (isLum(head)) {
      const bytes = new Uint8Array(await file.arrayBuffer());
      if (run !== state.run) return;
      await openLum(bytes, file, run);
      return;
    }

    say("Görüntü çözülüyor…", "busy");
    let bitmap;
    try {
      bitmap = await createImageBitmap(file);
    } catch {
      say("Bu dosya bir görüntü olarak açılamadı (bozuk veya desteklenmeyen biçim).", "err");
      return;
    }
    if (run !== state.run) {
      if (bitmap.close) bitmap.close();
      return;
    }

    const maxEdge = Number(el.maxedge.value);
    const naturalWidth = bitmap.width;
    const naturalHeight = bitmap.height;
    const longest = Math.max(naturalWidth, naturalHeight);
    const scale = maxEdge > 0 ? Math.min(1, maxEdge / longest) : 1;
    const width = Math.max(1, Math.round(naturalWidth * scale));
    const height = Math.max(1, Math.round(naturalHeight * scale));

    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(bitmap, 0, 0, width, height);
    if (bitmap.close) bitmap.close();
    const sourcePixels = ctx.getImageData(0, 0, width, height);
    if (run !== state.run) return;

    const scaled = width !== naturalWidth || height !== naturalHeight;
    state.source = {
      image: sourcePixels,
      name: file.name || "yapıştırılan görüntü",
      originalBytes: file.size,
      width,
      height,
      originalWidth: naturalWidth,
      originalHeight: naturalHeight,
      scaled,
      hasAlpha: hasTransparency(sourcePixels),
    };

    el.scaleNote.hidden = !scaled;
    if (scaled) {
      el.scaleNote.textContent =
        `Görüntü ${naturalWidth} × ${naturalHeight}; ölçüm için ${width} × ${height} boyutuna indirildi. ` +
        "wasm'da thread yok ve kayıpsız kodlama megapiksel başına ≈6,5 saniye sürüyor " +
        `(bu kare için yaklaşık ${(width * height / 1e6 * 6.5).toFixed(1)} s). Daha büyük ölçüm için ` +
        "yukarıdan 1024 px ya da “tam boyut” seçin.";
    }

    drawPreviews(sourcePixels, sourcePixels);
    el.capBefore.textContent = `kaynak · ${tr(width)} × ${tr(height)}`;
    el.capAfter.textContent = "LUMEN";
    await measureAll(run);
  } catch (err) {
    if (run === state.run) say(`İşlem başarısız: ${err.message}`, "err");
  } finally {
    endJob(run);
  }
}

async function openLum(bytes, file, run) {
  say(".lum dosyası çözülüyor…", "busy");
  let result;
  try {
    result = await call("decode", { bytes }, [bytes.buffer]);
  } catch (err) {
    if (run === state.run) say(`Bu .lum dosyası okunamadı: ${err.message}`, "err");
    return;
  }
  if (run !== state.run) return;

  const info = result.info;
  state.source = null;
  state.browserRows = [];
  state.lum = {
    bytes: null,
    info,
    rgba: result.rgba,
    width: result.width,
    height: result.height,
    decodeMs: result.decodeMs,
  };
  state.after = { width: result.width, height: result.height, data: result.rgba };

  drawPreviews(state.after, state.after);
  el.capBefore.textContent = "çözülen dosya";
  el.capAfter.textContent = info.lossy ? `LUMEN q${info.quality}` : "LUMEN kayıpsız";

  el.meta.innerHTML =
    `<span><b>${escapeHtml(file.name)}</b></span>` +
    `<span class="chip">${info.width} × ${info.height}</span>` +
    `<span class="chip">${["gri", "gri+alfa", "rgb", "rgba"][info.channels - 1] || info.channels + " kanal"}</span>` +
    `<span class="chip ${info.lossy ? "warn" : "good"}">${info.lossy ? `kayıplı q${info.quality}` : "kayıpsız"}</span>` +
    `<span class="chip">v${info.version}</span>` +
    `<span>dosya <b>${bytesText(file.size)}</b></span>`;

  const raw = info.width * info.height * info.channels;
  document.querySelector("#c-save .k").textContent = "Ham veri";
  const saveVal = document.querySelector("#c-save .v");
  if (!saveVal.querySelector("small")) saveVal.innerHTML = "<span data-v>—</span>";
  setCounter("c-save", raw, (x) => bytesShort(x));
  document.querySelector("#c-save").className = "counter";
  $("c-save-sub").textContent = `${bytesText(raw)} → ${bytesText(file.size)}`;

  const ratioNode = document.querySelector("#c-ratio .v");
  if (!ratioNode.querySelector("small")) ratioNode.innerHTML = "<span data-v>—</span><small>%</small>";
  const pct = raw ? (1 - file.size / raw) * 100 : 0;
  setCounter("c-ratio", pct, (x) => tr(x, 1));
  document.querySelector("#c-ratio").className = `counter ${pct >= 0 ? "accent" : "bad"}`;
  $("c-ratio-sub").textContent = `${tr(raw / file.size, 2)}× ham veriye göre`;

  $("c-psnr").style.visibility = "";
  setCounter("c-psnr", null);          // cancels any in-flight count-up
  $("c-psnr-sub").textContent = "dosya zaten kodlanmış";
  $("c-time").querySelector(".k").textContent = "Çözme";
  setCounter("c-time", result.decodeMs, (x) => tr(x, 1));
  $("c-time-sub").textContent = "tek çekirdek · wasm";

  el.tbody.innerHTML = "";
  el.tableNote.textContent =
    "Bu dosya zaten bir LUMEN container'ı: yeniden kodlamak yerine çözüldü. " +
    "Karşılaştırma tablosu için bir PNG/JPEG bırakın.";
  el.dlPng.disabled = false;
  el.dlLum.disabled = true;
  el.results.classList.add("on");
  say(`Hazır — .lum dosyası ${tr(result.decodeMs, 0)} ms'de çözüldü.`, "ok");
}

async function measureAll(run) {
  const s = state.source;
  if (!s) return;
  const q = state.quality;

  try {
    say("Pikseller codec'e aktarılıyor…", "busy");
    await nextFrame();
    await prepareSource(s);
    if (run !== state.run) return;

    say("LUMEN kodlanıyor…", "busy");
    await nextFrame();
    const m = await call("measure", { quality: q });
    if (run !== state.run) return;

    applyChosen(m);
    drawPreviews(s.image, state.after);
    el.capAfter.textContent = q > 0 ? `LUMEN q${q}` : "LUMEN kayıpsız";
    const onlyLum = { source: s, baseline: s.originalBytes, rows: lumRows(m, s), chosen: m.chosen, quality: q, alpha: s.hasAlpha };
    render(onlyLum);
    say("Tarayıcının kendi kodlayıcıları çalışıyor…", "busy");
    await nextFrame();

    const [png, jpeg, webp] = await Promise.all([
      browserEncode(s.image, "image/png"),
      browserEncode(s.image, "image/jpeg", 0.9),
      browserEncode(s.image, "image/webp", 0.9),
    ]);
    if (run !== state.run) return;

    const rows = [...lumRows(m, s)];
    if (png) rows.push(rowOf("PNG", "tarayıcı", false, png, s.originalBytes, "browser"));
    if (jpeg) rows.push(rowOf("JPEG", "q90", false, jpeg, s.originalBytes, "browser"));
    if (webp) rows.push(rowOf("WebP", "q90", false, webp, s.originalBytes, "browser"));
    state.browserRows = rows.filter((r) => !r.subject);

    // Rebuild the LUMEN rows from the same measurement, then add the browser
    // rows. Numbers do not change between the two renders.
    const chosenPsnr = m.chosen.psnr;
    const summary = isFinite(chosenPsnr)
      ? `${tr(chosenPsnr, 2)} dB`
      : "kayıpsız (∞)";
    render({ source: s, baseline: s.originalBytes, rows, chosen: m.chosen, quality: q, alpha: s.hasAlpha });
    say(
      `Ölçüm tamam — ${bytesShort(m.chosen.bytes.length)}, ${summary}, ${tr(m.chosen.encodeMs, 0)} ms. ` +
      "Sonuç cihazınızda üretildi.",
      "ok",
    );
  } catch (err) {
    if (run === state.run) say(`Ölçüm başarısız: ${err.message}`, "err");
  }
}

async function recomputeLumen() {
  if (!state.source) return;
  const wasBusy = state.busy;
  state.run += 1;
  const run = state.run;
  const s = state.source;
  const q = state.quality;
  startJob();

  try {
    await ensureWorker({ force: wasBusy });
    if (run !== state.run) return;
    await prepareSource(s);
    if (run !== state.run) return;
    say(`LUMEN ${q > 0 ? `q${q}` : "kayıpsız"} kodlanıyor…`, "busy");
    await nextFrame();
    const m = await call("measure", { quality: q });
    if (run !== state.run) return;

    applyChosen(m);
    const rows = [...lumRows(m, s), ...state.browserRows];
    render({ source: s, baseline: s.originalBytes, rows, chosen: m.chosen, quality: q, alpha: s.hasAlpha });
    drawPreviews(s.image, state.after);
    el.capAfter.textContent = q > 0 ? `LUMEN q${q}` : "LUMEN kayıpsız";
    say("Ölçüm güncellendi.", "ok");
  } catch (err) {
    if (run === state.run) say(`Ölçüm başarısız: ${err.message}`, "err");
  } finally {
    endJob(run);
  }
}

function render(ctx) {
  const { source, baseline, rows, chosen, quality, alpha } = ctx;
  const chosenPsnr = chosen.psnr ?? psnrRgb(source.image, chosen);
  const saved = baseline - chosen.bytes.length;
  const pct = baseline ? (saved / baseline) * 100 : 0;

  document.querySelector("#c-save .k").textContent = "Tasarruf";
  const saveNode = document.querySelector("#c-save .v");
  if (!saveNode.querySelector("small")) saveNode.innerHTML = '<span data-v>—</span>';
  const ratioNode = document.querySelector("#c-ratio .v");
  if (!ratioNode.querySelector("small")) ratioNode.innerHTML = '<span data-v>—</span><small>%</small>';
  $("c-psnr").style.visibility = "";
  $("c-time").querySelector(".k").textContent = "Kodlama / çözme";

  animate(document.querySelector("#c-save .v [data-v]"), Math.abs(saved),
    (x) => (saved < 0 ? "+" : "−") + bytesShort(x));
  document.querySelector("#c-save").className = `counter ${saved >= 0 ? "good" : "bad"}`;
  $("c-save-sub").textContent = `${bytesText(baseline)} → ${bytesText(chosen.bytes.length)}`;

  animate(document.querySelector("#c-ratio .v [data-v]"), Math.abs(pct),
    (x) => (pct < 0 ? "+" : "") + tr(x, 1));
  document.querySelector("#c-ratio").className = `counter ${pct >= 0 ? "accent" : "bad"}`;
  $("c-ratio-sub").textContent = `${tr(baseline / chosen.bytes.length, 2)}× orijinal dosyaya göre`;

  const psnrNode = document.querySelector("#c-psnr .v [data-v]");
  if (!isFinite(chosenPsnr)) {
    psnrNode.textContent = "∞";
    $("c-psnr-sub").textContent = "kayıpsız — pikseller birebir aynı";
    document.querySelector("#c-psnr").className = "counter good";
  } else {
    animate(psnrNode, chosenPsnr, (x) => tr(x, 2));
    $("c-psnr-sub").textContent =
      chosenPsnr >= 40 ? "yüksek sadakat" : chosenPsnr >= 34 ? "iyi" : "ölçülü kayıp";
    document.querySelector("#c-psnr").className = "counter accent";
  }

  animate(document.querySelector("#c-time .v [data-v]"), chosen.encodeMs, (x) => tr(x, 0));
  $("c-time-sub").textContent = `çözme ${tr(chosen.decodeMs, 0)} ms · tek çekirdek`;

  const chips = [
    `<span><b>${escapeHtml(source.name)}</b></span>`,
    `<span class="chip">${source.width} × ${source.height}</span>`,
    `<span class="chip">${bytesText(baseline)} orijinal</span>`,
    `<span class="chip">${alpha ? "saydamlık var" : "opak"}</span>`,
    `<span class="chip">${quality > 0 ? `kayıplı q${quality}` : "kayıpsız"}</span>`,
  ];
  if (alpha) chips.push('<span class="chip warn">alfa da kodlanıyor</span>');
  if (source.scaled) {
    chips.push(`<span class="chip warn">${source.originalWidth} × ${source.originalHeight} → ${source.width} × ${source.height}</span>`);
  }
  el.meta.innerHTML = chips.join("");

  const sorted = [...rows].sort((a, b) => a.bytes - b.bytes);
  const smallest = sorted[0];
  el.tbody.innerHTML = sorted.map((r) => {
    const win = r === smallest;
    const delta = baseline ? baseline - r.bytes : 0;
    const deltaText = baseline
      ? (delta === 0 ? "aynı" : `${delta > 0 ? "−" : "+"}${bytesText(Math.abs(delta))}`)
      : "—";
    const psnr = isFinite(r.psnr) ? `${tr(r.psnr, 2)} dB` : "∞";
    const label = r.subject
      ? `<span class="fmt"><b>LUMEN</b><span>${r.kind}</span>${r.note === "wasm" ? '<span class="badge wasm">wasm</span>' : ""}${win ? '<span class="badge win">en küçük</span>' : ""}</span>`
      : `<span class="fmt"><b>${r.label}</b><span>${r.kind}</span>${win ? '<span class="badge win">en küçük</span>' : ""}</span>`;
    return `<tr class="${r.subject ? "subject" : ""}">
      <td>${label}</td>
      <td class="num">${bytesText(r.bytes)}</td>
      <td class="num">${deltaText}</td>
      <td class="num">${psnr}</td>
      <td class="num">${tr(r.meanMs, 1)} ms</td>
    </tr>`;
  }).join("");

  const lumRow = rows.find((r) => r.subject && (quality > 0 ? r.kind === `q${quality}` : r.kind === "kayıpsız"));
  const notes = [
    "PSNR, kaynak pikseller ile çözülen pikseller arasında R, G, B üzerinden hesaplanır; alfa ayrı tutulur.",
    "PNG/JPEG/WebP satırları bu tarayıcının kendi kodlayıcılarıyla, aynı piksellerden üretildi (JPEG ve WebP q90, tipik olarak 4:2:0). LUMEN kayıplı modda 4:4:4 çalışır — renk alt örneklemesi yapmaz.",
    "“Orijinale göre” sütunu kaynak dosyanın baytlarına göredir; dosya zaten PNG ise PNG satırı ondan büyük çıkabilir, çünkü tarayıcı farklı bir kodlayıcı kullanır.",
  ];
  if (lumRow && lumRow.alpha && lumRow.alpha.differing) {
    notes.push(`Bu görüntüde alfa kanalı da kodlandı: ${tr(lumRow.alpha.differing)} pikselde en fazla ${lumRow.alpha.worst} seviye fark.`);
  }
  if (!rows.some((r) => r.label === "WebP")) notes.push("Bu tarayıcı WebP kodlamayı desteklemedi, satır atlandı.");
  el.tableNote.textContent = notes.join(" ");

  el.results.classList.add("on");
}

function drawPreviews(before, after) {
  const b = imageDataOf(before);
  el.before.width = b.width;
  el.before.height = b.height;
  el.before.getContext("2d").putImageData(b, 0, 0);

  const a = imageDataOf(after);
  el.after.width = a.width;
  el.after.height = a.height;
  el.after.getContext("2d").putImageData(a, 0, 0);
  applySplit();
}

function applySplit() {
  const v = Number(el.split.value);
  el.after.style.clipPath = `inset(0 0 0 ${v}%)`;
}

function isLumFile() {
  return state.lum && state.lum.info && !state.source;
}

// ------------------------------------------------------------------- events

el.navToggle.addEventListener("click", () => {
  const open = el.navLinks.classList.toggle("open");
  el.navToggle.setAttribute("aria-expanded", String(open));
  el.navToggle.setAttribute("aria-label", open ? "Menüyü kapat" : "Menüyü aç");
});
for (const link of el.navLinks.querySelectorAll("a")) {
  link.addEventListener("click", () => {
    el.navLinks.classList.remove("open");
    el.navToggle.setAttribute("aria-expanded", "false");
  });
}

el.drop.addEventListener("click", (e) => {
  if (e.target.closest("button")) return;
  el.file.click();
});
el.drop.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    el.file.click();
  }
});
el.pick.addEventListener("click", (e) => {
  e.stopPropagation();
  el.file.click();
});
el.file.addEventListener("change", () => {
  if (el.file.files[0]) load(el.file.files[0]);
});

for (const type of ["dragenter", "dragover"]) {
  el.drop.addEventListener(type, (e) => {
    e.preventDefault();
    el.drop.classList.add("over");
  });
}
for (const type of ["dragleave", "drop"]) {
  el.drop.addEventListener(type, () => el.drop.classList.remove("over"));
}
el.drop.addEventListener("drop", (e) => {
  e.preventDefault();
  const file = e.dataTransfer?.files?.[0];
  if (file) load(file);
});

el.sample.addEventListener("click", async (e) => {
  e.stopPropagation();
  say("Örnek görüntü getiriliyor…", "busy");
  try {
    const res = await fetch("docs/assets/formats/png.png");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    await load(new File([blob], "ornek-kodak-kirpimi.png", { type: "image/png" }));
  } catch (err) {
    say(`Örnek yüklenemedi (${err.message}). Dosyayı elle bırakabilirsiniz.`, "err");
  }
});

el.segs.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-q]");
  if (!btn) return;
  state.quality = Number(btn.dataset.q);
  for (const b of el.segs.querySelectorAll("button")) {
    b.setAttribute("aria-pressed", String(b === btn));
  }
  recomputeLumen();
});

el.maxedge.addEventListener("change", () => {
  if (state.file && !isLumFile()) load(state.file);
});

el.split.addEventListener("input", applySplit);
el.cancel.addEventListener("click", cancelJob);

el.dlLum.addEventListener("click", () => {
  if (!state.lum?.bytes) return;
  download(new Blob([state.lum.bytes], { type: "image/lum" }), `${state.name}.lum`);
});

el.dlPng.addEventListener("click", async () => {
  if (!state.after) return;
  const canvas = document.createElement("canvas");
  canvas.width = state.after.width;
  canvas.height = state.after.height;
  canvas.getContext("2d").putImageData(imageDataOf(state.after), 0, 0);
  canvas.toBlob((blob) => blob && download(blob, `${state.name}.png`), "image/png");
});

el.copy.addEventListener("click", async () => {
  const snippet = `import { Lumen } from "./web/lum.js";
await Lumen.init();
const image = Lumen.decode(await (await fetch("foto.lum")).arrayBuffer());
canvas.getContext("2d").putImageData(Lumen.toImageData(image), 0, 0);`;
  try {
    await navigator.clipboard.writeText(snippet);
    el.copy.textContent = "Kopyalandı";
    setTimeout(() => (el.copy.textContent = "JS örneğini kopyala"), 1600);
  } catch {
    say("Panoya kopyalama tarayıcı tarafından engellendi.", "err");
  }
});

function download(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

// --------------------------------------------------------------------- boot

(async function boot() {
  for (const b of el.segs.querySelectorAll("button")) {
    b.setAttribute("aria-pressed", String(Number(b.dataset.q) === state.quality));
  }
  try {
    if (location.protocol === "file:") {
      throw new Error("dosya:// protokolü wasm yükleyemez; sayfayı HTTP üzerinden açın");
    }
    const info = await restartWorker();
    say(
      `Codec hazır (${tr(info.ms, 0)} ms · 253 KiB wasm). Bir görüntü bırakın ya da örneği deneyin.`,
      "ok",
    );
  } catch (err) {
    say(
      `Codec yüklenemedi: ${err.message}. Sayfayı bir HTTP sunucusundan açın — ` +
      "`python3 -m http.server` — çünkü wasm dosyası fetch ile alınır.",
      "err",
    );
  }

  if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {
    navigator.serviceWorker.register("./sw.js").catch(() => undefined);
  }
})();

// Expose a minimal hook so the site can be checked headlessly without a browser
// driver: ?selftest runs one real encode/decode round trip and records numbers.
window.__lumenSite = {
  state,
  Lumen,
  el,
  async selftest() {
    for (let i = 0; i < 240 && !state.ready; i += 1) {
      await new Promise((r) => setTimeout(r, 250));
    }
    const res = await fetch("docs/assets/formats/png.png");
    const blob = await res.blob();
    const file = new File([blob], "selftest.png", { type: "image/png" });
    await load(file);

    const lossless = state.lum;
    const psnr = state.source && lossless
      ? (lossless.psnr ?? psnrRgb(state.source.image, lossless))
      : null;
    const raster = state.source
      ? {
          name: state.source.name,
          width: state.source.width,
          height: state.source.height,
          natural: [state.source.originalWidth, state.source.originalHeight],
          alpha: state.source.hasAlpha,
          bytes: state.source.originalBytes,
          lumBytes: lossless?.bytes?.length ?? null,
          psnrDb: psnr === null ? null : isFinite(psnr) ? Number(psnr.toFixed(3)) : "inf",
          encodeMs: lossless ? Number(lossless.encodeMs.toFixed(2)) : null,
          decodeMs: lossless ? Number(lossless.decodeMs.toFixed(2)) : null,
          rows: (el.tbody.innerText || "").trim().slice(0, 800),
          tableNote: el.tableNote.textContent,
        }
      : null;

    // Round-trip the produced container back through the reader, the way a
    // visitor would: the file path must work, not just the encode call.
    const roundTrip = lossless
      ? await load(new File([lossless.bytes], "selftest.lum", { type: "image/lum" }))
        .then(() => ({
          width: state.lum.info.width,
          height: state.lum.info.height,
          lossy: state.lum.info.lossy,
          quality: state.lum.info.quality,
        }))
        .catch((err) => ({ error: String(err) }))
      : null;

    // The counters count up over 520 ms; let them land before reading the DOM.
    await new Promise((r) => setTimeout(r, 700));

    return {
      ready: state.ready,
      quality: state.quality,
      raster,
      roundTrip,
      lumView: {
        counters: (document.querySelector(".counters").innerText || "").trim(),
        meta: (el.meta.innerText || "").trim(),
      },
    };
  },
};

if (new URLSearchParams(location.search).has("selftest")) {
  window.addEventListener("load", () => {
    setTimeout(() => {
      window.__lumenSite.selftest().then((r) => {
        const pre = document.createElement("pre");
        pre.id = "selftest-result";
        pre.textContent = JSON.stringify(r);
        document.body.appendChild(pre);
      }).catch((err) => {
        const pre = document.createElement("pre");
        pre.id = "selftest-result";
        pre.textContent = JSON.stringify({ error: String(err) });
        document.body.appendChild(pre);
      });
    }, 50);
  });
}
