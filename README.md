# PYRA

**PYRA** is a lossless image format and codec written in **pure Python 3.12+ with zero dependencies** — standard library only. No Pillow, no NumPy, no C extensions.

The magic value is `b"PYRA\r\n\x1A\n"` — a recognizable 8-byte signature followed by control characters (`\r\n` line ending, `\x1A` EOF char, trailing `\n`). This borrows PNG's corruption-protection scheme: the suffix survives text-mode line-ending translation and pipes through naive byte-damaging tools as clear text garbage rather than silently passing.

## Design Goals

- **Lossless by construction** — the encoder runs the full inverse pipeline on the encoder side too, so `decoded == original` is guaranteed, not tested into existence.
- **Genuinely working features** — no stubs, no TODOs, no fake entries.
- **Bounded memory** — every limit is named, enforced and documented.
- **Deterministic** — the same inputs produce byte-identical output; tie-breaking is by lowest method ID.
- **Security-safe decoder** — the decoder treats every untrusted vector as adversarial.

## Feature Matrix

| Feature | Status | Proof |
|---------|--------|-------|
| Magic `PYRA\r\n\x1A\n` | ✅ | Byte-exact, 8B, text-translation-safe control chars |
| Lossless pipeline | ✅ | `test_lossless.py` 246 checks |
| Deterministic encoder | ✅ | Same input → identical bytes |
| Unsafe-input fuzz | ✅ | 200 RLE / 100 LZ / 80 pipeline / 150 corruption trials, 0 leaks |
| CRC32 integrity | ✅ | verify command + decoder |
| decode_region (ROI) | ✅ | Only tiles touching the region are decoded |
| Multi-predictor engine | ✅ | 7 reversible predictors |
| Per-file best method select | ✅ | Encodes all 3 methods per tile, keeps cheapest globally |
| Auto colour transform (v2) | ✅ | YCoCg vs none, measured with the real coder |
| Auto row filter (v2) | ✅ | One predictor per row, 3 bits/row in the tile body |
| Narrow residual packing (v3) | ✅ | Residuals packed with exactly the bits the alphabet needs |
| Parallel native codec (v3) | ✅ | Whole-image encode/decode in Rust, one thread per tile |
| Backward compatibility | ✅ | v1 and v2 containers still decode |
| SecurityError / CorruptedFileError | ✅ | Explicit typed errors |
| 16-bit depth | ✅ | Full-pixel domain covered |
| Animation container | ✅ | Frame count + fps header |
| CLI | ✅ | encode/decode/inspect/verify/benchmark |

## The Pipeline

```
Pixel
  │
  ├─▶ 1. Color Transform     YCoCg (reversible) or none — chosen automatically
  ├─▶ 2. Tile                images split into tile_size × tile_size blocks
  ├─▶ 3. Predictor           residuals: NONE/LEFT/UP/AVERAGE/PAETH/GRADIENT/MEDIAN
  │                          one predictor for the whole image, or one per row
  ├─▶ 4. Residual            exact integer residuals, modular alphabet (res+pred) % mod
  ├─▶ 5. RLE                 escape-coded run-length over residual bytes
  ├─▶ 6. LZ                  LZ77 with 3-byte hash buckets, self-overlap allowed
  └─▶ 7. Entropy             adaptive range coder (Fenwick-tree adaptive model)
```

Stages 1 and 3 are **planned** before the encoding starts: the encoder
measures the alternatives with the real range coder and keeps the cheapest
one. See [Automatic planning](#automatic-planning-v2).

### Canonicalization detail (RGB/RGBA)

Reversible YCoCg produces exact integers that can be negative or exceed the sample domain. Every transformed channel (including passthrough alpha) is canonicalized with a uniform offset into a modular alphabet:

```python
mod = 1 << (bit_depth + 3)   # RGB/RGBA
mod = 1 << (bit_depth)       # GRAY
offset = mod >> 1            # RGB/RGBA only
```

Decode side: subtract the offset, then invert the color transform. Bit-exact roundtrip verified for RGB/RGBA/GRAY × 8/16-bit × all 7 predictors.

With `transform = none` the pixels are predicted as they are: the modular
alphabet shrinks to `1 << bit_depth` (instead of `1 << (bit_depth + 3)` for
YCoCg) and no offset is needed. A narrower alphabet means a denser symbol
distribution and better entropy coding — for most photographs the plain
transform wins, which is why the encoder now measures both.

## Automatic planning (v2)

Before encoding, the codec picks three things:

| Choice | Options | How it is decided |
|--------|---------|-------------------|
| `transform` | YCoCg, none | measured with the real range coder on a sample of rows |
| `filter_mode` | global, adaptive | same measurement |
| `pred_type` | 7 predictors | same measurement (global mode only) |

The measurement is deliberately **not** an entropy estimate. The ideal code
length of the residual histogram is biased towards the wider YCoCg alphabet
and picks the wrong transform (measured: 1–6 % off on real photos, and it
chose the worse predictor). Instead the planner encodes the sample residuals
with the actual range coder and compares the resulting byte counts.

The sample is 8 rows spread across the whole image
(`idx[i] = min(h-1, i*h/8)`) and each sampled row is predicted against its
**real** previous row, so the "up" predictor sees the context it would see
during the real encode. The decision is therefore deterministic, and the
native (Rust) and the pure-Python planner always agree — the container bytes
are identical in both modes.

**Adaptive row filters.** In adaptive mode every row chooses its own
predictor from the same seven, scored by the modular L1 distance
`min(r, mod - r)` (ties go to the earlier candidate). The selection is
stored per tile as 3 bits per row, in front of the compressed stream:

```
tile body (adaptive) = [method byte (MIX only)][row filter table][stream]
```

## Compression ratio (real photos, 640 px, 8-bit RGB)

| Image | raw | PYRA v1 | **PYRA v3** | PNG (zlib) | v3 vs PNG |
|-------|-----|---------|-------------|------------|----------|
| Clouds | 691 200 | 226 227 (3.06x) | **210 553 (3.28x)** | 238 846 (2.89x) | −11.8 % |
| Monument Valley | 687 360 | 178 485 (3.85x) | **153 140 (4.49x)** | 142 754 (4.81x) | +7.3 % |
| Fuji-san | 689 280 | 219 158 (3.15x) | **204 653 (3.37x)** | 215 432 (3.20x) | −5.0 % |
| Rainbow | 817 920 | 281 869 (2.90x) | **244 797 (3.34x)** | 240 207 (3.41x) | +1.9 % |
| Numbat | 691 200 | 109 295 (6.32x) | **96 415 (7.17x)** | 91 272 (7.57x) | +5.6 % |
| **total** | 3 576 960 | 1 015 034 (3.52x) | **909 558 (3.93x)** | 928 511 (3.85x) | **−2.0 %** |

v3 is **10 % smaller than v1** and, on this set, slightly *better* than PNG —
while staying dependency-free and fully deterministic. Every byte is still
lossless: the decoded pixels are identical to the input (checked on every
measurement).

Where the v3 gain comes from: v2's automatic planning (colour transform +
row filter) plus the v3 residual packing, which stores a residual in exactly
the number of bits its alphabet needs (`((mod-1).bit_length()+7)//8` bytes
instead of rounding the alphabet up to a power of two). For an 8-bit image
that is one byte per residual instead of two, which halves the RLE and LZ
input — on Monument Valley that alone is 8 % off the final file.

## Compression Methods (one global per file, in the container header)

| ID | Name | Stage combination |
|----|------|-------------------|
| 0 | `raw` | residual symbols straight into the range coder |
| 1 | `rle` | residual bytes → RLE → literal tokens → range coder (EOF-terminated) |
| 2 | `lz` | residual bytes → RLE → LZ77 tokens → range coder (4-byte RLE length header) |
| 3 | `mix` | per-tile best of 0/1/2; each tile body starts with a 1-byte method tag |

The encoder runs all 3 candidates per tile, sums the sizes, and picks the single method that minimizes the total (tie → lower method ID → deterministic). With `mix`, every tile independently stores whichever method wins for it — mixed-content images (solid areas + noise areas) compress substantially better than any single global method.

## Container Layout (v3)

```
┌──────────────────────────────────────────────┐
│ magic            8B   "PYRA\r\n\x1A\n"       │
│ version          I    4B   (3)               │
│ width            I    4B                     │
│ height           I    4B                     │
│ bit_depth        B    1B  (8 or 16)          │
│ color_space      B    1B  (1=GRAY 2=RGB 3=RGBA) │
│ pred_type        B    1B  (0..6)             │
│ method           B    1B  (0=RAW 1=RLE 2=LZ 3=MIX) │
│ tile_size        I    4B                     │
│ num_tiles        I    4B                     │
│ transform        B    1B  (0=YCOCG 1=NONE)   │
│ filter_mode      B    1B  (0=GLOBAL 1=ADAPTIVE) │
├──────────────────────────────────────────────┤
│ index_size       I    4B                     │
│ tile index       num_tiles × (offset I, len I)  (8B each) │
├──────────────────────────────────────────────┤
│ compressed tiles ...                          │
├──────────────────────────────────────────────┤
│ CRC32 (over everything above)  I   4B        │
└──────────────────────────────────────────────┘
```

Header format string: `">8sIIIBBBBIIBB"` — 34 bytes.

**v1 and v2 files still load.** Version 1 used the 32-byte header
`">8sIIIBBBBII"` with no transform/filter bytes; such files are read as
`transform = YCOCG`, `filter_mode = GLOBAL`, which is exactly how they were
encoded. Version 2 has the same 34-byte layout as v3 but stores residuals
with the old, wider packing (the alphabet rounded up to a power of two
instead of the exact bit width); the decoder picks the packing width from
the version byte, so v2 files decode bit-exactly as well. Writing old
versions is no longer offered, reading them is.

## Security Limits and Fuzz Properties

| Limit | Value |
|-------|-------|
| MAX_WIDTH | 65536 |
| MAX_HEIGHT | 65536 |
| MAX_PIXELS | 16384 × 16384 |
| MAX_CHUNK_SIZE | 512 MB |

The decoder refuses:

- files shorter than 36 bytes
- bad magic
- bad CRC32
- invalid method byte
- offsets pointing past the end / truncated tile index
- invalid method byte (must be 0..3; per-tile method tags in MIX files must be 0..2)
- decompression bombs (RLE token streams capped at `2 * packed_len + 8`, LZ header length capped at `2 * packed_len + 64`, LZ symbol cap at `target_len + 1`, zero-length runs at RLE)

Every rejection raises `CorruptedFileError` (or `SecurityError`) — never a crash, never memory blowup. Fuzz trial counts above were generated by feeding random bytes into each decoder-stage API; 0 unbounded growth, 0 crashes.

## CLI

```bash
python3 -m pyra.cli <command> [args]

# Encoding with options
python3 -m pyra.cli encode in.ppm out.pyra \
    --method auto|raw|rle|lz|mix \
    --predictor auto|none|left|up|average|paeth|gradient|median \
    --transform auto|ycocg|none \
    --filter auto|global|adaptive \
    --tile 256 --colorspace auto|gray|rgb|rgba

python3 -m pyra.cli decode out.pyra restored.ppm
python3 -m pyra.cli inspect out.pyra
python3 -m pyra.cli verify out.pyra
python3 -m pyra.cli benchmark in.ppm     # PYRA vs PNG comparison (same pixels)
```

`auto` (the default everywhere) lets the codec measure the alternatives and
pick the cheapest one; the forced values reproduce the v1 behaviour exactly.

Example run (96×64 8-bit RGB synthetic image):

```
$ python3 -m pyra.cli benchmark /tmp/opencode/t96.ppm
Benchmarking /tmp/opencode/t96.ppm: 96x64 8-bit rgb (18432 raw bytes)
  encode: 0.317 s (57 KiB/s)
  decode: 0.052 s (346 KiB/s)
  raw size:       18432 bytes
  PYRA size:       1485 bytes  (method lz, ratio 12.41x)
  PNG size:         302 bytes  (ratio 61.03x, png encode 0.016 s, C zlib deflate)
  PYRA vs PNG: +1183 bytes (+391.7%)
  roundtrip: OK
```

### Why PNG wins on synthetic images (and that's fine)

PNG uses zlib's **C-optimized** DEFLATE and per-row adaptive filtering. PYRA's entropy stage is a hand-rolled adaptive range coder intended to be *honest* about pure-Python limits — it competes on being dependency-free, deterministic and security-clean, not on beating C.

For photographic-ish images (noise + gradients) PYRA gets closer:

## Optional Rust Core (native hot paths)

PYRA ships a pure-Python codec that is **complete on its own** — the optional
Rust and C++ backends only accelerate the byte-heavy hot paths without
changing a single output bit:

| Component | Files | Notes |
|-----------|-------|-------|
| `rust/pyra_core.rs` | 1 | std-only cdylib, no external crates |
| `rust/pyra_image.rs` | 1 | whole-image encoder/decoder, one thread per tile |
| `cpp/pyra_fast.cpp` | 1 | optional C++ helper (adaptive row filters) |
| `scripts/build_native.sh` | 1 | `rustc --edition 2021 -O --crate-type cdylib` (+ g++ if present) |
| `native.py` | 1 | stdlib `ctypes` loader, runtime dispatch flags |
| `fast.py` | 1 | stdlib `ctypes` loader for the optional C++ helper |
| `compress.py` dispatch | — | `None`-output contract → automatic pure fallback |

Ported stages (bit-exact to the Python reference, proven by the test suite):
`pack_residuals`, `unpack_residuals`, `rle_encode`, `rle_decode`, `lz_encode`
plus a batch-mode `lz_decode_tokens`, the **adaptive range coder itself**
(`pyra_range_encode` / `pyra_range_decode`, including the Fenwick CDF model
with its halving policy and the exact renormalization order), the
**fused per-method decoders** `pyra_decode_rle_method` /
`pyra_decode_lz_method` (one range-decoder session that streams the token
stream straight into RLE/LZ expansion and residual unpacking), the
**planner** `pyra_plan` (measures the transform / filter / predictor
candidates with the real coder on the same spread sample as the Python
reference), the predictor row kernels `apply_predictor_row` /
`reconstruct_row` (all 7 predictor types, Python `%` semantics via
`((x % m) + m) % m`), the reversible YCoCg transform `transform_forward` /
`transform_inverse`, and two **fused pipelines** in the same spirit as
`PyraCodec._residual_stream` and the decode tail: `pipe_forward` (color
transform → canonicalization → per-row prediction, one buffer conversion per
call) and `pipe_reconstruct` (row reconstruction → descaling → inverse
YCoCg).

The v2 stages are native too: `pipe_forward_adaptive_into` /
`pipe_reconstruct_adaptive_into` run the whole per-row predictor selection
(7 candidates per row, modular L1 score) and the matching reconstruction,
including the 3-bit-per-row filter table.

On top of that the whole-image codec runs a **zero-copy buffer pipeline**:
the pixel canvas lives in a single `array('i')` buffer and the native core
extracts every tile (`extract_tile`), predicts it in place
(`pipe_forward_into`), encodes the requested method candidate straight from
that buffer (`pyra_encode_raw_method` / `pyra_encode_rle_method` /
`pyra_encode_lz_method`) and, on the decode side, expands the residuals into
a buffer (`pyra_decode_rle_method` / `pyra_decode_lz_method`) and
reconstructs the tile straight into the output canvas (`pipe_reconstruct_into`).
No tile ever round-trips through a Python list any more.

**v3 goes one level higher**: `pyra_encode_image` / `pyra_decode_image` run
the *whole* container in Rust — planning, tiling, the three method
candidates, the container header, the tile index and the CRC32 — and encode
or decode the tiles **in parallel, one thread per tile** (`std::thread::scope`).
Python hands over the pixel buffer and gets the finished file back; the
pure-Python path stays available and produces byte-identical output.

```bash
scripts/build_native.sh        # -> _libpyra_core.so (loaded automatically)
PYRA_DISABLE_NATIVE=1 python3 -m pyra.tests.test_lossless   # force fallback
PYRA_NATIVE=0 ...              # same effect
```

Dispatch flags are read at **call time** (not import time), so the test suite
re-encodes whole containers in both modes and asserts the container bytes are
identical — 246 checks pass with the native core, the same core set passes on
the pure-Python path.

Measured speedups (256k samples, best of 3):

| stage              | native | pure   | speedup |
|--------------------|--------|--------|---------|
| rle (noise)        | 1.8 ms | 73 ms  | 41x     |
| lz (smooth)        | 18 ms  | 235 ms | 13x     |
| lz (noise)         | 107 ms | 465 ms | 4.3x    |
| pack 64k           | 1.6 ms | 8.4 ms | 5.3x    |
| unpack 64k         | 13 ms  | 34 ms  | 2.6x    |

Whole-container effect of the native core (256×256 tiles, best of 3,
v3 format with the automatic planner enabled):

| path            | encode | decode |
|-----------------|--------|--------|
| 256² GRAY + RAW | 42.3x  | 59.6x  |
| 256² GRAY + RLE | 45.2x  | 60.7x  |
| 256² GRAY + LZ  | 38.5x  | 61.6x  |
| 256² RGB  + RAW | 46.9x  | 54.2x  |
| 256² RGB  + RLE | 48.2x  | 62.3x  |
| 256² RGB  + LZ  | 41.5x  | 61.6x  |
| 256² RGBA + RAW | 49.7x  | 58.8x  |
| 256² RGBA + RLE | 50.3x  | 59.9x  |
| 256² RGBA + LZ  | 43.1x  | 60.8x  |

Noise-like input (worst case for the predictor) lands in the same range:
encode 22–54x, decode 51–56x.

A whole real photograph (640×360 RGB, Clouds) shows what the parallel
whole-image path buys on real data:

| stage   | native | pure-Python | speedup |
|---------|--------|-------------|---------|
| encode  | 127 ms | 12 684 ms   | 100x    |
| decode  | 31 ms  | 3 178 ms    | 102x    |

Decode used to be entropy-decode-bound (the range coder stayed in Python);
it is ported too, so both directions now run an order of magnitude faster
on the same bytes. What is left on the Python side is tile bookkeeping and
the final `array` → list conversion of the decoded canvas.

Inside the coder the hot loop was tuned without changing a single output
bit: the renormalization step picks the E1/E2/E3 branch with masks instead
of a branch, the LZ match table is keyed by a multiply-shift hash instead of
the standard library's SipHash, LZ matches are compared eight bytes at a
time, and the adaptive CDF model caches the last updated symbol — updating
a symbol never changes the cumulative sum below it, so a repeated symbol
(the common case in real residuals) skips the tree search entirely. That last
one alone is worth 18 % on encode and 28 % on decode of a real photo.

Profiling the remaining hot spots (rdtsc counters inside the decoder) puts
the cost at the CDF search and the renormalization, roughly 5.6
renormalization iterations per symbol with an even E1/E2 split. Two further
ideas were implemented, measured and **rejected** because they lost
performance without gaining correctness: batching consecutive E1 shifts
(+17% slower) and a 64-bit windowed bit reader (+3% slower on the RLE
decode). The remaining idea — a two-level CDF model instead of the Fenwick
tree — was left alone; it trades decoder speed for encoder update cost and
does not pay off for the large alphabets that the RAW method uses.

The native library is never required: if `rustc` or the `.so` is missing,
`native.py` returns `None` and the pure path takes over transparently.

## Architecture

```
pyra/
├── __init__.py        public API
├── __main__.py        python -m pyra entry point
├── cli.py             argparse CLI
├── api.py             encode_file / decode_file / decode_region / iter_tiles / encode_animation
├── codec.py           PyraCodec — per-tile encode/decode + method selection
├── container.py       magic, header, tile index, CRC32 (PyraContainer)
├── compress.py        pack_residuals / RLE / LZ77 (dispatches to the optional Rust core; TOKEN_* symbols)
├── native.py          ctypes loader for _libpyra_core.so — None-output fallback contract
├── fast.py            ctypes loader for _libpyra_fast.so (C++ fast path, same contract)
├── rust/pyra_core.rs  std-only Rust cdylib port of the hot paths (bit-exact to Python)
├── rust/pyra_image.rs whole-image encoder/decoder in Rust, one thread per tile
├── cpp/pyra_fast.cpp  C++20 shared library: adaptive row filters (bit-exact to Python)
├── scripts/build_native.sh  builds the Rust core and, if g++ is present, the C++ core
├── entropy.py         adaptive range coder + Fenwick tree
├── entropycost.py     exact integer entropy cost (used by the planner's tie-breaks)
├── predictors.py      7 reversible predictors + per-row adaptive selection
├── color.py           reversible YCoCg + optional "none" transform
├── tile.py            split / assemble
├── security.py        limits + typed errors
├── pngbench.py        stdlib-only PNG reader/writer (benchmark reference + write/read loop test)
└── tests/test_lossless.py   246-check verification suite
```

## Running Tests

```bash
python3 -m pyra.tests.test_lossless
```

Every check asserts `decoded == original` (no sampling, no approximation).

## License

MIT — do what you want, attribution appreciated.
