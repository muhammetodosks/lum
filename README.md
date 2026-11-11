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
| Lossless pipeline | ✅ | `test_lossless.py` 158 checks |
| Deterministic encoder | ✅ | Same input → identical bytes |
| Unsafe-input fuzz | ✅ | 200 RLE / 100 LZ / 80 pipeline / 150 corruption trials, 0 leaks |
| CRC32 integrity | ✅ | verify command + decoder |
| decode_region (ROI) | ✅ | Only tiles touching the region are decoded |
| Multi-predictor engine | ✅ | 7 reversible predictors |
| Per-file best method select | ✅ | Encodes all 3 methods per tile, keeps cheapest globally |
| SecurityError / CorruptedFileError | ✅ | Explicit typed errors |
| 16-bit depth | ✅ | Full-pixel domain covered |
| Animation container | ✅ | Frame count + fps header |
| CLI | ✅ | encode/decode/inspect/verify/benchmark |

## The Pipeline

```
Pixel
  │
  ├─▶ 1. Color Transform     YCoCg — reversible integer RGB/RGBA→YCoCg; GRAY passthrough
  ├─▶ 2. Tile                images split into tile_size × tile_size blocks
  ├─▶ 3. Predictor           residuals: NONE/LEFT/UP/AVERAGE/PAETH/GRADIENT/MEDIAN
  ├─▶ 4. Residual            exact integer residuals, modular alphabet (res+pred) % mod
  ├─▶ 5. RLE                 escape-coded run-length over residual bytes
  ├─▶ 6. LZ                  LZ77 with 3-byte hash buckets, self-overlap allowed
  └─▶ 7. Entropy             adaptive range coder (Fenwick-tree adaptive model)
```

### Canonicalization detail (RGB/RGBA)

Reversible YCoCg produces exact integers that can be negative or exceed the sample domain. Every transformed channel (including passthrough alpha) is canonicalized with a uniform offset into a modular alphabet:

```python
mod = 1 << (bit_depth + 3)   # RGB/RGBA
mod = 1 << (bit_depth)       # GRAY
offset = mod >> 1            # RGB/RGBA only
```

Decode side: subtract the offset, then invert the color transform. Bit-exact roundtrip verified for RGB/RGBA/GRAY × 8/16-bit × all 7 predictors.

## Compression Methods (one global per file, in the container header)

| ID | Name | Stage combination |
|----|------|-------------------|
| 0 | `raw` | residual symbols straight into the range coder |
| 1 | `rle` | residual bytes → RLE → literal tokens → range coder (EOF-terminated) |
| 2 | `lz` | residual bytes → RLE → LZ77 tokens → range coder (4-byte RLE length header) |
| 3 | `mix` | per-tile best of 0/1/2; each tile body starts with a 1-byte method tag |

The encoder runs all 3 candidates per tile, sums the sizes, and picks the single method that minimizes the total (tie → lower method ID → deterministic). With `mix`, every tile independently stores whichever method wins for it — mixed-content images (solid areas + noise areas) compress substantially better than any single global method.

## Container Layout

```
┌──────────────────────────────────────────────┐
│ magic            8B   "PYRA\r\n\x1A\n"       │
│ version          I    4B   (1)               │
│ width            I    4B                     │
│ height           I    4B                     │
│ bit_depth        B    1B  (8 or 16)          │
│ color_space      B    1B  (1=GRAY 2=RGB 3=RGBA) │
│ pred_type        B    1B  (0..6)             │
│ method           B    1B  (0=RAW 1=RLE 2=LZ 3=MIX) │
│ tile_size        I    4B                     │
│ num_tiles        I    4B                     │
├──────────────────────────────────────────────┤
│ index_size       I    4B                     │
│ tile index       num_tiles × (offset I, len I)  (8B each) │
├──────────────────────────────────────────────┤
│ compressed tiles ...                          │
├──────────────────────────────────────────────┤
│ CRC32 (over everything above)  I   4B        │
└──────────────────────────────────────────────┘
```

Header format string: `">8sIIIBBBBII"` — 32 bytes.

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
    --predictor none|left|up|average|paeth|gradient|median \
    --tile 256 --colorspace auto|gray|rgb|rgba

python3 -m pyra.cli decode out.pyra restored.ppm
python3 -m pyra.cli inspect out.pyra
python3 -m pyra.cli verify out.pyra
python3 -m pyra.cli benchmark in.ppm     # PYRA vs PNG comparison (same pixels)
```

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

PYRA ships a pure-Python codec that is **complete on its own** — the optional Rust
backend only accelerates the byte-heavy hot paths without changing a single
output bit:

| Component | Files | Notes |
|-----------|-------|-------|
| `rust/pyra_core.rs` | 1 | std-only cdylib, no external crates |
| `scripts/build_native.sh` | 1 | `rustc --edition 2021 -O --crate-type cdylib` |
| `native.py` | 1 | stdlib `ctypes` loader, runtime dispatch flags |
| `compress.py` dispatch | — | `None`-output contract → automatic pure fallback |

Ported stages (bit-exact to the Python reference, proven by the test suite):
`pack_residuals`, `unpack_residuals`, `rle_encode`, `rle_decode`, `lz_encode`
plus a batch-mode `lz_decode_tokens`, the **adaptive range coder itself**
(`pyra_range_encode` / `pyra_range_decode`, including the Fenwick CDF model
with its halving policy and the exact renormalization order), the
**fused per-method decoders** `pyra_decode_rle_method` /
`pyra_decode_lz_method` (one range-decoder session that streams the token
stream straight into RLE/LZ expansion and residual unpacking), the
predictor row kernels `apply_predictor_row` / `reconstruct_row` (all 7
predictor types, Python `%` semantics via `((x % m) + m) % m`), the
reversible YCoCg transform `transform_forward` / `transform_inverse`, and
two **fused pipelines** in the same spirit as `PyraCodec._residual_stream`
and the decode tail: `pipe_forward` (color transform → canonicalization →
per-row prediction, one buffer conversion per call) and `pipe_reconstruct`
(row reconstruction → descaling → inverse YCoCg).

On top of that the whole-image codec runs a **zero-copy buffer pipeline**:
the pixel canvas lives in a single `array('i')` buffer and the native core
extracts every tile (`extract_tile`), predicts it in place
(`pipe_forward_into`), encodes the requested method candidate straight from
that buffer (`pyra_encode_raw_method` / `pyra_encode_rle_method` /
`pyra_encode_lz_method`) and, on the decode side, expands the residuals into
a buffer (`pyra_decode_rle_method` / `pyra_decode_lz_method`) and
reconstructs the tile straight into the output canvas (`pipe_reconstruct_into`).
No tile ever round-trips through a Python list any more.

```bash
scripts/build_native.sh        # -> _libpyra_core.so (loaded automatically)
PYRA_DISABLE_NATIVE=1 python3 -m pyra.tests.test_lossless   # force fallback
PYRA_NATIVE=0 ...              # same effect
```

Dispatch flags are read at **call time** (not import time), so the test suite
re-encodes whole containers in both modes and asserts the container bytes are
identical — 237 checks pass with the native core, the same core set passes on
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
"photo" = gradient + noise; the canvas stays in one buffer end to end):

| path            | encode | decode |
|-----------------|--------|--------|
| 256² GRAY + LZ  | 66.3x  | 65.3x  |
| 256² RGB  + LZ  | 61.9x  | 67.5x  |
| 256² RGB  + RLE | 41.6x  | 68.2x  |
| 256² RGB  + RAW | 40.3x  | 62.6x  |
| 256² RGBA + LZ  | 63.7x  | 68.2x  |
| 256² RGBA + RLE | 44.6x  | 67.5x  |
| 512² RGB  + LZ  | 62.8x  | 68.1x  |

Noise-like input (worst case for the predictor) lands in the same range:
encode 41–52x, decode 53–67x.

Decode used to be entropy-decode-bound (the range coder stayed in Python);
it is ported too, so both directions now run an order of magnitude faster
on the same bytes. What is left on the Python side is tile bookkeeping and
the final `array` → list conversion of the decoded canvas.

Inside the coder the hot loop was tuned without changing a single output
bit: the renormalization step picks the E1/E2/E3 branch with masks instead
of a branch, the LZ match table is keyed by a multiply-shift hash instead of
the standard library's SipHash, and LZ matches are compared eight bytes at a
time.

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
├── rust/pyra_core.rs  std-only Rust cdylib port of the hot paths (bit-exact to Python)
├── scripts/build_native.sh  rustc build script
├── entropy.py         adaptive range coder + Fenwick tree
├── predictors.py      7 reversible predictors
├── color.py           reversible YCoCg
├── tile.py            split / assemble
├── security.py        limits + typed errors
├── pngbench.py        stdlib-only PNG reader/writer (benchmark reference + write/read loop test)
└── tests/test_lossless.py   189-check verification suite
```

## Running Tests

```bash
python3 -m pyra.tests.test_lossless
```

Every check asserts `decoded == original` (no sampling, no approximation).

## License

MIT — do what you want, attribution appreciated.
