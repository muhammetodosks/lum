// LUMEN in the browser — a ~290 KiB WebAssembly build of the same codec the
// CLI uses, plus the glue that turns its output into a canvas.
//
//   import { Lumen } from "./lum.js";
//   await Lumen.init();
//   const image = Lumen.decode(await (await fetch("photo.lum")).arrayBuffer());
//   document.body.append(Lumen.toCanvas(image));
//
// Nothing here re-implements the format: `lum_wasm_decode` and
// `lum_wasm_encode` in rust/lum_wasm.rs call `lum_image` / `lum_lossy`
// directly, so a file decoded in a page is bit-identical to `lum decode`.
//
// Memory is shared with the module, so every buffer returned by the codec is
// copied out and freed before the next call.

const MIME = "image/lum";
const EXTENSION = "lum";

let api = null;         // the wasm exports
let memory = null;      // WebAssembly.Memory
let scratch = 0;        // 24 bytes of wasm heap for info/out_len
let loading = null;     // the in-flight init() promise

class LumenError extends Error {
  constructor(code, message) {
    super(message || `LUMEN decode failed (code ${code})`);
    this.name = "LumenError";
    this.code = code;
  }
}

const ERROR_TEXT = {
  [-100]: "not a LUMEN file (bad magic, expected LUMA\\r\\n\\x1A\\n)",
  [-101]: "unsupported LUMEN container version",
  [-102]: "truncated LUMEN stream",
  [-103]: "encoder rejected the pixel buffer",
  [-104]: "unsupported channel count",
};

function fail(code) {
  return new LumenError(code, ERROR_TEXT[code]);
}

function view(Type, ptr, length) {
  // Always rebuilt: a wasm allocation may have grown and detached the buffer.
  return new Type(memory.buffer, ptr, length);
}

function copyIn(bytes) {
  const len = bytes.byteLength;
  const ptr = api.lum_alloc(len);
  if (!ptr) throw new LumenError(0, "out of memory");
  view(Uint8Array, ptr, len).set(bytes);
  return { ptr, len };
}

function ensureScratch() {
  if (!scratch) scratch = api.lum_alloc(64);
  return scratch;
}

// ---------------------------------------------------------------- the codec

async function init(url) {
  if (api) return Lumen;
  if (loading) return loading;
  const target = url || (typeof document !== "undefined" ? new URL("lum.wasm", document.baseURI) : "./lum.wasm");
  const isBytes = target instanceof ArrayBuffer ||
    (typeof ArrayBuffer !== "undefined" && ArrayBuffer.isView(target));
  loading = (async () => {
    let instance;
    if (isBytes) {
      const bytes = target instanceof ArrayBuffer
        ? target
        : target.buffer.slice(target.byteOffset, target.byteOffset + target.byteLength);
      ({ instance } = await WebAssembly.instantiate(bytes, {}));
    } else if (typeof fetch === "function" && typeof WebAssembly.instantiateStreaming === "function") {
      try {
        const response = await fetch(target);
        if (!response.ok) throw new Error(`HTTP ${response.status} for ${target}`);
        ({ instance } = await WebAssembly.instantiateStreaming(response, {}));
      } catch (err) {
        // A server that serves .wasm as text/plain breaks instantiateStreaming.
        const response = await fetch(target);
        if (!response.ok) throw new Error(`HTTP ${response.status} for ${target}`);
        const bytes = await response.arrayBuffer();
        ({ instance } = await WebAssembly.instantiate(bytes, {}));
      }
    } else if (typeof fetch === "function") {
      const bytes = await (await fetch(target)).arrayBuffer();
      ({ instance } = await WebAssembly.instantiate(bytes, {}));
    } else {
      throw new LumenError(0, "pass the .wasm bytes or a URL to Lumen.init()");
    }
    api = instance.exports;
    memory = api.memory;
    if (!memory) throw new LumenError(0, "the module exports no memory");
    ensureScratch();
    return Lumen;
  })();
  return loading;
}

/** Container metadata without decoding the pixels. */
function inspect(bytes) {
  const buf = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const { ptr, len } = copyIn(buf);
  const meta = ensureScratch();
  let rc;
  try {
    rc = api.lum_wasm_info(ptr, len, meta);
  } finally {
    api.lum_alloc_free(ptr, len);
  }
  if (rc < 0) throw fail(rc);
  const m = view(Int32Array, meta, 6).slice();
  return {
    width: m[0],
    height: m[1],
    channels: m[2],
    lossy: m[3] === 1,
    quality: m[4],
    version: m[5],
    mime: MIME,
    extension: EXTENSION,
  };
}

/**
 * Decode a `.lum` file.
 * @returns {{width:number,height:number,channels:number,lossy:boolean,data:Uint8Array}}
 */
function decode(bytes) {
  if (!api) throw new LumenError(0, "call Lumen.init() first");
  const buf = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const { ptr, len } = copyIn(buf);
  const info = ensureScratch();
  let out = 0;
  try {
    out = api.lum_wasm_decode(ptr, len, info);
  } finally {
    api.lum_alloc_free(ptr, len);
  }
  const [status, width, height, channels, kind] = view(Int32Array, info, 5).slice();
  if (!out) throw fail(status);
  const size = width * height * channels;
  const data = view(Uint8Array, out, size).slice();
  api.lum_free_buf(out, size);
  return { width, height, channels, lossy: kind === 1, data };
}

/**
 * Encode raw pixels (or an ImageData / canvas) into a `.lum`.
 * quality <= 0 gives the lossless container, 1..100 the lossy one.
 */
function encode(source, options = {}) {
  if (!api) throw new LumenError(0, "call Lumen.init() first");
  const { width, height, channels, data } = normalise(source);
  const quality = Math.max(0, Math.round(options.quality ?? 0));
  const { ptr, len } = copyIn(data);
  const outLen = ensureScratch() + 24;
  let out = 0;
  try {
    out = api.lum_wasm_encode(ptr, len, width, height, channels, quality, outLen);
  } finally {
    api.lum_alloc_free(ptr, len);
  }
  if (!out) throw fail(api.lum_wasm_last_error());
  const n = view(Uint32Array, outLen, 1)[0];
  const bytes = view(Uint8Array, out, n).slice();
  api.lum_free_buf(out, n);
  return bytes;
}

function normalise(source) {
  if (typeof ImageData !== "undefined" && source instanceof ImageData) {
    return {
      width: source.width,
      height: source.height,
      channels: 4,
      data: new Uint8Array(source.data.buffer, source.data.byteOffset, source.data.byteLength),
    };
  }
  if (typeof HTMLCanvasElement !== "undefined" && source instanceof HTMLCanvasElement) {
    const ctx = source.getContext("2d", { willReadFrequently: true });
    return normalise(ctx.getImageData(0, 0, source.width, source.height));
  }
  if (source && source.data && source.width && source.height) {
    const channels = source.channels || 4;
    return { width: source.width, height: source.height, channels, data: source.data };
  }
  throw new LumenError(0, "encode() expects ImageData, a canvas or {width,height,data}");
}

// -------------------------------------------------------------- presentation

/** Expand 1 or 3 channels to RGBA so the browser can draw it. */
function toRGBA(image) {
  if (image.channels === 4) return new Uint8ClampedArray(image.data);
  const { width, height, channels, data } = image;
  const out = new Uint8ClampedArray(width * height * 4);
  for (let i = 0, j = 0; i < width * height; i++, j += channels) {
    if (channels === 1) {
      const v = data[j];
      out[i * 4] = v;
      out[i * 4 + 1] = v;
      out[i * 4 + 2] = v;
    } else {
      out[i * 4] = data[j];
      out[i * 4 + 1] = data[j + 1];
      out[i * 4 + 2] = data[j + 2];
    }
    out[i * 4 + 3] = channels === 2 ? data[j + 1] : 255;
  }
  return out;
}

function toImageData(image) {
  return new ImageData(toRGBA(image), image.width, image.height);
}

function toCanvas(image) {
  const canvas = document.createElement("canvas");
  canvas.width = image.width;
  canvas.height = image.height;
  canvas.getContext("2d").putImageData(toImageData(image), 0, 0);
  return canvas;
}

function toBlob(image, type = "image/png", quality) {
  return new Promise((resolve, reject) => {
    toCanvas(image).toBlob((blob) => (blob ? resolve(blob) : reject(new LumenError(0, "toBlob failed"))), type, quality);
  });
}

function toURL(image, type = "image/png") {
  const canvas = toCanvas(image);
  return new Promise((resolve) => {
    canvas.toBlob((blob) => resolve(URL.createObjectURL(blob)), type);
  });
}

// ------------------------------------------------------------------- element

/**
 * <lum-img src="photo.lum" alt="..."></lum-img>
 *
 * Decodes into a shadow-DOM canvas and fires `lumload` with the container
 * metadata, or `lumerror` when the bytes are not a LUMEN file.
 */
const LumImageElement = typeof HTMLElement === "undefined" ? null : class extends HTMLElement {
  static get observedAttributes() {
    return ["src"];
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
  }

  connectedCallback() {
    if (this._lastSrc !== this.getAttribute("src")) this.render();
  }

  attributeChangedCallback(name, oldValue, newValue) {
    if (name === "src" && oldValue !== newValue) this.render();
  }

  async render() {
    const src = this.getAttribute("src");
    this._lastSrc = src;
    if (!src) return;
    this.shadowRoot.textContent = "";
    try {
      await init();
      const response = await fetch(src);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const bytes = new Uint8Array(await response.arrayBuffer());
      const head = inspect(bytes);
      if (head.width <= 0) throw new Error("not an image container");
      const image = decode(bytes);
      const canvas = toCanvas(image);
      canvas.style.cssText = "display:block;max-width:100%;height:auto";
      const alt = this.getAttribute("alt");
      if (alt) canvas.setAttribute("aria-label", alt);
      this.shadowRoot.append(canvas);
      this.dispatchEvent(new CustomEvent("lumload", { detail: { ...head, bytes: bytes.length } }));
    } catch (err) {
      const note = document.createElement("span");
      note.textContent = this.getAttribute("alt") || `LUMEN: ${err.message}`;
      note.style.cssText = "display:inline-block;padding:.5em .75em;font:13px system-ui;opacity:.7";
      this.shadowRoot.append(note);
      this.dispatchEvent(new CustomEvent("lumerror", { detail: err }));
    }
  }
};

if (LumImageElement && typeof customElements !== "undefined" && !customElements.get("lum-img")) {
  customElements.define("lum-img", LumImageElement);
}

export const Lumen = {
  MIME,
  EXTENSION,
  Error: LumenError,
  init,
  inspect,
  decode,
  encode,
  toRGBA,
  toImageData,
  toCanvas,
  toBlob,
  toURL,
  /** Bytes reserved by the wasm heap (it grows and never shrinks). */
  memoryBytes() {
    return memory ? memory.buffer.byteLength : 0;
  },
  get ready() {
    return api !== null;
  },
};

if (typeof globalThis !== "undefined" && !globalThis.Lumen) globalThis.Lumen = Lumen;
export default Lumen;
