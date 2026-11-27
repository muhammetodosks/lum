# LUMEN in the browser

`web/` holds a WebAssembly build of the reference codec and the small ES module
that turns its output into a canvas. No decoder is re-implemented here:
`rust/lum_wasm.rs` calls the same `lum_image` / `lum_lossy` functions the
native `.so` exposes, so a `.lum` file decoded in a page is **bit-identical**
to one decoded by `lum decode`, and a file encoded in a page is byte-identical
to one encoded by the CLI.

| File | What it is |
|------|-----------|
| `lum.wasm` | the codec, ≈253 KiB, built by `scripts/build_wasm.sh` |
| `lum.js` | the ES module: `init` / `decode` / `encode` / `inspect` / canvas helpers + `<lum-img>` |
| `index.html` | a self-contained demo: drop a `.lum` to decode, drop a PNG/JPEG to encode |
| `package.json` | `{"type": "module"}`, so `lum.js` imports as ESM without a bundler |

`lum.wasm` is committed on purpose: the demo then works from a plain static
host (GitHub Pages included) with no toolchain. Rebuild it after changing
anything under `rust/`:

```bash
./scripts/build_wasm.sh          # rustc --target wasm32-unknown-unknown -> web/lum.wasm
```

The repository root also has a product page (`index.html`, served by GitHub
Pages) that imports this module and measures the codec on the visitor's own
file: saved bytes, PSNR in dB and encode/decode milliseconds, next to PNG, JPEG
and WebP produced by the visitor's browser. `scripts/site_check.py` drives that
page in headless Chrome over CDP and fails if the numbers stop being real.

## Quick start

```html
<script type="module">
  import { Lumen } from "./lum.js";

  await Lumen.init();                        // fetches ./lum.wasm next to this module

  const image = Lumen.decode(await (await fetch("photo.lum")).arrayBuffer());
  document.querySelector("canvas")
          .getContext("2d")
          .putImageData(Lumen.toImageData(image), 0, 0);
</script>
```

The other direction, from anything the browser can draw:

```js
const canvas = document.querySelector("canvas");
const bytes = Lumen.encode(canvas.getContext("2d").getImageData(0, 0, w, h), {
  quality: 90,                 // 0 = lossless container, 1..100 = lossy (4:4:4)
});
const blob = new Blob([bytes], { type: "image/lum" });
```

Or the element, which needs no JavaScript at the call site:

```html
<script type="module" src="./lum.js"></script>
<lum-img src="photo.lum" alt="a photo"></lum-img>
```

`<lum-img>` decodes into a shadow-DOM canvas and fires `lumload` with the
container metadata; if the bytes turn out not to be a LUMEN file it fires
`lumerror` and shows the `alt` text.

## API

| Call | Returns |
|------|---------|
| `await Lumen.init(url?)` | the module itself; `url` may be a URL, an `ArrayBuffer` or a `Uint8Array` (Node) |
| `Lumen.inspect(bytes)` | `{width, height, channels, lossy, quality, version, mime, extension}` — reads the header, decodes nothing |
| `Lumen.decode(bytes)` | `{width, height, channels, lossy, data: Uint8Array}` — interleaved 8-bit samples |
| `Lumen.encode(source, {quality})` | `Uint8Array` with the container; `source` is `ImageData`, a canvas, or `{width, height, channels, data}` |
| `Lumen.toImageData(image)` | `ImageData` (1 and 3 channels expanded to RGBA) |
| `Lumen.toCanvas(image)` | a new `HTMLCanvasElement` |
| `Lumen.toBlob(image, type?, quality?)` | `Promise<Blob>` (PNG by default) |
| `Lumen.toURL(image, type?)` | `Promise<string>` object URL |
| `Lumen.memoryBytes()` | bytes reserved by the wasm heap |
| `Lumen.ready` | `true` once `init()` has resolved |
| `Lumen.MIME` / `Lumen.EXTENSION` | `"image/lum"` / `"lum"` |

Channels are `1` (grey), `2` (grey+alpha), `3` (RGB) or `4` (RGBA). Samples
wider than 8 bits are scaled down on decode (`>> (bit_depth - 8)`), which is
what the browser can draw; the file itself is untouched.

Errors are `Lumen.Error` instances with a `code`: `-100` bad magic, `-101`
unsupported container version, `-102` truncated stream, `-103` the encoder
rejected the pixel buffer, `-104` unsupported channel count. Container-level
failures keep the codec's own negative codes (CRC, entropy, header), so a
corrupt file is reported the same way the CLI reports it.

## Serving

Browsers compile `.wasm` with `WebAssembly.instantiateStreaming` only when the
server answers `Content-Type: application/wasm`; `lum.js` falls back to a
buffered `instantiate()` when it does not, so any static server works — but it
is worth configuring both types anyway.

```nginx
# nginx
types { image/lum lum; application/wasm wasm; }
```

```apache
# Apache
AddType image/lum .lum
AddType application/wasm .wasm
```

No `Cross-Origin-Opener-Policy` / `Cross-Origin-Embedder-Policy` headers are
needed: the module uses no shared memory and no `SharedArrayBuffer`.

Locally:

```bash
cd web && python3 -m http.server 8080     # then open http://localhost:8080/
```

## What it costs

The whole codec is compiled in (the demo also encodes), and `wasm32` has no
threads, so the parallel tile loops in `rust/lum_image.rs` take their serial
branch. The bytes produced are identical either way — only the wall time
changes. Measured in Node 22 on a 220×220 RGB photo crop (0.048 MP, one core):

| | wasm | note |
|---|---|---|
| lossless decode | 25 ms | the same decoder as the CLI |
| lossless encode | 415 ms | evaluates its candidate list serially; the native build spreads it over cores |
| lossy q90 encode | 10 ms | single-pass, so it is close to native |
| heap after a 0.048 MP round trip | 7.8 MiB | wasm memory grows and never shrinks |

So decoding is the fast path — which is the point of shipping a browser
decoder — and lossless *encoding* in a page costs about 6.5 seconds per
megapixel on one core (measured: 0.19 MP → 1.4 s, 0.77 MP → 5.1 s, 1.74 MP →
11.2 s). The demo defaults to quality 90 for dropped rasters; the product page
measures at a 512 px longest edge by default for the same reason.

## Verification

`tests/test_wasm.mjs` is not a smoke test. It encodes fixtures with the Python
reference, then asserts that the wasm build

- decodes them to pixels bit-identical to `lum decode` (lossless and lossy),
- re-encodes the same pixels to bytes identical to `lum encode`,
- writes a container that Python decodes back to the same pixels,
- downconverts a 16-bit greyscale file exactly,
- and rejects a non-LUMEN buffer (`-100`) and a truncated one (a negative code)
  instead of returning garbage.

```bash
./scripts/build_wasm.sh && node tests/test_wasm.mjs    # 27/27 checks
```

`LUMEN_BUILD_TESTS=1 ./scripts/run_tests.sh` runs it with the rest of the
suites when `node` and `rustc` are present.
