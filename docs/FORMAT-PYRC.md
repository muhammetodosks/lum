# PYRC — PYRA Cloud container format (v1)

PYRC is the container of the cloud profile of PYRA: it stores **multi-band,
multi-scene** observations (satellite imagery, radar composites, cloud
products) losslessly, with the metadata that makes such a cube usable
later on — the wavelength of a band, the units it is measured in, the
scale and offset that turn a stored value into a physical quantity, the
georeferencing of a scene and the time it was acquired.

The whole format is implemented **once**, in Rust
(`rust/pyra_cloud.rs`). The C++ layer (`cpp/pyra_cloud.cpp`) adds the
working-set helpers — raw cube I/O, per band statistics, false colour
rendering and band interleaving — and the Python package is a thin
`ctypes` binding (`pyra/cloud.py`) over both. There is deliberately **no
pure-Python fallback**: a second implementation would be a second
definition of the format, and the format is only worth having when it
has exactly one.

## Design goals

* **Lossless.** The decoded samples are byte-identical to the input
  samples. Nothing is quantised, scaled or rounded on the way in or out.
* **Verifiable.** Every structural element carries a checksum: the
  header, the metadata block, the tile index, every tile body, the whole
  payload — and the header additionally stores the **SHA-256 of the raw
  samples**, so a mixed-up cube is detected immediately.
* **Fast.** Tiles are coded in parallel, and the whole cube is encoded
  or decoded in a single native call.
* **Domain aware.** Bands carry their physical description, scenes carry
  their georeferencing and their acquisition time, and the predictor
  can look across scenes (time) and across bands (band), which is where
  the redundancy of real observations lives.

## File layout

```
+-------------------------+  offset 0
|  header (192 bytes)     |
+-------------------------+
|  band table             |  48 bytes per band
+-------------------------+
|  scene table            |  40 bytes per scene
+-------------------------+
|  metadata block         |  UTF-8 JSON
+-------------------------+
|  tile index             |  24 bytes per tile
+-------------------------+
|  tile bodies            |  the compressed samples
+-------------------------+
```

### Header (192 bytes, little-endian)

| offset | size | field | meaning |
|-------:|-----:|-------|---------|
| 0   | 8  | magic | `PYRC\r\n\x1a\n` |
| 8   | 4  | version | 1 |
| 12  | 4  | flags | reserved, 0 |
| 16  | 4  | width | samples per row |
| 20  | 4  | height | rows per scene |
| 24  | 4  | bands | number of bands |
| 28  | 4  | scenes | number of scenes (time steps) |
| 32  | 4  | sample_format | 0=u8 1=u16 2=u32 3=i16 4=i32 5=f32 |
| 36  | 4  | bits | bits per sample (8, 16, 32) |
| 40  | 4  | tile_size | tile edge in samples |
| 44  | 4  | tiles_x | tiles per row |
| 48  | 4  | tiles_y | tiles per column |
| 52  | 4  | num_entries | number of index entries (= bands × scenes × tiles) |
| 56  | 4  | band_offset | byte offset of the band table |
| 60  | 4  | band_size | band table size in bytes |
| 64  | 4  | scene_offset | byte offset of the scene table |
| 68  | 4  | scene_size | scene table size in bytes |
| 72  | 4  | meta_offset | byte offset of the metadata block |
| 76  | 4  | meta_size | metadata size in bytes |
| 80  | 4  | meta_crc | CRC-32 of the metadata block |
| 84  | 4  | payload_crc | CRC-32 of all tile bodies |
| 88  | 4  | header_crc | CRC-32 of bytes 0..88 |
| 92  | 4  | index_offset | byte offset of the tile index |
| 96  | 4  | index_size | tile index size in bytes |
| 100 | 4  | payload_offset | byte offset of the first tile body |
| 104 | 4  | predictor | predictor id used by the encoder (0 = none) |
| 108 | 4  | index_crc | CRC-32 of the tile index |
| 112 | 32 | sha256 | SHA-256 of the raw sample bytes |
| 144 | 32 | signature | reserved for an HMAC-SHA256 signature |
| 176 | 16 | reserved | zero |

The header CRC covers bytes 0..88, that is everything up to (but not
including) the CRC field itself. Every later structure — metadata, tile
index, tile bodies, payload — has its own checksum, so a corrupted
structure is always localised instead of producing garbage samples.

### Band table (48 bytes per band)

| offset | size | field | meaning |
|-------:|-----:|-------|---------|
| 0  | 16 | name | UTF-8 band name, zero padded |
| 16 | 8  | wavelength_nm | `f64`, centre wavelength in nanometres |
| 24 | 4  | type | band type (see below) |
| 28 | 4  | units | physical units (see below) |
| 32 | 4  | scale | `f32`, stored = physical / scale |
| 36 | 4  | offset | `f32`, physical = stored × scale + offset |
| 40 | 4  | valid_min | `f32`, lowest physical value |
| 44 | 4  | valid_max | `f32`, highest physical value |

Band types: `reflectance`, `radiance`, `brightness_temperature`,
`quality_flag`, `classification`, `precipitation`, `albedo`,
`elevation`.

Units: `reflectance`, `radiance`, `kelvin`, `percent`, `metre`,
`millimetre`, `none`.

### Scene table (40 bytes per scene)

| offset | size | field | meaning |
|-------:|-----:|-------|---------|
| 0  | 8 | timestamp | Unix seconds of the acquisition |
| 8  | 8 | lon0 | `f64`, longitude of the first column |
| 16 | 8 | lat0 | `f64`, latitude of the first row |
| 24 | 8 | pixel_size_x | `f64`, degrees per pixel along a row |
| 32 | 8 | pixel_size_y | `f64`, degrees per pixel along a column |

### Tile index (24 bytes per tile)

| offset | size | field |
|-------:|-----:|-------|
| 0  | 4 | offset of the tile body (absolute) |
| 4  | 4 | length of the tile body |
| 8  | 1 | method (0 = RAW, 1 = LZ) |
| 9  | 1 | flags (see the tile body section) |
| 10 | 2 | size of the row filter table in bytes |
| 12 | 4 | CRC-32 of the tile body |
| 16 | 2 | scene index |
| 18 | 2 | band index |
| 20 | 2 | tile index inside the band |
| 22 | 4 | reserved |

The tiles are stored **scene by scene, band by band, tile by tile**:
the entry with the lowest index belongs to scene 0, band 0, tile 0.

### Tile body

```
[ row filter table ]   present only when the 0x02 flag is set
[ entropy stream ]
```

The **row filter table** stores one predictor id per row (4 bits, MSB
first, `ceil(rows/2)` bytes). The **entropy stream** is a range-coded
symbol stream that carries the residual bytes of the tile.

Tile flags:

| flag | meaning |
|------|---------|
| 0x01 | the residual bytes were split into byte planes |
| 0x02 | the body starts with a row filter table |
| 0x04 | the residuals are escape coded (zig-zag) |
| 0x08 | the entropy stream uses one adaptive model per byte plane |

## The data model

```
sample[scene][band][row][col]
```

The samples of a scene are stored **band by band** (band planar): the
whole plane of band 0, then the whole plane of band 1, and so on. A
plane is `width * height` little-endian samples. Float samples are
stored as IEEE-754 binary32, also little-endian.

## Sample formats and code units

| sample format | bytes | canonical domain | residual modulus |
|---------------|------:|------------------|------------------|
| u8            | 1 | 0..255            | 256 |
| u16           | 2 | 0..65535          | 65536 |
| u32           | 2 | 0..2³²-1          | 2³² |
| i16           | 2 | signed, shifted by +32768 | 65536 |
| i32           | 4 | signed, shifted by +2³¹   | 2³² |
| f32           | 4 | four byte planes  | 256 per plane |

Signed samples are mapped into the unsigned domain by adding
`modulus/2`, which keeps the residual alphabet a plain power of two.
Float samples are never predicted; they are shuffled into their four
byte planes instead, so the entropy coder sees smooth byte streams
instead of the random bit pattern of a mantissa — and the data stays
exactly lossless.

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
| 7 | time | the same pixel in the **previous scene** |
| 8 | band | the same pixel in the **previous band** |

The spatial predictors use a **tile local** context: a tile never
borrows a sample from its neighbour, so every tile can be decoded on
its own. The two volumetric predictors are the exception — they read the
same pixel of the previous scene or of the previous band, which is why
the encoder walks the cube scene by scene and band by band.

With `predictor = -1` (the default) the encoder measures the candidates
on the real data and keeps the cheapest one per row. The score is the
circular distance of the residuals from zero (`min(r, modulus - r)`,
because the residual alphabet is modular); ties keep the first
candidate, so the choice is deterministic.

## Entropy coding

A binary range coder with an adaptive per-symbol model (a Fenwick tree
over the symbol frequencies) — the same coder the PYRA image format and
the PYRM medical format use, so all three formats share exactly one
implementation.

Two residual encodings are possible, and the encoder picks the smaller
one per tile:

* **Escape coding** (flag 0x04) — for integer formats whose residuals
  are usually small. The residual is centred (`r > modulus/2` becomes
  `r - modulus`), and a centred value in `[-63, 63]` is written as a
  single zig-zag symbol; anything larger is written as an escape
  symbol followed by 7-bit chunks. One symbol per sample is the common
  case, which is exactly what makes the coder fast.
* **Byte planes** (flag 0x01 + 0x08) — the residual bytes are split
  into byte planes (low plane first) and each plane gets **its own
  adaptive model**. The high planes of a wide sample turn into long
  runs of zero bytes, and a separate model per plane encodes that much
  better than one shared model.

With `method = -1` (the default) the encoder also compares the plain
stream against an LZ77 coded stream and keeps the smaller one. The LZ
candidate is abandoned as soon as it grows past the size of the plain
candidate, which cannot change the outcome but saves a lot of work.

## Integrity guarantees

A decoder checks, in this order:

1. the magic bytes and the version,
2. the header CRC-32 (bytes 0..88),
3. the tile index size against the tile count,
4. the metadata CRC-32,
5. the tile index CRC-32,
6. the CRC-32 of every tile body,
7. the CRC-32 of the whole payload,
8. the **SHA-256** of the decoded samples against the digest stored in
   the header.

Any mismatch raises `CloudCorruptedError`. A decoder never returns
partially decoded or unverified data.

## The PYRX raw cube format

The C++ layer reads and writes a *raw* cube — the samples without any
compression — so that a large observation can be moved around and
inspected without a codec round trip. The format is deliberately
trivial:

```
+-------------------------------+
|  magic "PYRX"          4 bytes|
|  version               1 byte |
|  sample_format         1 byte |
|  reserved              2 bytes|
|  width                 4 bytes|
|  height                4 bytes|
|  bands                 4 bytes|
|  scenes                4 bytes|
|  sample_bytes          4 bytes|
+-------------------------------+  32 bytes
|  samples (scene major, band planar) |
+-------------------------------+
```

All integers are little-endian. The samples follow the header exactly
as they would inside a PYRC container.

## Python API

```python
from pyra import cloud

blob = cloud.encode(samples, width=512, height=512, bands=12, scenes=4,
                    sample_format=cloud.SFMT_U16,
                    band_meta=[{"name": "B01", "wavelength_nm": 443.0,
                                "type": 0, "units": 0, "scale": 0.0001,
                                "offset": 0.0, "valid_min": 0.0,
                                "valid_max": 1.0}],
                    scene_meta=[{"timestamp": 1700000000, "lon0": 12.5,
                                 "lat0": 41.9, "pixel_size_x": 0.0003,
                                 "pixel_size_y": 0.0003}],
                    metadata={"satellite": "ANON"}, predictor=-1, method=-1)

info, samples_back = cloud.decode(blob)
band = cloud.read_band(blob, scene=2, band=5)     # only that plane
```

The C++ helpers are reached through the same module:

```python
cloud.raw_write("cube.pyrx", samples, 512, 512, 12, 4, cloud.SFMT_U16)
samples, w, h, bands, scenes, fmt = cloud.raw_read("cube.pyrx")
stats = cloud.band_stats(samples, w, h, bands, scenes, fmt, band=3)
rgb = cloud.render_rgb(samples, w, h, bands, scenes, fmt,
                        scene=0, band_r=3, band_g=1, band_b=0)
```

## Command line

```bash
# encode a raw cube (PYRX) into a PYRC container
python3 -m pyra cloud encode cube.pyrx cube.pyrc \
        --width 512 --height 512 --bands 12 --scenes 4 --format u16 \
        --band-name B01 --band-wavelength 443 --band-type reflectance \
        --scene-time 1700000000 --lon0 12.5 --lat0 41.9 \
        --meta satellite=ANON

python3 -m pyra cloud info cube.pyrc
python3 -m pyra cloud stats cube.pyrc --band 3 --histogram
python3 -m pyra cloud render cube.pyrc preview.ppm --scene 0 --red 3 --green 1 --blue 0
python3 -m pyra cloud decode cube.pyrc out.pyrx
python3 -m pyra cloud decode cube.pyrc band.pyrx --scene 1 --band 5
```

## Error classes

* `CloudError` — base class of the cloud profile errors.
* `UnsupportedFormatError` — unknown sample format, method or predictor.
* `CloudCorruptedError` — the container is damaged or not a PYRC file
  (it is both a `CloudError` and a `CorruptedFileError`).

## Native API

```c
int pyra_cloud_encode(const uint8_t* samples, size_t len,
                      uint32_t width, uint32_t height, uint32_t bands,
                      uint32_t scenes, uint32_t sample_format,
                      const uint8_t* band_table, size_t band_table_len,
                      const uint8_t* scene_table, size_t scene_table_len,
                      const uint8_t* meta, size_t meta_len,
                      size_t tile_size, int32_t predictor, int32_t method,
                      uint8_t** out_ptr, size_t* out_len);

int pyra_cloud_decode(const uint8_t* data, size_t len,
                      uint8_t** out_ptr, size_t* out_len, int32_t* meta_out);
int pyra_cloud_info(const uint8_t* data, size_t len, int32_t* meta_out);
int pyra_cloud_read_band(const uint8_t* data, size_t len, uint32_t scene,
                         uint32_t band, uint8_t** out_ptr, size_t* out_len);
```

`meta_out` receives 21 integers: version, flags, width, height, bands,
scenes, sample_format, bits, tile_size, tiles_x, tiles_y, num_entries,
predictor, band_offset, band_size, scene_offset, scene_size,
meta_offset, meta_size, index_offset, index_size.

`band_table_len` must be exactly `bands * 48` and `scene_table_len`
exactly `scenes * 40`; anything else is rejected.

Buffers returned by the encoder are released with `pyra_free_buf`.

## C++ helpers (`cpp/pyra_cloud.cpp`)

```c
int pyra_cloud_raw_write(const char* path, const uint8_t* data,
                         int width, int height, int bands, int scenes,
                         int sample_format);
int pyra_cloud_raw_read(const char* path, uint8_t* out, size_t cap,
                        int* info);
int pyra_cloud_band_stats(const uint8_t* cube, size_t len,
                          int width, int height, int bands, int scenes,
                          int sample_format, int band,
                          PyraCloudBandStats* out);
int pyra_cloud_render_rgb(const uint8_t* cube, size_t len,
                           int width, int height, int bands, int scenes,
                           int sample_format, int scene,
                           int band_r, int band_g, int band_b,
                           double window_r, double level_r,
                           double window_g, double level_g,
                           double window_b, double level_b,
                           uint8_t* out);
int pyra_cloud_interleave(const uint8_t* cube, size_t len, ...);
int pyra_cloud_deinterleave(const uint8_t* interleaved, size_t len, ...);
```

The module also has a self test: compile it with
`-DPYRA_CLOUD_SELFTEST` and run the resulting binary.

## Version history

* **v1** — first release: 192 byte header with a SHA-256 content hash,
  band and scene tables, a JSON metadata block, a tile index with its
  own CRC-32, tile-local spatial predictors plus the time and band
  predictors, escape and byte-plane residual coding, RAW and LZ77
  methods, and CRC-32 on every structure.
