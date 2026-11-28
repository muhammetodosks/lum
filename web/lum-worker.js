// LUMEN codec worker.
//
// The wasm codec is single-threaded and lossless encoding costs roughly
// 6.5 s/MP, so doing it on the page's main thread froze the interface on
// ordinary phone photos. This worker owns its own copy of the module: the page
// keeps animating, the visitor can cancel, and the only things that cross the
// boundary are pixel buffers and the resulting containers.
//
// Protocol
//   { id, type: "ping" }
//   { id, type: "prepare", payload: { width, height, hasAlpha, rgba } }
//       `rgba` is the source raster. The worker derives the interleaved RGB
//       (or RGBA) samples the codec wants, so the page never loops over a
//       12-megapixel buffer on its main thread. The buffer is transferred and
//       cached until the next prepare.
//   { id, type: "measure", payload: { quality } }
//   { id, type: "encode",  payload: { quality } }
//   { id, type: "decode",  payload: { bytes } }
// Reply
//   { id, ok: true, result }  |  { id, ok: false, error, code }

import { Lumen } from "./lum.js";

let loading = Lumen.init();
let pixels = null;
let source = null;

function ready() {
  return loading;
}

function psnrRgb(a, b) {
  const n = Math.min(a.length, b.length);
  let sum = 0;
  let count = 0;
  for (let i = 0; i < n; i += 4) {
    for (let c = 0; c < 3; c += 1) {
      const d = a[i + c] - b[i + c];
      sum += d * d;
      count += 1;
    }
  }
  if (!count) return 0;
  const mse = sum / count;
  return mse === 0 ? Infinity : 10 * Math.log10(65025 / mse);
}

function alphaError(a, b) {
  let worst = 0;
  let differing = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 3; i < n; i += 4) {
    const d = Math.abs(a[i] - b[i]);
    if (d) {
      differing += 1;
      if (d > worst) worst = d;
    }
  }
  return { worst, differing };
}

function typed(value, Type) {
  if (!value) return null;
  if (value instanceof Type) return value;
  return new Type(value);
}

function prepare(payload) {
  const rgba = typed(payload.rgba, Uint8ClampedArray);
  const { width, height } = payload;
  const n = width * height;
  source = rgba;
  if (payload.hasAlpha) {
    pixels = { width, height, channels: 4, data: new Uint8Array(rgba) };
  } else {
    const data = new Uint8Array(n * 3);
    for (let i = 0, j = 0, k = 0; i < n; i += 1, j += 4, k += 3) {
      data[k] = rgba[j];
      data[k + 1] = rgba[j + 1];
      data[k + 2] = rgba[j + 2];
    }
    pixels = { width, height, channels: 3, data };
  }
  return {
    width: pixels.width,
    height: pixels.height,
    channels: pixels.channels,
    sourceBytes: source ? source.length : 0,
  };
}

function score(bytes, encodeMs, quality) {
  const decodeT0 = performance.now();
  const image = Lumen.decode(bytes);
  const decodeMs = performance.now() - decodeT0;
  const rgba = Lumen.toRGBA(image);
  return {
    bytes,
    encodeMs,
    decodeMs,
    quality,
    psnr: source ? psnrRgb(source, rgba) : 0,
    alpha: source ? alphaError(source, rgba) : { worst: 0, differing: 0 },
    rgba,
    width: image.width,
    height: image.height,
    info: Lumen.inspect(bytes),
  };
}

function encodeOne(quality) {
  if (!pixels) throw new Error("no pixels prepared");
  const t0 = performance.now();
  const bytes = Lumen.encode(pixels, { quality });
  return score(bytes, performance.now() - t0, quality);
}

function measure(payload) {
  if (!pixels) throw new Error("no pixels prepared");
  const lossless = encodeOne(0);
  const lossy = payload && payload.quality > 0 ? encodeOne(payload.quality) : null;
  const chosen = lossy || lossless;
  return {
    lossless,
    lossy,
    chosen: {
      bytes: chosen.bytes,
      rgba: chosen.rgba,
      width: chosen.width,
      height: chosen.height,
      quality: chosen.quality,
      info: chosen.info,
      psnr: chosen.psnr,
      encodeMs: chosen.encodeMs,
      decodeMs: chosen.decodeMs,
    },
  };
}

function decodeContainer(payload) {
  const bytes = typed(payload.bytes, Uint8Array);
  const info = Lumen.inspect(bytes);
  const t0 = performance.now();
  const image = Lumen.decode(bytes);
  const decodeMs = performance.now() - t0;
  return {
    info,
    decodeMs,
    rgba: Lumen.toRGBA(image),
    width: image.width,
    height: image.height,
  };
}

function strip(result) {
  if (!result) return null;
  const { rgba, ...rest } = result;
  return rest;
}

function transferables(result) {
  const buffers = [
    result?.chosen?.bytes?.buffer,
    result?.chosen?.rgba?.buffer,
    result?.lossless?.bytes?.buffer,
    result?.lossy?.bytes?.buffer,
    result?.rgba?.buffer,
    result?.bytes?.buffer,
  ].filter(Boolean);
  return [...new Set(buffers)];
}

self.addEventListener("message", async (event) => {
  const { id, type, payload } = event.data || {};
  try {
    await ready();
    let result;
    if (type === "ping") {
      result = { ready: true, wasmBytes: Lumen.memoryBytes() };
    } else if (type === "prepare") {
      result = prepare(payload);
    } else if (type === "measure") {
      result = measure(payload);
    } else if (type === "encode") {
      result = encodeOne(payload.quality);
    } else if (type === "decode") {
      result = decodeContainer(payload);
    } else {
      throw new Error(`unknown worker request: ${type}`);
    }

    // The scoring copies are not needed twice: the page only draws `chosen`.
    if (type === "measure" && result) {
      result = {
        ...result,
        lossless: strip(result.lossless),
        lossy: strip(result.lossy),
      };
    }

    // eslint-disable-next-line no-undef
    self.postMessage({ id, ok: true, result }, transferables(result));
  } catch (err) {
    self.postMessage({
      id,
      ok: false,
      error: err && err.message ? err.message : String(err),
      code: err && err.code ? err.code : 0,
    });
  }
});
