# Changelog

All notable changes to PYRA and PYRM. The format versions are independent:
`PYRA` container v1 → v2 → v3, `PYRM` container v1 → v2.

The project keeps **bit-exact parity** between the native Rust/C++ backends and the
pure-Python reference: both produce byte-identical containers.

---

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
