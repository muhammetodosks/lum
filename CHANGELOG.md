# Changelog

All notable changes to LUMEN, LUMM and LUMC. The format versions are independent:
`LUMEN` container v1 → v2 → v3 (lossless) and v6 (lossy), `LUMM` container
v1 → v2.

The project keeps **bit-exact parity** between the native Rust/C++ backends and the
pure-Python reference: both produce byte-identical containers.

---

## v1.0.0 — 2026-11-23 — LUMEN: the format gets its name

The working name *PYRA* is retired. The format is **LUMEN** — light — with the
extension `.lum` next to `.jpg`/`.png`/`.jxl`, the CLI `lum`, and the magic
bytes `LUMA\r\n\x1A\n`. Nothing about the pixel pipeline changed: this release
is the rename plus the measurement work that earns the 1.0.

### The name

| Layer | Name | Magic | Extension |
|-------|------|-------|-----------|
| Lossless / lossy image | **LUMEN** | `LUMA\r\n\x1A\n` | `.lum` |
| Cloud / earth observation | **LUMC** | `LUMC\r\n\x1A\n` | `.lumc`, raw cubes `.lumx` |
| Medical / DICOM | **LUMM** | `LUMM\r\n\x1A\n` | `.lumm`, previews `.lump` |

The `LUMA` / `LUMC` / `LUMM` family keeps the previous base-plus-letter scheme,
so the three containers still read as siblings. The DICOM private creator is
`LUMEN-LUMM`.

**This is a breaking rename.** A 0.x file carries `PYRA\r\n\x1A\n` and is
rejected by 1.0.0 with `CorruptedFileError`; a 1.0.0 file is rejected by 0.x.
The container *layout*, the version bytes and every bit of the encoded stream
are otherwise unchanged, so the only difference between a 0.x stream and a
1.0.0 stream is the eight magic bytes and the renamed symbols.

Renamed in the same pass:

- package `pyra/` → `lum/`, imports `from lum import api, lossy`
- CLI `python3 -m pyra.cli` → `python3 -m lum.cli`
- native libraries `_libpyra_core.so` → `_liblum_core.so`, plus `_liblum_fast`,
  `_liblum_cloud` (the `.so` files stay out of git)
- Rust sources `pyra_{core,image,lossy,med,cloud}.rs` → `lum_*.rs`,
  C++ `pyra_{fast,cloud}.cpp` → `lum_*.cpp`
- the raw cloud interchange magic `PYRX` → `LUMX`
- tests `python3 -m lum.tests.test_*`

### Thirteen formats, measured on a corpus the codec never saw

Six Kodak PhotoCD images (0.393 MP mean) from the 24-image set, decoded to RGB
and re-encoded by the real encoder of every format — ImageMagick 6.9.12 for
PNG/JPEG/WebP/AVIF/JP2/TIFF/BMP/GIF and `ffmpeg -c:v libjxl` for JPEG XL. Each
output was decoded again and compared to the source; the byte counts and the
RGB PSNR below are what the encoders actually produced.

Lossless, mean bytes per image and KB per megapixel:

| Format | Mean size | KB/MP | vs LUMEN |
|--------|-----------|-------|----------|
| **LUMEN** | **285.9 KB** | **727.0** | — |
| JPEG XL lossless | 425.0 KB | 1080.9 | +48.7 % |
| WebP lossless | 475.3 KB | 1208.6 | +66.2 % |
| PNG (zlib 9) | 673.5 KB | 1712.8 | +135.6 % |
| TIFF (LZW) | 753.0 KB | 1915.1 | +163.4 % |
| BMP | 1152.1 KB | 2930.0 | +303.0 % |

Lossy at each encoder's quality 90, with the fidelity it really reached:

| Format | Mean size | KB/MP | PSNR |
|--------|-----------|-------|------|
| WebP q90 | 56.4 KB | 143.5 | 35.14 dB |
| JPEG XL d1.0 | 90.4 KB | 229.9 | 37.78 dB |
| AVIF q90 | 133.4 KB | 339.3 | 40.95 dB |
| JPEG q90 | 142.9 KB | 363.4 | 39.09 dB |
| **LUMEN q90** | **161.5 KB** | **410.8** | **43.70 dB** |
| GIF 256 colours | 262.2 KB | 666.8 | 37.29 dB |
| JPEG 2000 q90 | 465.2 KB | 1183.1 | 82.59 dB |

Nominal quality is not a common currency across codecs, which is why the PSNR
column is there. The honest reading: LUMEN lossless is the smallest container
in the comparison and beats JPEG XL by 24 % on photos it was not tuned on; the
lossy mode at q90 reaches 43.70 dB — above JPEG, WebP, AVIF, JPEG XL and GIF
at their own q90, with JPEG 2000's q90 being a near-lossless setting at 82.59 dB
for 2.9x the bytes — and
on the matched-fidelity curves over the full 24-image corpus it is level with
JPEG XL at 38 dB, 7.9 % larger at 34 dB and 6.1 % smaller at 42 dB. Those
curves were already in the README and are unchanged.

Speed per 0.393 MP image (four cores, native core, whole process):

| | encode | decode |
|---|---|---|
| LUMEN lossless | 203 ms | 70 ms |
| LUMEN q90 | 51 ms | 33 ms |

The external timings in the harness include process startup, so the ratios
understate LUMEN slightly rather than the other way round.

### New measurement tooling

- `benchmarks/bench_formats.py` — drives all thirteen encoders, decodes back,
  records bytes / PSNR / encode / decode time per image, writes
  `benchmarks/results/formats.json` and the centre crops.
- `benchmarks/format_report.py` — stdlib-only renderer: `psnr`, `summarise`,
  `best_by`, `render_bars_svg`, `render_scatter_svg`, `render_html`. No Pillow,
  so the test suite imports it without a dependency.
- `docs/assets/formats-bars.svg`, `docs/assets/formats-scatter.svg`,
  `docs/assets/formats.html` — the two charts and a 1.6 MB self-contained
  gallery with the decoded crops of all thirteen formats.
- `tests/test_format_report.py` — PSNR identity, size formatting, aggregation
  ordering, `best_by`, SVG validity, hostile-label escaping, empty-corpus
  rendering, gallery/table/notes.

Also fixed while renaming: the native Rust medical and cloud cores still
carried the old byte-array magic (`0x50,0x59,0x52,0x4D` / `…0x43`), so the
native path wrote `PYRM`/`PYRC` headers while the reader expected `LUMM`/`LUMC`.
Both `lum_med.rs` and `lum_cloud.rs` now emit the new magic, and
`scripts/build_native.sh` rebuilds them.

Test totals at release: 246 lossless, 72 lossy, 25 cloud, 106 medical,
33 format-report checks — all passing, plus the pure-Python path
(`LUMEN_DISABLE_NATIVE=1`) at 179 lossless and 104 medical.

---



The request was to test against `github.com/AOMediaCodec/av1-cts`. That
repository does not exist: `https://github.com/AOMediaCodec/av1-cts` returns 404,
`https://api.github.com/repos/AOMediaCodec/av1-cts` returns
`{"message": "Not Found", "status": "404"}`, and the AOMediaCodec organisation
lists 29 public repositories with no `av1-cts` among them. The conformance
vectors are public anyway, in the `aom-test-data` bucket that libaom's own pinned
manifest (`test/test-data.sha1`) references, so that is what is used here.

**Fetch and verification**

`benchmarks/fetch_av1_cts.py` lists the bucket, pulls 242 `.ivf` streams plus
their `.ivf.md5` siblings (6.4 MB), decodes each with libaom, and hashes every
decoded frame in its native 4:2:0 buffer.

```
streams with every frame digest matching: 242/242
frames matching:                          587/587
streams matching libaom test-data.sha1:   240/240 pinned (2 SVC streams not yet pinned)
FAILED: none
```

Getting there required one real fix in the verifier: libaom's digests are always
over a 4:2:0 frame buffer, so monochrome streams must be decoded to `yuv420p`
with neutral chroma, not to `gray`. Decoding them as `gray` matched 0 of 20
frames; decoding to `yuv420p` matched all of them.

**Measured**

One decoded frame per stream, 105 distinct sizes from 16x16 to 1920x1080, RGB:

| PSNR | n | LUMEN KB/MP | JXL KB/MP | mean | median | LUMEN wins |
|------|---|------------|-----------|------|--------|-----------|
| 34 dB | 239 | 112.27 | 110.78 | 100.2 % | 102.2 % | 95/239 |
| 38 dB | 241 | 165.48 | 157.31 | 107.1 % | 104.1 % | 64/241 |
| 42 dB | 241 | 236.88 | 226.51 | 105.3 % | 102.3 % | 97/241 |

Level at 34 dB, about 6 % larger at 38 and 42 dB. Conformance streams are
synthetic edge cases (quantizer sweeps, film grain, intra block copy, SVC
layers, frame sizes down to 16x16), so this is not a photograph comparison and
is labelled as such.

**Natural footage from the same bucket** — 10 frames each of three SDR
sequences, 30 frames total:

| PSNR | n | LUMEN KB/MP | JXL KB/MP | mean | median | LUMEN wins |
|------|---|------------|-----------|------|--------|-----------|
| 34 dB | 22 | 97.98 | 91.87 | 97.8 % | 103.8 % | 10/22 |
| 38 dB | 30 | 117.99 | 117.26 | 98.2 % | 106.8 % | 10/30 |
| 42 dB | 30 | 176.88 | 194.16 | 96.4 % | 99.9 % | 16/30 |

Per sequence at 38 dB: Animal 109.1 %, Health 116.7 %, Dance 68.9 %. The mean is
carried by Dance; the median is worse than the mean at 34 and 38 dB, which is
stated in the README rather than rounded away. The weakness is smooth content at
moderate rate — the same direction as the Kodak result.

Speed on the 1080x1920 frames: 13.2 s for the full LUMEN sweep against 31.4 s
for the JXL sweep, a factor of 2.4.

## v0.14.0 — independent corpus: Kodak, measured and published

Every rate comparison in this project until now used the six photographs the
quantiser was tuned on. That is a biased sample and it was reported as if it
were not. This release adds the standard independent corpus, measures it, and
publishes the result even though it is worse.

**Corpus and method**

`benchmarks/bench_kodak.py` downloads nothing and tunes nothing: it takes a
directory of PNGs, encodes each image on both codecs' own curves (LUMEN q=10..100
in steps of 5, JPEG XL through `libjxl -effort 7` at 17 distances from -1.0 to
15.0), and interpolates the size at a target RGB PSNR between the two nearest
measured points. A target one curve never reaches is dropped for that image
instead of being clamped, because clamping silently manufactures wins.

The corpus is the Kodak PhotoCD set — 24 images, 768x512 or 512x768 —
cloned from
`github.com/MohamedBakrAli/Kodak-Lossless-True-Color-Image-Suite`. Nothing in
the repository was fitted on it.

**Result**

| PSNR | n | LUMEN KB/MP | JXL KB/MP | LUMEN/JXL mean | median | LUMEN wins |
|------|---|------------|-----------|---------------|--------|-----------|
| 34 dB | 24 | 128.37 | 118.76 | 107.9 % | 109.8 % | 4/24 |
| 38 dB | 24 | 216.65 | 218.97 | 100.1 % | 103.8 % | 9/24 |
| 42 dB | 24 | 341.37 | 359.46 | 93.9 % | 95.5 % | 20/24 |

On content the encoder has not seen, LUMEN is **7.9 % larger than JXL at 34 dB**,
level at 38 dB, and 6.1 % smaller at 42 dB. The six-photo table in the README
said "smaller at every target"; that sentence is now scoped to the set it was
measured on.

The loss is not one bad image. At 34 dB the median (109.8 %) is worse than the
mean (107.9 %), so it is spread across the corpus, and LUMEN wins on only four of
the 24 images at that rate.

**Why it is published rather than tuned away**

Fitting the band table against Kodak would turn the independent corpus into a
second training set and destroy the only unbiased number in the project. The
next step is to fix the cause, not the sample: the rate term is plain squared
error over DCT coefficients, so at low rate it pays for detail a perceptual
metric would throw away. That is a hypothesis and is recorded as one — it has
not been isolated by measurement yet.

**Reproducibility**

Two complete runs of the benchmark produce identical tables. `--limit 2`
reproduces the first two per-image rows exactly, which is how the script was
checked after being moved out of `/tmp` into `benchmarks/`.

## v0.13.0 — encoder 6.4x faster, byte-identical

v0.12.0 claimed ~19 MP/s. Measuring it properly on a 1920x1080 photograph gave
**2.86 MP/s**. The old figure came from a small image and was never true at
camera resolution; the numbers below are measured on Province (1920x1080,
2.07 MP) at q=50/85/95, five iterations after a warm-up, on four cores.

Two wins, neither of which changes a single output byte:

| Stage | q50 | q85 | q95 |
|-------|-----|-----|-----|
| before | 2.86 | 2.78 | 2.67 MP/s |
| knobs hoisted | 12.59 | 11.36 | 10.08 MP/s |
| + plane threads | **18.67** | **17.12** | **16.28 MP/s** |

Decode went from 20.50 / 18.13 / 15.59 to **31.00 / 28.11 / 25.82 MP/s**.

**Deadzone and lambda read from the environment per coefficient**

`deadzone()` was called inside the 64-coefficient loop of `rdoq`, and `lam()`
once per block. Both wrap `std::env::var`, which takes a lock and allocates a
`String`: 65 environment lookups per block, on a path that runs once per
coefficient. They cannot change mid-image, so they are resolved once per plane
and passed in. Encoding: 2.86 → 12.59 MP/s, 4.4x, same bytes.

**Planes encoded and decoded in parallel**

A plane's DC prediction chain, neighbourhood state and range coder are all
confined to that plane, so plane order cannot affect a plane's bytes. Planes
are therefore spawned on scoped threads and their streams concatenated in plane
order. Below `PAR_MIN_PIXELS` (256x256) the sequential path is kept: thread
spawn costs more than a small image is worth.

Correctness was checked against a second build compiled with threading forced
off (`PAR_MIN_PIXELS = usize::MAX`), comparing both container bytes and decoded
pixels across four images, two sizes (320x240 and 1920x1080, straddling the
threshold) and two qualities: 16/16 identical. The v6 regression references
(`ref_q50`, `ref_q85`) still hash to the same values, and `tests/test_lossy.py`
passes 72/72.

**Speed against JPEG XL, same machine**

Measured through `ffmpeg -c:v libjxl -effort 7` on the same 1920x1080 image,
distances 2.0/1.0/0.5 (90/128/185 KB):

* encode: JXL 1.92-2.00 MP/s, LUMEN 16.3-18.7 MP/s -> **~9x faster**
* decode: JXL 11.2-12.6 MP/s, LUMEN 25.8-31.0 MP/s -> **~2.7x faster**

The JXL figures include ffmpeg's own startup and PPM I/O, so they are an upper
bound on JXL's speed and the ratio is if anything conservative.

**Rejected**

* Reading the knobs once per image instead of once per plane: no measurable
  difference, and per plane keeps the change local.
* Block-level threading with a shared coder: the range coder is serial by
  construction, so this would need a restart per block and cost 2-4 % of the
  rate. Plane threads capture almost all of the gain on real images.
* `-C target-cpu=native`: measured at 3 %, and it would make the binary
  non-portable for no meaningful return next to the 6.4x above.

## v0.12.0 — per-band quantisation, LUMEN v6

The lossy codec's weak point was never the entropy model: `LUMEN_STATS` reports
the range coder at 100.0 % of the ideal cost, so every byte it writes is a byte
the model asked for. The bytes were going to the wrong frequencies. The JPEG
base tables shape the step as a function of `u+v`, which treats all ten
frequency bands the same way at every quality; the measured optimum does not.

This release adds a per-band multiplier table, fitted against the real codec at
matched PSNR, and makes LUMEN faster than JXL at every measured distortion.

**Changed**

* Container version 6. The quantiser changed, so a v5 decoder would produce
  different pixels from the same bytes; the version bump makes that a hard
  error instead of a silent difference. Versions 4 and 5 are rejected.
* `band_table()` multiplies the step of each of the ten `class_of` bands on top
  of the spectral tilt, for luma and chroma alike. It sits outside the `knobs`
  gate, so encoder and decoder always derive the same table from `quality`.
* The shape is blended across three anchors (`BAND_Q_LOW` 10, `BAND_Q_MID` 56,
  `BAND_Q_HIGH` 90) and held constant outside that range.
  `LUMEN_QBAND` still overrides it for experiments; `LUMEN_GSCALE` still applies
  after it.

**Measured**

Bytes per megapixel at matched RGB PSNR against JPEG XL (`libjxl --effort 7`,
distance swept -1..15), six 800 px backgrounds, ratios averaged per image:

| PSNR | v5 | v6 | change | n |
|------|----|----|--------|-----|
| 30 dB | 87.9 % | **83.2 %** | -4.7 % | 4 |
| 34 dB | 89.2 % | **87.3 %** | -1.9 % | 4 |
| 38 dB | 101.9 % | **99.2 %** | -2.7 % | 6 |
| 42 dB | 101.7 % | **97.6 %** | -4.1 % | 6 |
| 46 dB | 91.3 % | **88.2 %** | -3.1 % | 6 |

LUMEN is now smaller than JXL at all five targets. The 30 and 34 dB rows cover
four images rather than six because JXL cannot go below 34.64 dB on Clouds or
35.68 dB on Fuji at any distance, so there is nothing to compare there.
Absolute rates where all six images are covered: 61.15 / 109.66 / 183.36 KB/MP
at 38 / 42 / 46 dB.

The gain is largest where it was needed. Fuji, Clouds and Province — the flat
photos that were losing 2 % — gain 5.2 / 3.8 / 3.5 % at 38 dB, while the
textured images (Rainbow, Northan) keep their existing 30-50 % lead.

**Measured and rejected**

* One table fitted at 38 dB applied everywhere. It generalises at 46 dB
  (-2.7 %) but pushes flat images backwards at 34 dB (Fuji +5.4 %, Clouds
  +3.2 %). The optimal shape is a function of rate, not a constant.
* Two anchors on a single line between q=10 and q=90. Better on average at
  30 dB, but the fitted shape bulges upward through the middle, so a straight
  line is wrong exactly there: Fuji still lost 2.1 % at 38 dB — the one point
  where LUMEN trailed JXL. Three anchors cost 1.0 % at 30 dB (where the margin
  is already comfortable) and fix 38 dB, so the three-anchor version is kept.
* Extrapolating the blend outside q=10..90. The end shapes are held constant
  instead: past the measured range the line produced steps coarser than any
  fit justified.

**Method note**

The matched-PSNR search now fails loudly when a target is not bracketed. An
earlier version silently snapped to the nearest measured point, which reported
an 11 % win on Rainbow and Fuji at q=85 — those curves never reach 38 dB, and
the "win" was a comparison between two different distortions.

## v0.11.0 — lossy codec (LUMEN v5)

LUMEN gains a photographic mode. Until now the container was lossless only: it
competed with PNG, had no quality knob, and was useless for camera output. The
new mode is a full transform codec — YCbCr, 8x8 DCT, perceptual quantisation and
a context-modelled binary range coder — written in the same dependency-free Rust
core as the rest of the project.

**Added**

* `encode --quality Q` (1..100) and `encode --subsampling 4:4:4|4:2:0`. `auto`
  (the default, quality 0) keeps the lossless v3 path, so nothing changes for
  existing callers.
* YCoCg colour transform instead of YCbCr, flagged in the `coding` byte so
  files written with either transform stay readable.
* Container version 5: 36-byte header (`quality`, `chroma multiplier`,
  `mode`, `plane_count`), then
  `u32 length + stream` per plane, then CRC-32 over the whole file. Version 4
  files are rejected rather than decoded: the entropy model changed and they
  would decode to the wrong pixels.
* `lum.lossy` ctypes binding (`encode`, `decode`, `info`) and a
  `LumenCodec.decode` dispatch for v5.
* `lum inspect` reports quality, chroma format and plane count for v5 files.
* `tests/test_lossy.py`: 63 checks covering round-trip accuracy, monotone
  distortion in quality, header validation, corruption rejection and the
  codec dispatch.

**Measured**

Mean bytes per megapixel at matched RGB PSNR over six 800 px backgrounds,
compared with JPEG XL (`libjxl --effort 7`, distance swept over -1..15):

| PSNR | LUMEN | as % of JXL |
|------|------|-------------|
| 30 dB | 16.5 KB | 87.9 % |
| 34 dB | 32.4 KB | 89.2 % |
| 38 dB | 63.4 KB | 101.8 % |
| 42 dB | 113.8 KB | 101.7 % |
| 46 dB | 190.3 KB | 91.3 % |

Encoding was stated here as ~19 MP/s single-threaded. That number was wrong:
measured on a 1920x1080 photo it was 2.86 MP/s. The mistake is corrected and
explained in v0.13.0, which makes the claim true rather than deleting it.

**Tuning that earned its place**

* `LAMBDA` 0.1 → 1.6: the RDOQ rate estimate assumed 3 bits for a unit
  magnitude, the measurements said 5.9, so nonzeros were underpriced.
* Spectral tilt, interpolated in quality from -0.6 to -0.95: the JPEG base
  tables spend too much on high frequencies for this coder at these rates.
  Worth -7 % at 46 dB.
* Magnitude coding as truncated unary with an Exp-Golomb escape instead of a
  binary tree.
* `last` position through a 6-bit adaptive tree instead of unary length plus
  raw bits.
* Sign prediction from lower-frequency coefficients already coded in the same
  block.
* Chroma step multiplier 0.72, carried in the header: the inverse YCbCr
  transform weights chroma error by 1.402/1.772, so chroma error dominates RGB
  MSE even though the eye tolerates it. Spending *more* on chroma than luma is
  cheaper than the reverse.
* YCoCg instead of YCbCr. Its inverse has unit coefficients (`R = Y + Co - Cg`,
  `G = Y + Cg`, `B = Y - Co - Cg`), so the same chroma error costs far less in
  RGB terms. Worth -7.4 % at 38 dB, the largest single win after the tilt.

**Measured and rejected**

* 4:2:0 chroma at matched RGB PSNR was always larger than 4:4:4.
* Flat high-frequency shaping, chroma stride, a second-order tilt term, and a
  finer 9-state neighbour context all measured neutral or worse.
* A within-block energy context for significance: neutral to negative.
* Coarser chroma (`LUMEN_CQ` 1.1 and 1.25, `LUMEN_CS` 1.4/2.0/2.8): always larger.
* Five alternative DC predictors (average of left+above, planar
  `l+u-ul`, median of the two and the planar guess, above-only, left-only).
  LOCO-I's median edge detector won at every operating point:

  ```
  med3        16.5 / 32.4 / 63.4 / 113.8 / 190.3 KB at 30/34/38/42/46 dB
  average     17.5 / 33.8 / 65.3 / 116.1 / 193.1
  planar      17.4 / 33.5 / 64.6 / 115.1 / 191.7
  med3+planar 18.8 / 35.4 / 67.1 / 118.2 / 195.2
  above only  18.7 / 35.3 / 67.0 / 118.0 / 195.1
  left only   16.8 / 32.8 / 63.8 / 114.2 / 190.8
  ```

  The block DC is the local mean, and `med3` is already the tightest
  clamp on it. Nothing to gain there.
* Modelling the exp-golomb remainder bits with adaptive probabilities
  instead of direct bits. The prefix fixes the magnitude class `k`, and the
  theory was that values bunch up at the bottom of each class, biasing the
  top remainder bit towards zero. They do not: a full bit-budget dump at
  46 dB on Province shows `raw` 12 828 B -> 0 B while `dc` 2 015 -> 4 776
  and `esc` 8 554 -> 18 598, i.e. the modelled bits cost almost exactly
  what the direct bits did. Total ideal 70 447 -> 70 425 B on a 70 KB
  plane: 23 bytes. Inside a magnitude class the remainder is uniform, so
  there is no information to extract.
* Above-right as a third neighbour in the significance and magnitude
  context. It is already reconstructed when the block is coded, so it is
  free information -- and it is still neutral at 34 dB and no better
  anywhere else (16.5 / 32.5 / 63.4 / 113.8 / 190.3).
* 16x16 DCT blocks. A standalone two-transform experiment at matched PSNR
  (orthonormal 8x8 vs 16x16 DCT, same quantiser, zero-order entropy bound
  plus per-block side costs) splits exactly along the existing weakness:

  ```
              34 dB    38 dB    42 dB
  Province   +0.3 %   +3.0 %   +4.7 %     (already our weak spot)
  Rainbow    -3.3 %   -1.0 %   -1.3 %     (already our strong spot)
  ```

  Doubling the transform is a wash on ratio but makes the weakness
  deeper, so it is not taken.

## v0.10.0 — near-lossless LUMM

The medical container can now trade a small, bounded error for a smaller
file. The bound is absolute and exact: no sample moves by more than the
requested amount.

**Added**

* `encode(..., max_error=N)` and `lum med encode --max-error N`: the
  encoder quantizes each canonical sample onto a `2N+1` grid **before**
  prediction, so the residual chain and the decoder stay exact and the
  error bound is independent of modular wrap-around.
* `lum_med_encode` takes the quantization step, the header stores it in
  `flags` bit 1 plus the packed word at offset 76 (`predictor | step << 8`),
  and `info()` reports it as `step`.
* The slice predictor stays available in the near-lossless mode (the
  quantized samples are what gets predicted).
* `tests/test_medical.py`: error-bound, native/reference parity, mode flag,
  verify and refusal checks (106 checks total).

**Changed**

* The SHA-256 content hash is skipped for `step > 1` containers: the decoded
  samples intentionally differ from the original. The CRC-32 chain still
  covers the file integrity.
* Float samples are refused in the near-lossless mode.

Measured (64x64 u8 noise): 4323 B lossless, 4092 B at error 1, 2977 B at
error 8.

## v0.9.0 — native region decoding

A region decode existed, but it ran entirely in Python. It is now part of the
native core as well.

**Added**

* `lum_med_decode_region` in `rust/lum_med.rs`: decodes a box of a volume
  natively. Only the tiles the box touches are decoded, the tiles needed for
  the volumetric context (the previous slice of the same tile when the
  `slice` predictor is used) are resolved recursively and cached, and the
  requested rows are reconstructed in place.
* `medical/native.py`: the binding, and the dispatch in `medical/codec.py`
  that uses it automatically when the shared library is present.

**Changed**

* The full decode and the region decode now share one tile decoder
  (`decode_tile_stream`), so the two paths cannot drift apart.
* `tests/test_medical.py`: the native region decoder is compared with the
  pure-Python reference (97 checks).

**Measured**

* 2 592 combinations of sample format, geometry, tile size, predictor and
  box shape: the native region output equals the pure-Python region output
  *and* the tight crop of the full decode — 0 differences.

---

## v0.8.0 — the cloud working set: C++ tools, command line, checksummed index

The `LUMC` container of v0.7.0 is now a complete working set: the codec was
there, but there was no way to look at the data. This release adds the tools
around it.

**Added**

* `cpp/lum_cloud.cpp`: a C++20 working-set layer for cloud cubes.
  * The `LUMX` raw cube format (32 byte header, band planar samples) with
    `lum_cloud_raw_write` / `lum_cloud_raw_read`.
  * `lum_cloud_band_stats`: minimum, maximum, mean, standard deviation and a
    256 bin histogram per band (Welford algorithm, canonical samples).
  * `lum_cloud_render_rgb`: false-colour composite of three bands with a
    window and a level per band.
  * `lum_cloud_interleave` / `lum_cloud_deinterleave`: band planar ↔ pixel
    interleaved conversion.
  * A built-in self test (`-DLUMEN_CLOUD_SELFTEST`, 17 checks).
* `lum cloud` command group: `encode`, `info`, `stats`, `render`, `decode`
  (the whole cube or a single band with `--scene` and `--band`).
* `lum/cloud.py`: the C++ bindings for the working-set operations.
* `docs/FORMAT-LUMC.md`: the full format specification.
* `rust/lum_cloud.rs`: a `#[cfg(test)]` unit test module (9 checks: CRC and
  SHA-256 reference vectors, the round trip, every predictor, every sample
  format, the damage and truncation rejections).
* `scripts/run_tests.sh`: runs every suite — the native Python suites, the
  pure-Python fallback suites and, with `LUMEN_BUILD_TESTS=1`, the Rust unit
  tests and the C++ self test.

**Changed**

* The tile index of the container now has its own CRC-32 (header offset 108).
  Until now the header CRC only covered bytes 0..88 and the index — which
  holds the tile offsets, lengths, methods and per tile checksums — had no
  checksum of its own. Every structure of the container now has one.
* `read_band` no longer decodes the whole cube twice: it decodes only the
  planes it needs, following the `TIME` / `BAND` context chain.
* `lum.cloud` is exported from the package root (`from lum import cloud`).

**Fixed**

* `lum_cloud_deinterleave` wrote its output in the interleaved order it was
  reading instead of the band planar order the contract promises; the output
  was a copy of the input.

---

## v0.7.0 — LUMC: the cloud container (Rust only)

A new container format for earth observation data: multi-band, multi-scene
cubes with per-band radiometric metadata and per-scene georeferencing.
The codec is implemented **in Rust only** (`rust/lum_cloud.rs`); the Python
package is a thin ctypes binding (`lum/cloud.py`) with no pure-Python
fallback, so there is exactly one implementation and no parity risk.

**Added**

* `LUMC` container: 192 byte header, band table (48 B/entry: name, wavelength,
  type, units, scale, offset, valid range), scene table (40 B/entry: timestamp,
  origin, pixel size), tile index (24 B/entry) and a SHA-256 content hash.
* Nine predictors, including two that cross the tile borders: `TIME` (the same
  band of the previous scene) and `BAND` (the same pixel of the previous band).
* Two entropy paths: a zig-zag escape code (one symbol per sample) for wide
  integers and byte planes with one adaptive model per plane otherwise.
* `lum cloud` Python binding: `encode`, `decode`, `info`, `read_band`
  (a single band without touching the rest of the cube).
* `lum/tests/test_cloud.py`: round trip over every sample format, geometry,
  method and predictor, band reads against the full decode, determinism,
  bit-flip fuzzing and the input guards.

**Fixed** (all found by the new test suite)

* `raw_of` always wrote four bytes per sample, so a 16 bit plane was twice as
  long as it should be and every sample after the first was shifted.
* Signed samples were written back in the canonical domain instead of their
  two's complement form (an offset of 32768 resp. 2^31).
* The LZ match distance was read back with its bytes swapped.
* The fixed predictor id was never written into the header, so a container
  that used the volumetric `TIME` predictor could not be decoded.
* The index entries carried wrong scene/band/tile labels.

---

## v0.6.0 — region decoding (LUMM v3)

### Added
- **`medical.decode_region(blob, x, y, z, width, height, depth)`**: decodes
  only the box of the volume that is asked for; the tiles outside are never
  read. The header, metadata, index, per-tile CRC-32 and the HMAC signature
  are still verified; the payload CRC-32 and the SHA-256 content hash are
  not, because both cover the whole payload and proving them would mean
  decoding everything.
- **CLI `med decode --region X,Y,Z,W,H,D`**: writes the samples of the
  requested box.

### Fixed
- **`med decode` did not write its output file.** The handler reported the
  sample count but never wrote the samples, so the command looked
  successful while producing nothing. The regression is covered by a test.

### Measured and rejected
- **Three-dimensional context modelling**: worth 1.04 % on a CT volume, not
  the 10-20 % that was expected - consecutive slices are nearly
  independent. The format was left alone.

## v0.5.0 — signed containers and provenance (LUMM v3)

### Added
- **Optional HMAC-SHA256 signature** (`medical/signature.py`). The 32 byte
  reserved area of the header (offset 112..144) now holds a keyed MAC that
  covers everything else in the file: the header fields, the metadata
  block, the tile index and the payload. A CRC or a plain hash proves that
  a file was not damaged; a keyed signature proves that the holder of the
  secret key produced it and that nobody touched it afterwards.
  `encode(..., sign_key=...)` signs a container, `decode(blob,
  verify_key=...)` refuses a container whose signature does not match, and
  `verify(blob, key)` reports the state (`unsigned`, `unverified`,
  `verified`). Unsigned containers keep working unchanged.
- **Provenance audit block** (`encode(..., audit=True)`): the metadata
  gains an `Audit` object with the encoder, the codec, the geometry and
  the SHA-256 of the source samples. It carries no timestamp unless one is
  asked for (`audit_timestamp=`), so the encoding stays byte-for-byte
  reproducible and the native and the pure-Python backend still agree.
- **CLI**: `med encode --sign-key FILE --audit [--audit-timestamp ISO]`,
  `med decode --verify-key FILE`, `med verify --key FILE`.

### Measured
- The signed and audited container is byte-identical between the native
  Rust core and the pure-Python reference (verified for three sample
  formats, two geometries and two data kinds).
- The signature costs nothing at decode time when no key is supplied: the
  file is simply reported as `unverified`.

### Fixed
- An empty signing key was silently ignored instead of being refused.

## v0.4.0 — context modelling (LUMM v3)

### Added
- **One adaptive model per byte plane** in the LUMM container (version 3,
  tile flag `0x08`). The high planes of a wide sample are long runs of zero
  bytes; a separate model per plane learns that immediately instead of
  sharing one model with the noisy low plane. The Python reference and the
  Rust core implement the same coder and produce byte-identical files.

### Measured
- 16-bit textured volume: 2 887 268 -> 2 709 400 bytes (**-6.2 %**).
- 64x64x8 textured volume: 50 900 -> 44 556 bytes (**-12.5 %**) for 16-bit
  and 67 376 -> 55 524 bytes (**-17.6 %**) for 32-bit.
- CT-like volumes with tiny residuals: unchanged (the escape coding already
  wins there), random noise: unchanged (+0.04 %).

### Fixed
- The LZ method no longer claims the context flag it does not use.

---

## v0.3.0 — the clinical update (LUMM v2)

### Added
- **DICOM compatible clinical metadata** (`medical/dicom.py`): `build_dicom_metadata`,
  `validate_dicom_metadata`, `is_dicom_compatible`, `rescale`, `window`, `photometric`,
  `describe`. The keyword set mirrors DICOM (PatientID, Modality, StudyInstanceUID,
  Rows/Columns/NumberOfFrames, BitsAllocated/BitsStored/HighBit, PixelRepresentation,
  PhotometricInterpretation, RescaleSlope/Intercept, WindowCenter/Width,
  PixelSpacing, SliceThickness, TransferSyntaxUID, SOPClassUID …).
- **Real DICOM Part 10 files** (`medical/dicomfile.py`): Explicit VR Little Endian
  reader and writer, 128 byte preamble + `DICM`, file meta group, uncompressed
  pixel data, per-modality SOP class. Any other transfer syntax is rejected
  instead of being misread.
- **`medical.verify(blob)`**: checks every integrity guarantee and reports the
  clinical problems of the metadata block (`dicom_problems`).
- **Third-party DICOM conformance**: the DICOM files are checked against
  pydicom in both directions - pydicom parses every file LUMM writes (five
  sample formats plus a multi-frame series, byte-identical pixel data and
  clinical attributes) and LUMM parses the files pydicom writes from scratch.
  The check lives in `tests/test_medical.py` and is skipped when pydicom is
  not installed.
- **`medical.generate_uid(seed)`**: standards-compliant `2.25.` UIDs; deterministic
  with a seed, unique without one.
- **LOCO-I / JPEG-LS MED predictor** (`PRED_MED = 8`), added to the adaptive row
  filter candidate list.
- **Window/level preview** with rescale calibration and MONOCHROME1 inversion
  (`medical/preview.py`, C++ `lum_med_preview`).
- **CLI `med` command group**: `encode --dicom --require-dicom`, `decode`, `info`,
  `dump` (full JSON document, `--metadata-only`), `verify`, `preview`, `to-dicom`,
  `from-dicom`.
- **Escape coding** in LUMM: one symbol per sample for 16/32 bit formats, with
  zig-zag mapping of small residuals and escape chunks for the rare large ones.
  Cut the file size of a 16-bit CT volume by 22 %.
- **Allocation-free LZ matcher** (Rust and Python): fixed hash table with
  `heads`/`prev` chains instead of a `HashMap` of deques.
- **Branch-free modular arithmetic** in the medical core (mask instead of a
  conditional).

### Changed
- The medical core was rewritten from scratch: the whole volume is coded in
  parallel (tiles and slices are independent), the tile preparation is shared
  between the RAW and the LZ candidate, and the losing candidate is abandoned as
  soon as it provably exceeds the winner's size.
- `medical/preview.py` applies the calibration first, then the window, then the
  photometric inversion.
- The C++ preview averages the channels (like the Python path) and truncates
  instead of rounding, so both backends emit the same bytes.

### Fixed
- `write_dicom` wrote the same tag twice (0020,0011) — duplicate elements are
  invalid in DICOM. Elements are now written in ascending tag order.
- `build_dicom_metadata` did not produce the ten standard attributes that the
  DICOM writer emits, so the metadata of a LUMM container and of the equivalent
  DICOM file were not the same set.
- `is_dicom_compatible()` ignored *contradictions* (geometry, pixel
  representation, photometric interpretation, HighBit, rescale slope, unusable
  window) and only looked for missing fields.
- `build_dicom_metadata` wrote an invalid window width of zero.
- `med dump` printed only the metadata block; the geometry was invisible.
- The LZ token limit was derived from the symbol count instead of the packed
  byte count, so 16 and 32 bit files could not be read back.
- Signed samples were decoded with a conversion that overflowed.
- The Rust candidate scorer used a fixed size array that did not match the
  number of predictors (panic in debug builds).

### Measured
| volume (u16) | encode | decode | ratio |
|---|---|---|---|
| 128×128×16 | 35.5 ms | 22.6 ms | 2.67x |
| 256×256×32 | 274.8 ms | 159.1 ms | 2.69x |
| 512×512×64 | 2 388.5 ms | 1 414.9 ms | 2.70x |

Against the pure-Python reference: 215x faster to encode, 113x faster to decode.
Compared with the first medical core: **7.5x** encode, **3.1x** decode, 22 %
smaller files.

---

## v0.2.0 — the speed update (LUMEN v3)

### Added
- **Whole-image native codec** (`rust/lum_image.rs`): `lum_encode_image`,
  `lum_encode_image_bytes`, `lum_decode_image`. Planning, tiling, the method
  candidates, the container header, the tile index and the CRC32 all happen in
  Rust, with one thread per tile.
- **Narrow residual packing** (container v3): a residual is stored in exactly the
  bits its alphabet needs (`((mod-1).bit_length()+7)//8` bytes) instead of
  rounding the alphabet up to a power of two.
- **Adaptive CDF model cache**: the last updated symbol is remembered, so a
  repeated symbol skips the Fenwick tree walk. A pure implementation change —
  the output bytes do not move.
- **Early abandonment of losing candidates** (bit budget): the RAW candidate
  defines a byte budget for the RLE and LZ candidates; a candidate that exceeds
  it is dropped, because the final size can only grow.

### Changed
- v1 and v2 containers still load: the decoder picks the packing width from the
  version byte.
- The planner measures its candidates in parallel.

### Measured
Real photo (640×360 RGB): encode 503 → 127 ms, decode 85 → 31 ms — 100x and
102x faster than the pure-Python path. Five real photos: 923 328 → 909 558
bytes (3.93x of raw), slightly ahead of PNG (3.85x).

---

## v0.1.0 — the first release (LUMEN v1 / v2)

### Added
- A stdlib-only lossless image codec: seven reversible predictors, a reversible
  YCoCg transform, an adaptive per-row filter, RAW/RLE/LZ methods and an
  adaptive range coder with a Fenwick CDF model.
- A native Rust core (`rust/lum_core.rs`) with a ctypes dispatch layer and a
  pure-Python fallback; an optional C++20 core for the adaptive row filters.
- Container v1 (32 byte header) and v2 (34 byte header, automatic colour
  transform, automatic row filter).
- A CLI (`encode`, `decode`, `inspect`, `verify`, `benchmark`) and a Python API
  (`encode`, `decode`, `encode_file`, `decode_file`, `decode_region`,
  `iter_tiles`, `encode_animation`).
- A verification suite of 246 checks (native) / 179 checks (pure Python).

### Measured
Five real photos: 1 015 034 bytes (3.52x of raw) against PNG's 928 511
(3.85x) — on par, without a single dependency.
