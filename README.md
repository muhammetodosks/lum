# PYRA

**PYRA** is a lossless image format and codec written in **pure Python 3.12+ with zero dependencies** — standard library only. No Pillow, no NumPy, no C extensions.

The magic value is `b"PYRA\r\n\x1A\n"` — a recognizable 8-byte signature followed by control characters (`\r\n` line ending, `\x1A` EOF char, trailing `\n`). This borrows PNG's corruption-protection scheme: the suffix survives text-mode line-ending translation and pipes through naive byte-damaging tools as clear text garbage rather than silently passing.

## Documentation

| Document | Content |
|----------|---------|
| [`README.md`](README.md) | This file — features, ratios, speed, CLI |
| [`PYRA-DOCUMENTATION.md`](PYRA-DOCUMENTATION.md) | The complete technical documentation of both formats (26 sections) |
| [`docs/FORMAT-PYRM.md`](docs/FORMAT-PYRM.md) | The PYRM medical container specification |
| [`CHANGELOG.md`](CHANGELOG.md) | Version history with the measured results |

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
| Cloud container PYRC | ✅ | `test_cloud.py` 25 checks, Rust-only codec |
| Medical container PYRM | ✅ | `test_medical.py` 49 checks, 864 native/pure parity trials |
| DICOM Part 10 files | ✅ | Real DICOM in and out, explicit VR little endian, uncompressed |
| 3D volume support | ✅ | Slice-local tiles, `slice` predictor for volumetric data |
| SHA-256 content hash | ✅ | Every container carries the hash of the raw samples |
| DICOM compatible metadata | ✅ | Calibration, photometric interpretation, display window, sample description |
| Clinical metadata validation | ✅ | A contradictory or incomplete metadata block is refused |
| LOCO-I / JPEG-LS predictor | ✅ | The median edge detector as an eighth predictor |
| `med verify` command | ✅ | Every integrity guarantee checked without decoding |

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

## Medical imaging (PYRM)

`PYRM` is a sibling container for medical data: the same lossless
predictor + entropy coder core, but built for volumes, signed samples and
auditability. The full specification lives in
[`docs/FORMAT-PYRM.md`](docs/FORMAT-PYRM.md).

| Property | How it is met |
|----------|---------------|
| Lossless | The decoded samples are bit-identical to the input; the container stores the SHA-256 of the raw samples |
| Corruption detection | Header CRC-32, metadata CRC-32, per-tile CRC-32, payload CRC-32 and the SHA-256 content hash are all verified on decode |
| Clinical semantics | DICOM keyword metadata: rescale slope/intercept, photometric interpretation (MONOCHROME1/2, RGB), window centre/width, bits stored/allocated, pixel representation |
| Metadata validation | `build_dicom_metadata()` derives a consistent block, `validate_dicom_metadata()` refuses a contradictory one, `require_dicom=True` makes the encoder enforce it |
| Volumes | `depth` slices, tile-local coding so slices and tiles decode in parallel, plus a `slice` predictor that reads the previous slice |
| Sample formats | u8, u16, u32, i16, i32, f32 (floats are shuffled into byte planes before coding) |
| Metadata | UTF-8 JSON block with DICOM keywords (patient ids, scanner, protocol, calibration, window, photometric interpretation) with its own CRC |
| Preview | Window/level 8-bit preview of any slice, computed in C++ |
| DICOM files | Reads and writes real DICOM Part 10 files (explicit VR little endian, uncompressed), so a container can go to a PACS and come back |
| Context modelling (v3) | ✅ | One adaptive model per byte plane, 6-18 % smaller on textured volumes |
| Region decoding | ✅ | Only the tiles a box touches are decoded |
| Signed containers (HMAC-SHA256) | ✅ | A keyed MAC over the whole file, in the reserved header bytes |
| Third-party DICOM conformance | ✅ | pydicom parses our files and we parse pydicom's, both byte-exact |
| Metadata fidelity | PYRM → DICOM → PYRM keeps the clinical attributes **exactly** the same set - no loss, no invention, no duplicated DICOM tag |
| Clinical consistency | A container whose metadata contradicts the image is reported as **not** clinically usable, with the exact problems listed |
| Speed | One entropy symbol per *sample* (not per byte): the centred residual is zig-zag coded and only rare outliers escape into a few extra symbols |

The decoder never returns unverified data: every integrity check runs before
the samples are handed back, and a mismatch raises `CorruptedFileError`.

```python
from pyra.medical import encode, decode, info, preview_slice

blob = encode(samples, width, height, depth=1, channels=1, sample_format=1,
              metadata={"scanner": "Siemens"})
meta, samples_back = decode(blob)
```

```bash
# a clinically complete container: the geometry dependent attributes are
# derived, --require-dicom refuses to write an invalid metadata block
python3 -m pyra med encode ct.raw --width 512 --height 512 --depth 64 \
        --format u16 --dicom --require-dicom \
        --patient-id ANON-001 --modality CT \
        --study-uid 1.2.840.113619.2.1 --series-uid 1.2.840.113619.2.1.4 \
        --window-center 40 --window-width 400 ct.pyrm

# or a free form metadata block
python3 -m pyra med encode ct.raw --width 512 --height 512 --depth 64 \
        --format u16 --meta scanner=Siemens --meta patient=anon ct.pyrm

python3 -m pyra med info ct.pyrm          # header + clinical attributes
python3 -m pyra med dump ct.pyrm          # geometry + integrity + metadata as JSON
python3 -m pyra med dump --metadata-only ct.pyrm   # just the metadata block
python3 -m pyra med verify ct.pyrm        # every integrity guarantee
python3 -m pyra med preview ct.pyrm s30.pgm --slice 30

python3 -m pyra med to-dicom   ct.pyrm ct.dcm    # DICOM Part 10
python3 -m pyra med from-dicom ct.dcm ct.pyrm
```

### Signed containers

A clinical archive must be able to tell an untouched file from a modified
one. `PYRM` signs the container with **HMAC-SHA256**; the 32 byte signature
lives in the reserved header bytes, and the metadata can carry a
provenance block (encoder, geometry, the hash of the source samples and an
optional timestamp).

```bash
# sign the container and record where it came from
python3 -m pyra med encode ct.raw --width 512 --height 512 --depth 64 \
        --format u16 --dicom --require-dicom --patient-id ANON-001 \
        --modality CT --sign-key site.key --audit ct.pyrm

python3 -m pyra med verify --key site.key ct.pyrm   # signature: verified
python3 -m pyra med decode --verify-key site.key ct.pyrm out.raw
```

Reading a signed container without a key is allowed but reported as
`unverified`; reading it with the **wrong** key is refused, and so is a
container whose payload, metadata or header was modified after signing.

The preview takes the display window from the metadata (the command line
can override it), applies the calibration of the modality before the window
and honours the photometric interpretation — a `MONOCHROME1` study is shown
inverted, as it must be.

### PYRM speed

Measured on a synthetic 16-bit CT-like volume (gradient + noise + bone
texture), native core versus the pure-Python reference:

| volume | encode | decode | ratio |
|--------|--------|--------|-------|
| 128×128×16 u16 | 36 ms (169xx) | 23 ms (93x) | 2.67x |
| 256×256×32 u16 | 275 ms (215x) | 159 ms (113x) | 2.69x |
| 512×512×64 u16 | 2 389 ms | 1 415 ms | 2.70x |

The numbers in brackets are the speedup over the pure-Python reference,
which produces the very same bytes. Over the three volumes the file size
drops from 38.3 MB to 14.2 MB (2.69x).

Compared to the first PYRM release the rewritten Rust core and the
one-symbol-per-sample coding changed both:

| 256×256×32 u16 (4 MB) | before | now | gain |
|----------------------|--------|-----|------|
| encode (automatic method) | 2 051 ms | **275 ms** | 7.5x |
| decode | 491 ms | **159 ms** | 3.1x |
| file size | 2 000 713 B | **1 556 684 B** | 22 % smaller |

The file gets *smaller* as well, because one zig-zag coded symbol per
sample is a better model of the residuals than two independent byte
models.

Two more changes in the same vein made the encoder faster without
touching a single output bit:

* the modular reduction of a residual is a **mask** instead of a chain of
  conditionals - the residual modulus is always a power of two, and the
  sign of a residual is data dependent, so every mispredicted branch cost
  more than the arithmetic around it;
* the LZ matcher uses the classic two array layout (the most recent
  position per key hash plus a "previous position with the same hash"
  array) instead of a hash map with a heap allocated chain per key, so it
  never allocates and its working set stays inside the cache.

The Python reference implements the same matcher, so both paths still
produce byte-identical containers.

## Cloud and earth observation (PYRC)

`PYRC` is the third container of the project, written for satellite, radar
and other multi-band observations. It is implemented **in Rust only** — the
Python package is a thin `ctypes` binding, so there is exactly one codec and
no pure-Python fallback to keep in sync.

| Property | How it is met |
|----------|---------------|
| Multi-band, multi-scene | A cube of `scene × band × row × column` samples with per-band and per-scene tables |
| Band metadata | Name, wavelength, type, units, scale, offset and the valid range of every band |
| Scene metadata | Timestamp, geographic origin and the pixel size of every scene |
| Volumetric prediction | The `TIME` predictor reads the previous scene, the `BAND` predictor the previous band |
| Lossless | The decoded cube is byte-identical to the input and the header carries its SHA-256 |
| Speed | Tiles are coded in parallel; a single band can be read without decoding the rest of the cube |
| Integrity | Every structure has a checksum: header, metadata, tile index, tile bodies, payload, plus the SHA-256 of the cube |
| Tooling | A C++ working-set layer (statistics, histograms, false-colour rendering, planar/interleaved conversion) and a `pyra cloud` command group |

```python
from pyra.cloud import encode, decode, read_band

blob = encode(samples, width=4096, height=4096, bands=12, scenes=8,
              sample_format=1,                    # u16
              band_meta=[{"name": "B01", "wavelength_nm": 443.0}],
              scene_meta=[{"timestamp": 1700000000}])
meta, cube = decode(blob)
one_band = read_band(blob, scene=0, band=3)      # only that band is decoded
```

### The cloud command line

```bash
# a raw cube (the "PYRX" working-set format) into a container
python3 -m pyra cloud encode cube.pyrx cube.pyrc --width 4096 --height 4096 \
        --bands 12 --scenes 8 --format u16 --band-name B01 --band-wavelength 443

python3 -m pyra cloud info cube.pyrc            # geometry, bands, scenes
python3 -m pyra cloud stats cube.pyrc --band 0  # min/max/mean/stddev per band
python3 -m pyra cloud render cube.pyrc rgb.ppm --scene 0 --red 3 --green 2 --blue 1
python3 -m pyra cloud decode cube.pyrc --scene 0 --band 3 one_band.pyrx
```

### The C++ working-set layer

`cpp/pyra_cloud.cpp` (C++20, std only) provides the operations a cloud
workflow needs on a decoded cube, without a Python round trip:

| Function | What it does |
|----------|--------------|
| `pyra_cloud_raw_write` / `pyra_cloud_raw_read` | The `PYRX` raw cube format (32 byte header, band planar) |
| `pyra_cloud_band_stats` | Minimum, maximum, mean, standard deviation and a 256 bin histogram per band |
| `pyra_cloud_render_rgb` | False-colour composite of three bands with a window and a level |
| `pyra_cloud_interleave` / `pyra_cloud_deinterleave` | Band planar ↔ pixel interleaved conversion |

The file format is specified in
[`docs/FORMAT-PYRC.md`](docs/FORMAT-PYRC.md).

---

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
├── rust/pyra_med.rs   native PYRM encoder/decoder (SHA-256, parallel tiles)
├── rust/pyra_cloud.rs native PYRC encoder/decoder (the cloud codec, Rust only)
├── cpp/pyra_fast.cpp  C++20 shared library: adaptive row filters (bit-exact to Python)
├── cpp/pyra_cloud.cpp C++20 working-set layer for cloud cubes: raw I/O, statistics, rendering
├── cloud.py            PYRC binding: encode / decode / info / read_band + the C++ helpers
├── scripts/build_native.sh  builds the Rust core and, if g++ is present, the C++ cores
├── scripts/run_tests.sh     runs every suite (native, fallback, Rust unit tests, C++ self test)
├── entropy.py         adaptive range coder + Fenwick tree
├── entropycost.py     exact integer entropy cost (used by the planner's tie-breaks)
├── predictors.py      7 reversible predictors + per-row adaptive selection
├── color.py           reversible YCoCg + optional "none" transform
├── tile.py            split / assemble
├── security.py        limits + typed errors
├── pngbench.py        stdlib-only PNG reader/writer (benchmark reference + write/read loop test)
├── medical/           PYRM medical container (constants, container, predictors, lz, codec, preview, dicom, dicomfile, native)
├── docs/FORMAT-PYRM.md    the PYRM format specification
├── docs/FORMAT-PYRC.md    the PYRC format specification
└── tests/             test_lossless.py (246 checks) + test_medical.py (96 checks) + test_cloud.py (25 checks)
```

## Running Tests

```bash
python3 -m pyra.tests.test_lossless
python3 -m pyra.tests.test_medical
python3 -m pyra.tests.test_cloud

bash scripts/run_tests.sh              # all of the above, native and fallback
PYRA_BUILD_TESTS=1 bash scripts/run_tests.sh   # plus the Rust unit tests and the C++ self test
```

Every check asserts `decoded == original` (no sampling, no approximation).

## License

MIT — do what you want, attribution appreciated.
