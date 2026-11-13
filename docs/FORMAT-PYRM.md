# PYRM — PYRA Medical container format (v1)

PYRM is the lossless container used for medical and scientific imaging: CT,
MR, ultrasound, microscopy, pathology slides. It is designed around three
requirements that matter in a clinical setting:

1. **No data loss, ever.** The container is *lossless* — decoded samples are
   bit-identical to the input samples. Nothing is quantised, windowed or
   rounded on the way in or out.
2. **Detectable corruption.** Every structural element is protected: the
   header carries its own CRC-32, the metadata block carries a CRC-32, every
   tile body carries a CRC-32, the whole payload carries a CRC-32 and the
   header additionally stores the **SHA-256 of the raw sample bytes**. A
   decoder that finds any mismatch refuses the file instead of handing back
   plausible-looking but wrong pixels.
3. **Fast.** The entropy coder, the predictors, the LZ77 matcher and the
   container assembly are implemented in Rust (and partly C++); a pure
   Python reference implementation exists and produces *byte-identical*
   files, which is what the test suite asserts.

## File layout

```
+--------------------+  offset 0
|  header (144 B)    |
+--------------------+  offset 144
|  metadata block    |  UTF-8 JSON, CRC-32 protected
+--------------------+
|  tile index        |  16 bytes per tile
+--------------------+
|  tile bodies       |  the compressed samples
+--------------------+
```

### Header (144 bytes, little-endian)

| offset | size | field | meaning |
|-------:|-----:|-------|---------|
| 0   | 8  | magic | `PYRM\r\n\x1a\n` |
| 8   | 4  | version | 2 (1 is still readable) |
| 12  | 4  | flags | reserved, 0 |
| 16  | 4  | width | samples per row |
| 20  | 4  | height | rows per slice |
| 24  | 4  | depth | number of slices (1 for a 2D image) |
| 28  | 4  | channels | samples per pixel |
| 32  | 4  | sample_format | 0=u8 1=u16 2=u32 3=i16 4=i32 5=f32 |
| 36  | 4  | bits | bits per sample (8, 16, 32) |
| 40  | 4  | tile_size | tile edge in samples |
| 44  | 4  | num_tiles | total tiles (all slices) |
| 48  | 4  | index_offset | byte offset of the tile index |
| 52  | 4  | index_size | tile index size in bytes |
| 56  | 4  | meta_offset | byte offset of the metadata block |
| 60  | 4  | meta_size | metadata size in bytes |
| 64  | 4  | meta_crc | CRC-32 of the metadata block |
| 68  | 4  | payload_crc | CRC-32 of all tile bodies |
| 72  | 4  | header_crc | CRC-32 of bytes 0..72 |
| 76  | 4  | predictor | fixed predictor id (used when a tile has no row table) |
| 80  | 32 | sha256 | SHA-256 of the raw sample bytes |
| 112 | 32 | reserved | zero |

### Tile index

One 16 byte record per tile:

| offset | size | field |
|-------:|-----:|-------|
| 0  | 4 | offset of the tile body (absolute) |
| 4  | 4 | length of the tile body |
| 8  | 1 | method (0 = RAW, 1 = LZ) |
| 9  | 1 | flags (0x01 = byte-plane split, 0x02 = adaptive row filter, 0x04 = escape coding) |
| 10 | 2 | filter table size in bytes |
| 12 | 4 | CRC-32 of the tile body |

Tiles are ordered slice by slice, and inside a slice in raster order.

### Tile body

```
[ row filter table ]   present only when the 0x02 flag is set
[ entropy stream ]
```

The **row filter table** stores one predictor id per row (4 bits, MSB first,
`ceil(rows/2)` bytes). The **entropy stream** is a range-coded symbol stream
that carries the residual symbols of the tile.

## Code units

Every sample is turned into one or more *code values* before prediction:

| sample format | code values | value bytes | residual modulus |
|---------------|-------------|-------------|------------------|
| u8            | 1 per sample | 1 | 256 |
| u16           | 1 per sample | 2 | 65536 |
| u32           | 1 per sample | 4 | 2^32 |
| i16           | 1 per sample | 2 | 65536 (signed values are shifted by +32768) |
| i32           | 1 per sample | 4 | 2^32 (signed values are shifted by +2^31) |
| f32           | 4 per sample | 1 | 256 (four byte planes) |

Signed samples are mapped into the unsigned code domain by adding
`modulus/2`, which keeps the residual alphabet a plain power of two and lets
the entropy coder use a fixed, cheap model.

Float samples are *shuffled into their four byte planes* (low plane first)
before coding, and are never predicted. The entropy coder then sees smooth
byte streams instead of the random bit pattern of a float mantissa, which
compresses far better and stays exactly lossless.

### Escape coding (16 and 32 bit samples, flag 0x04)

Coding one symbol per residual *byte* would need two symbols for a 16 bit
sample, although more than 99 % of the residuals of real medical data fit
into +-63. Wide samples are therefore coded with **one symbol per sample**:

* the residual is mapped into the signed range `[-modulus/2, modulus/2)`;
* a residual of at most +-63 is zig-zag encoded into the alphabet `0..127`
  (`0` -> 0, `1` -> -1, `2` -> 1, ...);
* a larger residual escapes: the marker `128` is followed by
  `ceil(bits / 7)` symbols that carry the two's complement bit pattern of
  the value in 7 bit chunks (three chunks for 16 bit samples, five for 32
  bit ones).

The entropy alphabet of such a tile is therefore 129 symbols, and the
escape rate of typical data is below one percent. The decoder rebuilds the
exact same residual values, so the tile stays bit-exact lossless.

The encoder measures the escape rate of every tile and only uses this coding
when at most a quarter of the residuals escape; otherwise it falls back to
the byte plane coding, which is cheaper for noisy data. The tile flag
records which of the two layouts was used.

## Predictors

| id | name | prediction |
|---:|------|------------|
| 0 | none | 0 |
| 1 | left | the sample to the left |
| 2 | up | the sample above |
| 3 | average | `(left + up) >> 1` |
| 4 | paeth | PNG Paeth predictor |
| 5 | gradient | `left + up - upper_left` |
| 6 | median | JPEG-LS / LOCO-I median predictor |
| 7 | slice | the same sample in the previous slice (3D only) |
| 8 | med | the median edge detector of LOCO-I / JPEG-LS |

The prediction context is **tile local**: a tile never borrows a sample from
a neighbouring tile, so every tile can be decoded independently (and the
encoder and decoder can run tiles in parallel). The volumetric predictor
(`slice`) is the only exception — it reads the same sample from the previous
slice.

When the encoder is asked for `predictor = auto`, every row picks its own
predictor: the candidate set is scored with the *circular* distance of the
residuals from zero (`min(r, modulus - r)`, because the residual alphabet is
modular) and the cheapest candidate wins. Ties keep the first candidate, so
the choice is deterministic across implementations.

## Clinical semantics (DICOM compatible)

A lossless container is not enough for clinical data: the *meaning* of the
stored numbers has to travel with them. The metadata block is a UTF-8 JSON
object whose keys are the DICOM keywords, so it can be mapped onto a real
DICOM data set without renaming anything.

| attribute | meaning |
|-----------|---------|
| `PatientID`, `PatientName` | the (anonymised) patient |
| `StudyInstanceUID`, `SeriesInstanceUID`, `SOPInstanceUID` | the identity of the study, the series and the instance |
| `Modality`, `StudyDate` | the acquisition method and the date |
| `SliceThickness`, `PixelSpacing` | the geometry of the acquisition |
| `RescaleSlope`, `RescaleIntercept` | the calibration: `value = slope * stored + intercept` |
| `WindowCenter`, `WindowWidth` | the display window, in the calibrated domain |
| `PhotometricInterpretation` | `MONOCHROME1`, `MONOCHROME2` or `RGB` |
| `BitsStored`, `BitsAllocated`, `PixelRepresentation` | the sample description (0 = unsigned, 1 = signed) |
| `SamplesPerPixel`, `Rows`, `Columns`, `NumberOfFrames` | the geometry, mirrored from the header |
| `TransferSyntaxUID` | the transfer syntax the data set claims |

Two of these deserve a warning:

* **MONOCHROME1** maps the *lowest* value to white. It is the inverted
  convention used by some modalities, and a preview that ignores the flag
  shows a study with the wrong brightness. The preview helper honours it.
* **RescaleSlope / RescaleIntercept** are not decoration: without them the
  stored numbers are meaningless (a CT stores Hounsfield units shifted by
  -1024, for instance). The display window is defined in the calibrated
  domain, so the calibration is applied *before* the window.

`pyra.medical.build_dicom_metadata()` builds a complete and consistent
block (the geometry dependent attributes are derived), and
`pyra.medical.validate_dicom_metadata()` checks a block against the image
geometry: a contradicting `Columns`, an impossible `PixelRepresentation`,
an unknown photometric interpretation, a missing patient identifier or a
non-positive `WindowWidth` are all refused. The encoder takes the same
check with `require_dicom=True`, so a clinical series with inconsistent
metadata is rejected instead of being stored silently.

## Entropy coding

A binary range coder with an adaptive per-symbol model (a Fenwick tree over
the symbol frequencies). The coder is the same one the `PYRA` image format
uses, so the two formats share exactly one implementation.

* `METHOD_RAW` (0): the packed residual bytes are coded directly (256
  symbols).
* `METHOD_LZ` (1): the packed residual bytes are LZ77-tokenised first
  (literal bytes, plus a match token with three extra symbols) and the token
  stream is coded (258 symbols) with an end-of-stream marker.

With `method = auto` the encoder codes both candidates and keeps the smaller
one per tile.

## Integrity guarantees

A decoder checks, in this order:

1. the magic bytes and the version,
2. the header CRC-32 (bytes 0..72),
3. the tile index size against the tile count,
4. the metadata CRC-32,
5. the CRC-32 of every tile body,
6. the CRC-32 of the whole payload,
7. the **SHA-256** of the decoded samples against the digest stored in the
   header.

Any mismatch raises `CorruptedFileError`. A decoder never returns partially
decoded or unverified data.

## Python API

```python
from pyra.medical import (encode, decode, info, verify, build_dicom_metadata,
                          preview_from_metadata)

meta = build_dicom_metadata(512, 512, 64, 1, 1,      # u16
                            patient_id="ANON-1", modality="CT",
                            study_uid="1.2.3", series_uid="1.2.3.4",
                            slice_thickness=1.0,
                            rescale_slope=1.0, rescale_intercept=-1024.0,
                            window_center=40.0, window_width=400.0)

blob = encode(samples, 512, 512, 64, 1, 1,
              metadata=meta, require_dicom=True,
              predictor=-1, method=-1)  # -1 = choose automatically

metadata, samples_back = decode(blob)
print(info(blob))
print(verify(blob))            # every integrity check, without trusting it
png_bytes = preview_from_metadata(samples_back, 512, 512, 64, 1, 1,
                                   metadata, slice=30)
```

## Command line

```bash
python3 -m pyra med encode raw.bin volume.pyrm --width 512 --height 512 \
        --depth 64 --format u16 --meta patient=anon --meta modality=CT
python3 -m pyra med info volume.pyrm        # header + clinical attributes
python3 -m pyra med dump volume.pyrm        # the metadata block as JSON
python3 -m pyra med verify volume.pyrm      # every integrity guarantee
python3 -m pyra med decode volume.pyrm out.bin
python3 -m pyra med preview volume.pyrm slice.pgm --slice 12
```

`--window` and `--level` are optional for the preview: without them the
window of the metadata is used.

### Building a clinically complete container

`--dicom` derives every geometry dependent attribute (Rows, Columns,
NumberOfFrames, SamplesPerPixel, BitsAllocated, BitsStored,
PixelRepresentation, PhotometricInterpretation) from the image itself and
fills the clinical attributes from the options:

```bash
python3 -m pyra med encode raw.bin volume.pyrm --width 512 --height 512 \
        --depth 64 --format u16 --dicom --require-dicom \
        --patient-id ANON-001 --patient-name "Doe^Jane" --modality CT \
        --study-uid 1.2.840.113619.2.1 --series-uid 1.2.840.113619.2.1.4 \
        --sop-uid 1.2.840.113619.2.1.4.9 --study-date 20260113 \
        --slice-thickness 1.5 --rescale-slope 1 --rescale-intercept -1024 \
        --window-center 40 --window-width 400 --photometric mono2
```

`--require-dicom` validates the metadata against the geometry before
anything is written: a missing identifier, a contradicting geometry, a
zero rescale slope or a non-positive window width aborts the encoding with
a non-zero exit code and no file. `--meta KEY=VALUE` entries override the
derived values, so a free form attribute can always be added.

## Error classes

* `MedicalError` — base class of the format errors.
* `CorruptedFileError` — the file is damaged or not a PYRM container.
* `UnsupportedFormatError` — unknown sample format, method or predictor.
* `MedicalMetadataError` — the metadata block is incomplete or contradicts
  the image geometry.

## Version history

* **v2** — the DICOM compatible clinical metadata layer (calibration,
  photometric interpretation, display window, sample description) and the
  LOCO-I median edge detector as an eighth predictor.
* **v1** — first release: 144 byte header, tile index, metadata block,
  adaptive per-row predictors, byte-plane split for wide samples, RAW/LZ
  entropy methods, CRC-32 everywhere plus a SHA-256 content hash.

A v1 container still decodes: the version byte selects the accepted
predictor ids, the header layout is identical.
