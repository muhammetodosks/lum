# Changelog

All notable changes to PYRA and PYRM. The format versions are independent:
`PYRA` container v1 → v2 → v3 (lossless) and v5 (lossy), `PYRM` container
v1 → v2.

The project keeps **bit-exact parity** between the native Rust/C++ backends and the
pure-Python reference: both produce byte-identical containers.

---

## v0.11.0 — lossy codec (PYRA v5)

PYRA gains a photographic mode. Until now the container was lossless only: it
competed with PNG, had no quality knob, and was useless for camera output. The
new mode is a full transform codec — YCbCr, 8x8 DCT, perceptual quantisation and
a context-modelled binary range coder — written in the same dependency-free Rust
core as the rest of the project.

**Added**

* `encode --quality Q` (1..100) and `encode --subsampling 4:4:4|4:2:0`. `auto`
  (the default, quality 0) keeps the lossless v3 path, so nothing changes for
  existing callers.
* Container version 5: 36-byte header (`quality`, `mode`, `plane_count`), then
  `u32 length + stream` per plane, then CRC-32 over the whole file. Version 4
  files are rejected rather than decoded: the entropy model changed and they
  would decode to the wrong pixels.
* `pyra.lossy` ctypes binding (`encode`, `decode`, `info`) and a
  `PyraCodec.decode` dispatch for v5.
* `pyra inspect` reports quality, chroma format and plane count for v5 files.
* `tests/test_lossy.py`: 63 checks covering round-trip accuracy, monotone
  distortion in quality, header validation, corruption rejection and the
  codec dispatch.

**Measured**

Mean bytes per megapixel at matched RGB PSNR over six 800 px backgrounds,
compared with JPEG XL (`libjxl --effort 7`, distance swept over -1..15):

| PSNR | PYRA | as % of JXL |
|------|------|-------------|
| 30 dB | 18.0 KB | 96.8 % |
| 34 dB | 36.1 KB | 98.4 % |
| 38 dB | 69.7 KB | 117.3 % |
| 42 dB | 124.3 KB | 114.4 % |
| 46 dB | 204.8 KB | 101.1 % |

Encoding runs at ~19 MP/s single-threaded, roughly ten times the JXL reference
in the same harness.

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

**Measured and rejected**

* 4:2:0 chroma at matched RGB PSNR was always larger than 4:4:4.
* Flat high-frequency shaping, chroma stride, a second-order tilt term, and a
  finer 9-state neighbour context all measured neutral or worse.
* A within-block energy context for significance: neutral to negative.

## v0.10.0 — near-lossless PYRM

The medical container can now trade a small, bounded error for a smaller
file. The bound is absolute and exact: no sample moves by more than the
requested amount.

**Added**

* `encode(..., max_error=N)` and `pyra med encode --max-error N`: the
  encoder quantizes each canonical sample onto a `2N+1` grid **before**
  prediction, so the residual chain and the decoder stay exact and the
  error bound is independent of modular wrap-around.
* `pyra_med_encode` takes the quantization step, the header stores it in
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

* `pyra_med_decode_region` in `rust/pyra_med.rs`: decodes a box of a volume
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

The `PYRC` container of v0.7.0 is now a complete working set: the codec was
there, but there was no way to look at the data. This release adds the tools
around it.

**Added**

* `cpp/pyra_cloud.cpp`: a C++20 working-set layer for cloud cubes.
  * The `PYRX` raw cube format (32 byte header, band planar samples) with
    `pyra_cloud_raw_write` / `pyra_cloud_raw_read`.
  * `pyra_cloud_band_stats`: minimum, maximum, mean, standard deviation and a
    256 bin histogram per band (Welford algorithm, canonical samples).
  * `pyra_cloud_render_rgb`: false-colour composite of three bands with a
    window and a level per band.
  * `pyra_cloud_interleave` / `pyra_cloud_deinterleave`: band planar ↔ pixel
    interleaved conversion.
  * A built-in self test (`-DPYRA_CLOUD_SELFTEST`, 17 checks).
* `pyra cloud` command group: `encode`, `info`, `stats`, `render`, `decode`
  (the whole cube or a single band with `--scene` and `--band`).
* `pyra/cloud.py`: the C++ bindings for the working-set operations.
* `docs/FORMAT-PYRC.md`: the full format specification.
* `rust/pyra_cloud.rs`: a `#[cfg(test)]` unit test module (9 checks: CRC and
  SHA-256 reference vectors, the round trip, every predictor, every sample
  format, the damage and truncation rejections).
* `scripts/run_tests.sh`: runs every suite — the native Python suites, the
  pure-Python fallback suites and, with `PYRA_BUILD_TESTS=1`, the Rust unit
  tests and the C++ self test.

**Changed**

* The tile index of the container now has its own CRC-32 (header offset 108).
  Until now the header CRC only covered bytes 0..88 and the index — which
  holds the tile offsets, lengths, methods and per tile checksums — had no
  checksum of its own. Every structure of the container now has one.
* `read_band` no longer decodes the whole cube twice: it decodes only the
  planes it needs, following the `TIME` / `BAND` context chain.
* `pyra.cloud` is exported from the package root (`from pyra import cloud`).

**Fixed**

* `pyra_cloud_deinterleave` wrote its output in the interleaved order it was
  reading instead of the band planar order the contract promises; the output
  was a copy of the input.

---

## v0.7.0 — PYRC: the cloud container (Rust only)

A new container format for earth observation data: multi-band, multi-scene
cubes with per-band radiometric metadata and per-scene georeferencing.
The codec is implemented **in Rust only** (`rust/pyra_cloud.rs`); the Python
package is a thin ctypes binding (`pyra/cloud.py`) with no pure-Python
fallback, so there is exactly one implementation and no parity risk.

**Added**

* `PYRC` container: 192 byte header, band table (48 B/entry: name, wavelength,
  type, units, scale, offset, valid range), scene table (40 B/entry: timestamp,
  origin, pixel size), tile index (24 B/entry) and a SHA-256 content hash.
* Nine predictors, including two that cross the tile borders: `TIME` (the same
  band of the previous scene) and `BAND` (the same pixel of the previous band).
* Two entropy paths: a zig-zag escape code (one symbol per sample) for wide
  integers and byte planes with one adaptive model per plane otherwise.
* `pyra cloud` Python binding: `encode`, `decode`, `info`, `read_band`
  (a single band without touching the rest of the cube).
* `pyra/tests/test_cloud.py`: round trip over every sample format, geometry,
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

## v0.6.0 — region decoding (PYRM v3)

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

## v0.5.0 — signed containers and provenance (PYRM v3)

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

## v0.4.0 — context modelling (PYRM v3)

### Added
- **One adaptive model per byte plane** in the PYRM container (version 3,
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

## v0.3.0 — the clinical update (PYRM v2)

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
  pydicom in both directions - pydicom parses every file PYRM writes (five
  sample formats plus a multi-frame series, byte-identical pixel data and
  clinical attributes) and PYRM parses the files pydicom writes from scratch.
  The check lives in `tests/test_medical.py` and is skipped when pydicom is
  not installed.
- **`medical.generate_uid(seed)`**: standards-compliant `2.25.` UIDs; deterministic
  with a seed, unique without one.
- **LOCO-I / JPEG-LS MED predictor** (`PRED_MED = 8`), added to the adaptive row
  filter candidate list.
- **Window/level preview** with rescale calibration and MONOCHROME1 inversion
  (`medical/preview.py`, C++ `pyra_med_preview`).
- **CLI `med` command group**: `encode --dicom --require-dicom`, `decode`, `info`,
  `dump` (full JSON document, `--metadata-only`), `verify`, `preview`, `to-dicom`,
  `from-dicom`.
- **Escape coding** in PYRM: one symbol per sample for 16/32 bit formats, with
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
  DICOM writer emits, so the metadata of a PYRM container and of the equivalent
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

## v0.2.0 — the speed update (PYRA v3)

### Added
- **Whole-image native codec** (`rust/pyra_image.rs`): `pyra_encode_image`,
  `pyra_encode_image_bytes`, `pyra_decode_image`. Planning, tiling, the method
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

## v0.1.0 — the first release (PYRA v1 / v2)

### Added
- A stdlib-only lossless image codec: seven reversible predictors, a reversible
  YCoCg transform, an adaptive per-row filter, RAW/RLE/LZ methods and an
  adaptive range coder with a Fenwick CDF model.
- A native Rust core (`rust/pyra_core.rs`) with a ctypes dispatch layer and a
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
