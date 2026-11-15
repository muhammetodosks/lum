//! PYRC — the PYRA Cloud container.
//!
//! A lossless container for earth observation data: multi-band scenes over
//! time, with per-band radiometric metadata and per-scene georeferencing.
//!
//! # Data model
//!
//! ```text
//! sample[scene][band][row][col]
//! ```
//!
//! The samples of one scene are stored band by band (band planar), each band
//! as a plane of `width * height` little endian samples.
//!
//! # Compression
//!
//! Every tile is coded independently in the spatial dimension. Two
//! predictors cross the tile borders and are what makes cloud data
//! compress:
//!
//! * **TIME** - the same band of the previous scene (radar loops, satellite
//!   revisits, numerical model output),
//! * **BAND** - the same pixel of the previous band (spectral correlation).
//!
//! Both are legal because the decoder rebuilds the scenes in order.
//!
//! The residual alphabet is modular, so a residual close to the *end* of the
//! alphabet counts as small. Wide samples are coded with a zig-zag escape
//! scheme, narrow ones with one adaptive model per byte plane.

use super::pyra_image::crc32;
use super::pyra_med::sha256;
use super::{CtxRangeDec, CtxRangeEnc, LzMatcher, LzTok, RangeDec, RangeEnc};

// ------------------------------------------------------------------ format

const CLOUD_MAGIC: [u8; 8] = [0x50, 0x59, 0x52, 0x43, 0x0D, 0x0A, 0x1A, 0x0A];
const CLOUD_VERSION: u32 = 1;

const HEADER_SIZE: usize = 192;
const HEADER_CRC_OFFSET: usize = 88;
const BAND_ENTRY_SIZE: usize = 48;
const SCENE_ENTRY_SIZE: usize = 40;
const INDEX_ENTRY_SIZE: usize = 24;

const SFMT_U8: i32 = 0;
const SFMT_U16: i32 = 1;
const SFMT_U32: i32 = 2;
const SFMT_I16: i32 = 3;
const SFMT_I32: i32 = 4;
const SFMT_F32: i32 = 5;

const PRED_NONE: i32 = 0;
const PRED_LEFT: i32 = 1;
const PRED_UP: i32 = 2;
const PRED_AVERAGE: i32 = 3;
const PRED_PAETH: i32 = 4;
const PRED_GRADIENT: i32 = 5;
const PRED_MEDIAN: i32 = 6;
const PRED_TIME: i32 = 7;
const PRED_BAND: i32 = 8;
const PRED_COUNT: i32 = 9;

const CANDIDATES_SPATIAL: [i32; 7] = [0, 1, 2, 3, 4, 5, 6];
const CANDIDATES_TIME: [i32; 8] = [0, 1, 2, 3, 4, 5, 6, 7];
const CANDIDATES_FULL: [i32; 9] = [0, 1, 2, 3, 4, 5, 6, 7, 8];

const METHOD_RAW: i32 = 0;
const METHOD_LZ: i32 = 1;

const TILE_FLAG_SPLIT: u8 = 0x01;
const TILE_FLAG_ADAPTIVE: u8 = 0x02;
const TILE_FLAG_ESCAPE: u8 = 0x04;
const TILE_FLAG_CONTEXT: u8 = 0x08;

const ESCAPE_SYMBOL: u32 = 128;
const ESCAPE_ALPHABET: usize = 129;
const ZIGZAG_LIMIT: i64 = 63;
const ESCAPE_CHUNK_BITS: u32 = 7;

const ENTROPY_SYMBOLS_RAW: usize = 256;
const ENTROPY_SYMBOLS_LZ: usize = 258;

const E_CLOUD_SHAPE: isize = -600;
const E_CLOUD_FORMAT: isize = -601;
const E_CLOUD_HEADER: isize = -602;
const E_CLOUD_CRC: isize = -603;
const E_CLOUD_METHOD: isize = -604;
const E_CLOUD_LZ: isize = -605;
const E_CLOUD_ALLOC: isize = -606;
const E_CLOUD_TRUNC: isize = -607;

// ------------------------------------------------------------- sample format

/// Coding parameters of a sample format.
#[derive(Clone, Copy)]
struct CloudFormat {
    sample_bytes: usize,
    /// Code values per sample: 1 for the integer formats, 4 for float32
    /// (the four byte planes of the sample).
    values_per_sample: usize,
    /// Bytes of one code value group: the sample width for integers, 1 for
    /// float32 (whose planes are single bytes).
    value_bytes: usize,
    /// Size of the residual alphabet (a power of two).
    modulus: i64,
    /// Offset that maps a signed sample into the unsigned code domain.
    canon_offset: i64,
    signed: bool,
    is_float: bool,
    /// Number of 7 bit chunks an escaped residual is split into.
    escape_chunks: usize,
}

fn cloud_format(sample_format: i32) -> Option<CloudFormat> {
    let (sample_bytes, signed, is_float) = match sample_format {
        SFMT_U8 => (1usize, false, false),
        SFMT_U16 => (2, false, false),
        SFMT_U32 => (4, false, false),
        SFMT_I16 => (2, true, false),
        SFMT_I32 => (4, true, false),
        SFMT_F32 => (4, false, true),
        _ => return None,
    };
    if is_float {
        Some(CloudFormat {
            sample_bytes,
            values_per_sample: 4,
            value_bytes: 1,
            modulus: 256,
            canon_offset: 0,
            signed: false,
            is_float: true,
            escape_chunks: 1,
        })
    } else {
        let bits = 8 * sample_bytes as u32;
        let modulus = 1i64 << bits;
        let canon_offset = if signed { modulus >> 1 } else { 0 };
        Some(CloudFormat {
            sample_bytes,
            values_per_sample: 1,
            value_bytes: sample_bytes,
            modulus,
            canon_offset,
            signed,
            is_float: false,
            escape_chunks: ((bits as usize) + 6) / 7,
        })
    }
}

/// Canonical (unsigned) code value of the sample at `off`.
///
/// Signed samples are shifted into the unsigned domain so that the residual
/// alphabet stays a plain power of two.
#[inline]
fn canonical_at(raw: &[u8], off: usize, fmt: &CloudFormat) -> u32 {
    let sb = fmt.sample_bytes;
    let mut v: u64 = 0;
    for k in 0..sb {
        v |= (raw[off + k] as u64) << (8 * k);
    }
    let bits = 8 * sb as u32;
    if fmt.signed {
        let sign = 1u64 << (bits - 1);
        if v & sign != 0 {
            v = v.wrapping_sub(1u64 << bits);
        }
    }
    let mask = (1u64 << bits) - 1;
    ((v.wrapping_add(fmt.canon_offset as u64)) & mask) as u32
}

/// The raw bytes of the canonical code value.
#[inline]
fn raw_of(value: u32, fmt: &CloudFormat) -> Vec<u8> {
    // Exactly `sample_bytes` bytes per sample: a wider buffer would shift
    // every following sample in the plane. Signed samples are shifted back
    // out of the canonical domain into their two's complement form.
    let x = if fmt.signed {
        let signed = value as i64 - fmt.canon_offset;
        (signed as i32 as u32).to_le_bytes()
    } else {
        value.to_le_bytes()
    };
    x[..fmt.sample_bytes].to_vec()
}

// --------------------------------------------------------------- arithmetic

/// The residual modulus is always a power of two, so the reduction is a
/// mask: no division and no data dependent branch.
#[inline]
fn mod_norm(r: i64, m: i64) -> i64 {
    ((r as u64) & ((m as u64) - 1)) as i64
}

/// Circular distance of a residual from zero.
#[inline]
fn residual_distance(res: i64, m: i64) -> i64 {
    let d = m - res;
    if res <= d {
        res
    } else {
        d
    }
}

// --------------------------------------------------------------- predictors

#[inline]
fn cloud_paeth(a: i64, b: i64, c: i64) -> i64 {
    let p = a + b - c;
    let pa = (p - a).abs();
    let pb = (p - b).abs();
    let pc = (p - c).abs();
    if pa <= pb && pa <= pc {
        a
    } else if pb <= pc {
        b
    } else {
        c
    }
}

#[inline]
fn cloud_median(a: i64, b: i64, c: i64) -> i64 {
    if c >= a.max(b) {
        a.min(b)
    } else if c <= a.min(b) {
        a.max(b)
    } else {
        a + b - c
    }
}

/// The predicted value for one sample.
///
/// `left`, `up` and `upper_left` are the spatial neighbours inside the
/// tile, `time_val` the same band of the previous scene and `band_val` the
/// same pixel of the previous band.
#[inline]
fn cloud_predict(
    pred: i32,
    left: i64,
    up: i64,
    upper_left: i64,
    time_val: i64,
    band_val: i64,
    modulus: i64,
) -> i64 {
    let p = match pred {
        PRED_NONE => 0,
        PRED_LEFT => left,
        PRED_UP => up,
        PRED_AVERAGE => (left + up) >> 1,
        PRED_PAETH => cloud_paeth(left, up, upper_left),
        PRED_GRADIENT => left + up - upper_left,
        PRED_MEDIAN => cloud_median(left, up, upper_left),
        PRED_TIME => time_val,
        PRED_BAND => band_val,
        _ => 0,
    };
    mod_norm(p, modulus)
}

/// Score every predictor candidate of a row in a single pass.
///
/// The neighbour context is loaded once per sample instead of once per
/// candidate, and no residual array is written: the winner is computed
/// afterwards. Ties keep the first candidate.
fn score_candidates(
    row: &[u32],
    prev: Option<&[u32]>,
    time_row: Option<&[u32]>,
    band_row: Option<&[u32]>,
    modulus: i64,
    candidates: &[i32],
) -> i32 {
    let mut scores = [0i64; 9];
    let n = row.len();
    for i in 0..n {
        let left = if i > 0 { row[i - 1] as i64 } else { 0 };
        let up = match prev {
            Some(p) => p[i] as i64,
            None => 0,
        };
        let upper_left = if i > 0 {
            match prev {
                Some(p) => p[i - 1] as i64,
                None => 0,
            }
        } else {
            0
        };
        let time_val = match time_row {
            Some(t) => t[i] as i64,
            None => 0,
        };
        let band_val = match band_row {
            Some(b) => b[i] as i64,
            None => 0,
        };
        let value = row[i] as i64;
        for (k, &pred) in candidates.iter().enumerate() {
            let p = cloud_predict(pred, left, up, upper_left, time_val, band_val, modulus);
            scores[k] += residual_distance(mod_norm(value - p, modulus), modulus);
        }
    }
    let mut best = candidates[0];
    let mut best_score = -1i64;
    for (k, &pred) in candidates.iter().enumerate() {
        if best_score < 0 || scores[k] < best_score {
            best_score = scores[k];
            best = pred;
        }
    }
    best
}

/// Residuals of one row for one predictor (modular alphabet).
fn row_residuals(
    row: &[u32],
    prev: Option<&[u32]>,
    time_row: Option<&[u32]>,
    band_row: Option<&[u32]>,
    modulus: i64,
    pred: i32,
) -> Vec<u32> {
    let n = row.len();
    let mut out = vec![0u32; n];
    for i in 0..n {
        let left = if i > 0 { row[i - 1] as i64 } else { 0 };
        let up = match prev {
            Some(p) => p[i] as i64,
            None => 0,
        };
        let upper_left = if i > 0 {
            match prev {
                Some(p) => p[i - 1] as i64,
                None => 0,
            }
        } else {
            0
        };
        let time_val = match time_row {
            Some(t) => t[i] as i64,
            None => 0,
        };
        let band_val = match band_row {
            Some(b) => b[i] as i64,
            None => 0,
        };
        let p = cloud_predict(pred, left, up, upper_left, time_val, band_val, modulus);
        out[i] = mod_norm(row[i] as i64 - p, modulus) as u32;
    }
    out
}

/// Rebuild the canonical rows of one tile from its residuals.
fn reconstruct_tile(
    residuals: &[u32],
    pred_ids: &[i32],
    per_row: usize,
    th: usize,
    modulus: i64,
    time_rows: Option<&[Vec<u32>]>,
    band_rows: Option<&[Vec<u32>]>,
) -> Vec<u32> {
    let mut rows: Vec<u32> = vec![0u32; per_row * th];
    for r in 0..th {
        let row_res = &residuals[r * per_row..(r + 1) * per_row];
        let (head, tail) = rows.split_at_mut(r * per_row);
        let row_out = &mut tail[..per_row];
        let prev: Option<&[u32]> = if r > 0 {
            Some(&head[(r - 1) * per_row..r * per_row])
        } else {
            None
        };
        let time_row = time_rows.map(|t| t[r].as_slice());
        let band_row = band_rows.map(|b| b[r].as_slice());
        let pred = pred_ids[r];
        for i in 0..per_row {
            let left = if i > 0 { row_out[i - 1] as i64 } else { 0 };
            let up = match prev {
                Some(p) => p[i] as i64,
                None => 0,
            };
            let upper_left = if i > 0 {
                match prev {
                    Some(p) => p[i - 1] as i64,
                    None => 0,
                }
            } else {
                0
            };
            let time_val = match time_row {
                Some(t) => t[i] as i64,
                None => 0,
            };
            let band_val = match band_row {
                Some(b) => b[i] as i64,
                None => 0,
            };
            let p = cloud_predict(pred, left, up, upper_left, time_val, band_val, modulus);
            row_out[i] = mod_norm(row_res[i] as i64 + p, modulus) as u32;
        }
    }
    rows
}

// ------------------------------------------------------------ filter table

/// Pack one predictor id per row (4 bits, MSB first).
fn pack_filter_bits(preds: &[i32]) -> Vec<u8> {
    let mut out = Vec::with_capacity((preds.len() + 1) / 2);
    let mut acc: u32 = 0;
    let mut nbits: u32 = 0;
    for &p in preds {
        acc = (acc << 4) | ((p as u32) & 0xF);
        nbits += 4;
        if nbits >= 8 {
            out.push(((acc >> (nbits - 8)) & 0xFF) as u8);
            nbits -= 8;
            acc &= (1u32 << nbits) - 1;
        }
    }
    if nbits > 0 {
        out.push(((acc << (8 - nbits)) & 0xFF) as u8);
    }
    out
}

/// Inverse of `pack_filter_bits`.
fn unpack_filter_bits(data: &[u8], rows: usize) -> Vec<i32> {
    let mut out = Vec::with_capacity(rows);
    let mut acc: u32 = 0;
    let mut nbits: u32 = 0;
    let mut pos = 0usize;
    for _ in 0..rows {
        while nbits < 4 {
            if pos >= data.len() {
                acc <<= 4;
                nbits += 4;
                break;
            }
            acc = (acc << 8) | (data[pos] as u32);
            pos += 1;
            nbits += 8;
        }
        out.push(((acc >> (nbits - 4)) & 0xF) as i32);
        nbits -= 4;
        acc &= (1u32 << nbits) - 1;
    }
    out
}

// ------------------------------------------------------------- byte planes

/// Split code values into byte planes (low plane first).
fn split_planes(values: &[u32], value_bytes: usize) -> Vec<u8> {
    if value_bytes == 1 {
        return values.iter().map(|&v| (v & 0xFF) as u8).collect();
    }
    let mut out = Vec::with_capacity(values.len() * value_bytes);
    for plane in 0..value_bytes {
        let shift = 8 * plane;
        for &v in values {
            out.push(((v >> shift) & 0xFF) as u8);
        }
    }
    out
}

/// Inverse of `split_planes`.
fn merge_planes(data: &[u8], count: usize, value_bytes: usize) -> Vec<u32> {
    if value_bytes == 1 {
        return data[..count].iter().map(|&b| b as u32).collect();
    }
    let mut out = vec![0u32; count];
    for plane in 0..value_bytes {
        let base = plane * count;
        let shift = 8 * plane;
        for i in 0..count {
            out[i] |= (data[base + i] as u32) << shift;
        }
    }
    out
}

// ------------------------------------------------------------ escape coding

/// Is the escape coding worth it for these residuals?
///
/// The escape scheme pays off when most residuals fit into the zig-zag
/// range; when a quarter of them already escape, the byte planes win.
fn escape_worth_it(residuals: &[u32], fmt: &CloudFormat) -> bool {
    if residuals.is_empty() {
        return true;
    }
    let half = fmt.modulus >> 1;
    let mut escapes = 0usize;
    for &r in residuals.iter() {
        let rv = r as i64;
        let c = if rv < half { rv } else { rv - fmt.modulus };
        if c < -ZIGZAG_LIMIT || c > ZIGZAG_LIMIT {
            escapes += 1;
            if escapes * 4 > residuals.len() {
                return false;
            }
        }
    }
    true
}

/// Code residuals as one symbol per sample.
fn escape_symbols(residuals: &[u32], fmt: &CloudFormat) -> Vec<u32> {
    let mut out: Vec<u32> = Vec::with_capacity(residuals.len() + residuals.len() / 16);
    let half = fmt.modulus >> 1;
    let bits = 8 * fmt.sample_bytes as u32;
    for &r in residuals.iter() {
        let rv = r as i64;
        let c = if rv < half { rv } else { rv - fmt.modulus };
        if c >= -ZIGZAG_LIMIT && c <= ZIGZAG_LIMIT {
            let z = if c >= 0 { 2 * c } else { -2 * c - 1 };
            out.push(z as u32);
        } else {
            out.push(ESCAPE_SYMBOL);
            let raw = (c as u64) & ((1u64 << bits) - 1);
            for k in 0..fmt.escape_chunks {
                out.push(((raw >> (ESCAPE_CHUNK_BITS as usize * k)) & 0x7F) as u32);
            }
        }
    }
    out
}

/// Inverse of `escape_symbols`; returns the modular residuals.
fn escape_residuals<I: Iterator<Item = u32>>(
    mut it: I,
    count: usize,
    fmt: &CloudFormat,
) -> Result<Vec<u32>, isize> {
    let mut out: Vec<u32> = Vec::with_capacity(count);
    let bits = 8 * fmt.sample_bytes as u32;
    let sign_bit = 1u64 << (bits - 1);
    let modulus = 1u64 << bits;
    while out.len() < count {
        let sym = match it.next() {
            Some(s) => s,
            None => return Err(E_CLOUD_TRUNC),
        };
        let c: i64 = if sym < ESCAPE_SYMBOL {
            if sym & 1 != 0 {
                -(((sym + 1) / 2) as i64)
            } else {
                (sym / 2) as i64
            }
        } else if sym == ESCAPE_SYMBOL {
            let mut raw: u64 = 0;
            for k in 0..fmt.escape_chunks {
                let part = match it.next() {
                    Some(p) => p,
                    None => return Err(E_CLOUD_TRUNC),
                };
                if part >= ESCAPE_SYMBOL {
                    return Err(E_CLOUD_LZ);
                }
                raw |= (part as u64) << (ESCAPE_CHUNK_BITS as usize * k);
            }
            if raw & sign_bit != 0 {
                (raw as i64) - (modulus as i64)
            } else {
                raw as i64
            }
        } else {
            return Err(E_CLOUD_LZ);
        };
        out.push(mod_norm(c, fmt.modulus) as u32);
    }
    Ok(out)
}

// ------------------------------------------------------------------ entropy

fn entropy_encode(symbols: &[u32], n_symbols: usize) -> Vec<u8> {
    let mut enc = RangeEnc::new(n_symbols, symbols.len());
    for &s in symbols {
        enc.push(s as usize);
    }
    enc.finish()
}

fn entropy_decode(data: &[u8], n_symbols: usize, count: usize) -> Vec<u32> {
    let mut dec = RangeDec::new(data, n_symbols);
    let mut out = Vec::with_capacity(count);
    for _ in 0..count {
        out.push(dec.decode_symbol() as u32);
    }
    out
}

/// Code a byte stream with one adaptive model per byte plane.
fn entropy_encode_planes(data: &[u8], n_symbols: usize, n_planes: usize) -> Vec<u8> {
    let planes = if n_planes < 1 { 1 } else { n_planes };
    let per_plane = data.len() / planes;
    let mut enc = CtxRangeEnc::new(n_symbols, planes, data.len() * 8);
    for p in 0..planes {
        let start = p * per_plane;
        for &b in &data[start..start + per_plane] {
            enc.push(b as usize, p);
        }
    }
    enc.finish()
}

/// Inverse of `entropy_encode_planes`.
fn entropy_decode_planes(
    data: &[u8], n_symbols: usize, n_planes: usize, count: usize,
) -> Vec<u8> {
    let planes = if n_planes < 1 { 1 } else { n_planes };
    let mut dec = CtxRangeDec::new(data, n_symbols, planes);
    let mut out = Vec::with_capacity(count * planes);
    for p in 0..planes {
        for _ in 0..count {
            out.push(dec.decode_symbol(p) as u8);
        }
    }
    out
}

// ---------------------------------------------------------------------- lz

fn lz_encode_bytes(data: &[u8]) -> Vec<u32> {
    let mut matcher = LzMatcher::new(data);
    let mut out: Vec<u32> = Vec::with_capacity(data.len() / 2 + 8);
    loop {
        match matcher.next_token() {
            LzTok::Eof => {
                out.push(super::TOKEN_EOF);
                break;
            }
            LzTok::Lit(b) => out.push(b as u32),
            LzTok::Match { len, dist } => {
                out.push(super::TOKEN_MATCH);
                out.push((len - super::MINMATCH) as u32);
                let d0 = dist - 1;
                out.push((d0 & 0xFF) as u32);
                out.push((d0 >> 8) as u32);
            }
        }
    }
    out
}

fn lz_decode_bytes(symbols: &[u32], target_len: usize) -> Result<Vec<u8>, isize> {
    let mut out: Vec<u8> = Vec::with_capacity(target_len);
    let mut pos = 0usize;
    let n = symbols.len();
    while pos < n {
        let sym = symbols[pos];
        pos += 1;
        if sym == super::TOKEN_EOF {
            break;
        }
        if sym < 256 {
            if out.len() >= target_len {
                return Err(E_CLOUD_LZ);
            }
            out.push(sym as u8);
            continue;
        }
        if sym != super::TOKEN_MATCH {
            return Err(E_CLOUD_LZ);
        }
        if pos + 3 > n {
            return Err(E_CLOUD_LZ);
        }
        let len_extra = symbols[pos];
        // The matcher writes the distance low byte first.
        let dist_lo = symbols[pos + 1];
        let dist_hi = symbols[pos + 2];
        pos += 3;
        if len_extra > 255 || dist_hi > 255 || dist_lo > 255 {
            return Err(E_CLOUD_LZ);
        }
        let length = super::MINMATCH + len_extra as usize;
        let dist = (dist_hi * 256 + dist_lo + 1) as usize;
        if dist > out.len() {
            return Err(E_CLOUD_LZ);
        }
        if out.len() + length > target_len {
            return Err(E_CLOUD_LZ);
        }
        for _ in 0..length {
            let b = out[out.len() - dist];
            out.push(b);
        }
    }
    if out.len() != target_len {
        return Err(E_CLOUD_LZ);
    }
    Ok(out)
}

// ------------------------------------------------------------------ tiles

struct TileGeom {
    x: usize,
    y: usize,
    w: usize,
    h: usize,
}

fn tile_grid(width: usize, height: usize, tile_size: usize) -> Vec<TileGeom> {
    let mut out = Vec::new();
    let mut ty = 0usize;
    while ty < height {
        let th = if tile_size < height - ty { tile_size } else { height - ty };
        let mut tx = 0usize;
        while tx < width {
            let tw = if tile_size < width - tx { tile_size } else { width - tx };
            out.push(TileGeom { x: tx, y: ty, w: tw, h: th });
            tx += tile_size;
        }
        ty += tile_size;
    }
    out
}

/// Canonical rows of one tile.
fn canonical_rows(
    raw: &[u8],
    row_stride: usize,
    base_off: usize,
    tw: usize,
    th: usize,
    fmt: &CloudFormat,
) -> Vec<Vec<u32>> {
    let sb = fmt.sample_bytes;
    let mut rows = Vec::with_capacity(th);
    for r in 0..th {
        let mut row = Vec::with_capacity(tw);
        let off = base_off + r * row_stride;
        for c in 0..tw {
            row.push(canonical_at(raw, off + c * sb, fmt));
        }
        rows.push(row);
    }
    rows
}

/// Encode one integer tile.
fn encode_tile_int(
    rows: &[Vec<u32>],
    time_rows: Option<&[Vec<u32>]>,
    band_rows: Option<&[Vec<u32>]>,
    fmt: &CloudFormat,
    predictor: i32,
    method: i32,
) -> (Vec<u8>, i32, u32) {
    let th = rows.len();
    let per_row = rows[0].len();
    let mut flags: u32 = 0;
    let mut table: Vec<u8> = Vec::new();
    let mut pred_ids: Vec<i32> = Vec::with_capacity(th);
    let mut residuals: Vec<u32> = Vec::with_capacity(per_row * th);

    let candidates: &[i32] = if time_rows.is_some() && band_rows.is_some() {
        &CANDIDATES_FULL
    } else if time_rows.is_some() {
        &CANDIDATES_TIME
    } else {
        &CANDIDATES_SPATIAL
    };

    for r in 0..th {
        let prev: Option<&[u32]> = if r > 0 { Some(rows[r - 1].as_slice()) } else { None };
        let time_row = time_rows.map(|t| t[r].as_slice());
        let band_row = band_rows.map(|b| b[r].as_slice());
        if predictor < 0 {
            let pred_id = score_candidates(
                &rows[r], prev, time_row, band_row, fmt.modulus, candidates,
            );
            pred_ids.push(pred_id);
            residuals.extend_from_slice(&row_residuals(
                &rows[r], prev, time_row, band_row, fmt.modulus, pred_id,
            ));
        } else {
            residuals.extend_from_slice(&row_residuals(
                &rows[r], prev, time_row, band_row, fmt.modulus, predictor,
            ));
        }
    }
    if predictor < 0 {
        table = pack_filter_bits(&pred_ids);
        flags |= TILE_FLAG_ADAPTIVE as u32;
    }

    // The escape scheme carries one symbol per sample; the byte planes
    // carry one byte per code value with a model per plane.
    let use_escape = fmt.value_bytes > 1 && escape_worth_it(&residuals, fmt);
    if use_escape {
        flags |= TILE_FLAG_ESCAPE as u32;
        let symbols = escape_symbols(&residuals, fmt);
        let stream = entropy_encode(&symbols, ESCAPE_ALPHABET);
        let mut body = table;
        body.extend_from_slice(&stream);
        return (body, METHOD_RAW, flags);
    }

    if fmt.value_bytes > 1 {
        flags |= TILE_FLAG_SPLIT as u32;
    }
    flags |= TILE_FLAG_CONTEXT as u32;
    let planes = split_planes(&residuals, fmt.value_bytes);
    let n_planes = if fmt.value_bytes > 1 { fmt.value_bytes } else { 1 };
    let raw_body = entropy_encode_planes(&planes, ENTROPY_SYMBOLS_RAW, n_planes);

    let chosen = if method == METHOD_LZ {
        METHOD_LZ
    } else if method == METHOD_RAW {
        METHOD_RAW
    } else {
        // LZ only pays off on byte streams; it is tried with a budget so a
        // hopeless candidate is abandoned early.
        let lz = lz_encode_bytes(&planes);
        let tokens = lz.len();
        let mut enc = RangeEnc::new(ENTROPY_SYMBOLS_LZ, tokens * 8 + 64);
        let mut aborted = false;
        for &t in lz.iter() {
            enc.push(t as usize);
            if enc.bit_len() > raw_body.len() * 8 {
                aborted = true;
                break;
            }
        }
        if aborted {
            METHOD_RAW
        } else {
            let stream = enc.finish();
            if stream.len() < raw_body.len() {
                let mut body = table;
                body.extend_from_slice(&stream);
                return (body, METHOD_LZ, flags);
            }
            METHOD_RAW
        }
    };

    let mut body = table;
    if chosen == METHOD_LZ {
        let lz = lz_encode_bytes(&planes);
        let mut enc = RangeEnc::new(ENTROPY_SYMBOLS_LZ, lz.len() * 8 + 64);
        for &t in lz.iter() {
            enc.push(t as usize);
        }
        body.extend_from_slice(&enc.finish());
    } else {
        body.extend_from_slice(&raw_body);
    }
    (body, chosen, flags)
}

/// Encode one float32 tile (four byte planes, no prediction).
fn encode_tile_float(
    raw: &[u8],
    row_stride: usize,
    base_off: usize,
    tw: usize,
    th: usize,
    method: i32,
) -> (Vec<u8>, i32, u32) {
    let mut planes: Vec<u8> = Vec::with_capacity(tw * th * 4);
    let mut p0 = Vec::with_capacity(tw * th);
    let mut p1 = Vec::with_capacity(tw * th);
    let mut p2 = Vec::with_capacity(tw * th);
    let mut p3 = Vec::with_capacity(tw * th);
    for r in 0..th {
        let off = base_off + r * row_stride;
        for c in 0..tw {
            let o = off + c * 4;
            p0.push(raw[o]);
            p1.push(raw[o + 1]);
            p2.push(raw[o + 2]);
            p3.push(raw[o + 3]);
        }
    }
    planes.extend_from_slice(&p0);
    planes.extend_from_slice(&p1);
    planes.extend_from_slice(&p2);
    planes.extend_from_slice(&p3);
    let flags = TILE_FLAG_SPLIT as u32 | TILE_FLAG_CONTEXT as u32;
    let raw_body = entropy_encode_planes(&planes, ENTROPY_SYMBOLS_RAW, 4);
    let chosen = if method == METHOD_LZ { METHOD_LZ } else { METHOD_RAW };
    let mut body = Vec::new();
    if chosen == METHOD_LZ {
        let lz = lz_encode_bytes(&planes);
        let mut enc = RangeEnc::new(ENTROPY_SYMBOLS_LZ, lz.len() * 8 + 64);
        for &t in lz.iter() {
            enc.push(t as usize);
        }
        body.extend_from_slice(&enc.finish());
    } else {
        body.extend_from_slice(&raw_body);
    }
    (body, chosen, flags)
}

/// Decode one integer tile into its canonical rows.
fn decode_tile_int(
    body: &[u8],
    flags: u32,
    method: i32,
    tw: usize,
    th: usize,
    fmt: &CloudFormat,
    time_rows: Option<&[Vec<u32>]>,
    band_rows: Option<&[Vec<u32>]>,
    predictor: i32,
) -> Result<Vec<u32>, isize> {
    let (stream, pred_ids): (&[u8], Vec<i32>) = if flags & TILE_FLAG_ADAPTIVE as u32 != 0 {
        let nbytes = (th + 1) / 2;
        if body.len() < nbytes {
            return Err(E_CLOUD_TRUNC);
        }
        (&body[nbytes..], unpack_filter_bits(&body[..nbytes], th))
    } else {
        (body, vec![predictor.max(0); th])
    };
    let per_row = tw;
    let count = per_row * th;
    let n_planes = if flags & TILE_FLAG_SPLIT as u32 != 0 {
        fmt.value_bytes
    } else {
        1
    };
    let residuals: Vec<u32> = if flags & TILE_FLAG_ESCAPE as u32 != 0 {
        let mut dec = RangeDec::new(stream, ESCAPE_ALPHABET);
        escape_residuals(
            (0..).map_while(|_| Some(dec.decode_symbol() as u32)),
            count,
            fmt,
        )?
    } else if method == METHOD_LZ {
        // The LZ method carries the byte plane stream as a token stream.
        let mut dec = RangeDec::new(stream, ENTROPY_SYMBOLS_LZ);
        let mut tokens: Vec<u32> = Vec::new();
        let limit = count * n_planes * 2 + 64;
        loop {
            let sym = dec.decode_symbol() as u32;
            if sym == super::TOKEN_EOF {
                break;
            }
            tokens.push(sym);
            if tokens.len() > limit {
                return Err(E_CLOUD_LZ);
            }
        }
        let bytes = lz_decode_bytes(&tokens, count * n_planes)?;
        merge_planes(&bytes, count, n_planes)
    } else {
        let bytes = entropy_decode_planes(stream, ENTROPY_SYMBOLS_RAW, n_planes, count);
        merge_planes(&bytes, count, n_planes)
    };
    Ok(reconstruct_tile(
        &residuals, &pred_ids, per_row, th, fmt.modulus, time_rows, band_rows,
    ))
}

/// Decode one float32 tile into its raw bytes.
fn decode_tile_float(
    body: &[u8],
    flags: u32,
    method: i32,
    tw: usize,
    th: usize,
) -> Result<Vec<u8>, isize> {
    let count = tw * th;
    let planes: Vec<u8> = if method == METHOD_LZ {
        // The LZ method carries the four byte planes as one token stream.
        let mut dec = RangeDec::new(body, ENTROPY_SYMBOLS_LZ);
        let mut tokens: Vec<u32> = Vec::new();
        let limit = count * 4 * 2 + 64;
        loop {
            let sym = dec.decode_symbol() as u32;
            if sym == super::TOKEN_EOF {
                break;
            }
            tokens.push(sym);
            if tokens.len() > limit {
                return Err(E_CLOUD_LZ);
            }
        }
        lz_decode_bytes(&tokens, count * 4)?
    } else {
        entropy_decode_planes(body, ENTROPY_SYMBOLS_RAW, 4, count)
    };
    let mut out = Vec::with_capacity(count * 4);
    for i in 0..count {
        out.push(planes[i]);
        out.push(planes[count + i]);
        out.push(planes[2 * count + i]);
        out.push(planes[3 * count + i]);
    }
    Ok(out)
}

// ------------------------------------------------------------- parallelism

fn parallel_map<T, F>(n: usize, f: F) -> Vec<T>
where
    F: Fn(usize) -> T + Sync,
    T: Send,
{
    if n == 0 {
        return Vec::new();
    }
    let threads = std::thread::available_parallelism()
        .map(|v| v.get())
        .unwrap_or(4)
        .min(n);
    let chunk = (n + threads - 1) / threads;
    let mut parts: Vec<Vec<T>> = Vec::new();
    std::thread::scope(|scope| {
        let mut handles = Vec::new();
        let mut start = 0usize;
        while start < n {
            let end = (start + chunk).min(n);
            let fref = &f;
            handles.push(scope.spawn(move || {
                let mut local = Vec::with_capacity(end - start);
                for i in start..end {
                    local.push(fref(i));
                }
                local
            }));
            start = end;
        }
        for h in handles {
            match h.join() {
                Ok(v) => parts.push(v),
                Err(_) => parts.push(Vec::new()),
            }
        }
    });
    parts.into_iter().flatten().collect()
}

// ----------------------------------------------------------------- encoder

/// Encode a multi-band, multi-scene cube into a PYRC container.
///
/// The band and scene tables are passed verbatim (48 and 40 bytes per
/// entry); the metadata block is stored as given.
#[no_mangle]
pub unsafe extern "C" fn pyra_cloud_encode(
    samples: *const u8,
    len: usize,
    width: u32,
    height: u32,
    bands: u32,
    scenes: u32,
    sample_format: u32,
    band_table: *const u8,
    band_table_len: usize,
    scene_table: *const u8,
    scene_table_len: usize,
    meta: *const u8,
    meta_len: usize,
    tile_size: usize,
    predictor: i32,
    method: i32,
    out_ptr: *mut *mut u8,
    out_len: *mut usize,
) -> isize {
    let fmt = match cloud_format(sample_format as i32) {
        Some(f) => f,
        None => return E_CLOUD_FORMAT,
    };
    let (width, height, bands, scenes) = (
        width as usize,
        height as usize,
        bands as usize,
        scenes as usize,
    );
    if width == 0 || height == 0 || bands == 0 || scenes == 0 || tile_size == 0 {
        return E_CLOUD_SHAPE;
    }
    if predictor < -1 || predictor >= PRED_COUNT {
        return E_CLOUD_METHOD;
    }
    if method != -1 && method != METHOD_RAW && method != METHOD_LZ {
        return E_CLOUD_METHOD;
    }
    if band_table_len != bands * BAND_ENTRY_SIZE
        || scene_table_len != scenes * SCENE_ENTRY_SIZE
    {
        return E_CLOUD_SHAPE;
    }
    let plane_bytes = width * height * fmt.sample_bytes;
    let total_bytes = plane_bytes * bands * scenes;
    if samples.is_null() || len < total_bytes {
        return E_CLOUD_SHAPE;
    }
    let raw = std::slice::from_raw_parts(samples, total_bytes);
    let band_blob: &[u8] = if band_table_len == 0 || band_table.is_null() {
        &[]
    } else {
        std::slice::from_raw_parts(band_table, band_table_len)
    };
    let scene_blob: &[u8] = if scene_table_len == 0 || scene_table.is_null() {
        &[]
    } else {
        std::slice::from_raw_parts(scene_table, scene_table_len)
    };
    let meta_blob: &[u8] = if meta_len == 0 || meta.is_null() {
        &[]
    } else {
        std::slice::from_raw_parts(meta, meta_len)
    };

    let grid = tile_grid(width, height, tile_size);
    let tiles_per_slice = grid.len();
    let num_entries = scenes * bands * tiles_per_slice;

    let mut bodies: Vec<Vec<u8>> = Vec::with_capacity(num_entries);
    let mut methods: Vec<u32> = Vec::with_capacity(num_entries);
    let mut flags_v: Vec<u32> = Vec::with_capacity(num_entries);

    let row_stride = width * fmt.sample_bytes;
    for s in 0..scenes {
        for b in 0..bands {
            let plane_off = (s * bands + b) * plane_bytes;
            let time_off = if s > 0 { Some((s - 1) * bands + b) } else { None }
                .map(|p| p * plane_bytes);
            let band_off = if b > 0 {
                Some((s * bands + b - 1) * plane_bytes)
            } else {
                None
            };
            let encoded = parallel_map(tiles_per_slice, |t| -> Option<(Vec<u8>, u32, u32)> {
                let geom = &grid[t];
                if fmt.is_float {
                    let (body, m, fl) = encode_tile_float(
                        raw, row_stride, plane_off + geom.y * row_stride + geom.x * fmt.sample_bytes,
                        geom.w, geom.h, method,
                    );
                    return Some((body, m as u32, fl));
                }
                let rows = canonical_rows(
                    raw, row_stride, plane_off + geom.y * row_stride + geom.x * fmt.sample_bytes,
                    geom.w, geom.h, &fmt,
                );
                let time_rows = time_off.map(|off| {
                    canonical_rows(
                        raw, row_stride, off + geom.y * row_stride + geom.x * fmt.sample_bytes,
                        geom.w, geom.h, &fmt,
                    )
                });
                let band_rows = band_off.map(|off| {
                    canonical_rows(
                        raw, row_stride, off + geom.y * row_stride + geom.x * fmt.sample_bytes,
                        geom.w, geom.h, &fmt,
                    )
                });
                let (body, m, fl) = encode_tile_int(
                    &rows,
                    time_rows.as_deref(),
                    band_rows.as_deref(),
                    &fmt,
                    predictor,
                    method,
                );
                Some((body, m as u32, fl))
            });
            for item in encoded {
                match item {
                    Some((body, m, fl)) => {
                        bodies.push(body);
                        methods.push(m);
                        flags_v.push(fl);
                    }
                    None => return E_CLOUD_ALLOC,
                }
            }
        }
    }

    // ---- assemble
    let meta_crc = crc32(meta_blob);
    let index_size = num_entries * INDEX_ENTRY_SIZE;
    let band_offset = HEADER_SIZE;
    let scene_offset = band_offset + band_blob.len();
    let meta_offset = scene_offset + scene_blob.len();
    let index_offset = meta_offset + meta_blob.len();
    let payload_offset = index_offset + index_size;

    let mut index = Vec::with_capacity(index_size);
    let mut payload: Vec<u8> = Vec::new();
    let mut offset = payload_offset;
    for i in 0..bodies.len() {
        index.extend_from_slice(&(offset as u32).to_le_bytes());
        index.extend_from_slice(&(bodies[i].len() as u32).to_le_bytes());
        index.push(methods[i] as u8);
        index.push(flags_v[i] as u8);
        index.extend_from_slice(&0u16.to_le_bytes());
        index.extend_from_slice(&crc32(&bodies[i]).to_le_bytes());
        // The entries are ordered scene, band, tile.
        let tiles = tiles_per_slice;
        let scene = i / (bands * tiles);
        let band = (i / tiles) % bands;
        let tile = i % tiles;
        index.extend_from_slice(&(scene as u16).to_le_bytes());
        index.extend_from_slice(&(band as u16).to_le_bytes());
        index.extend_from_slice(&(tile as u16).to_le_bytes());
        index.extend_from_slice(&0u16.to_le_bytes());
        payload.extend_from_slice(&bodies[i]);
        offset += bodies[i].len();
    }
    let payload_crc = crc32(&payload);
    let digest = sha256(raw);

    let mut header = vec![0u8; HEADER_SIZE];
    header[0..8].copy_from_slice(&CLOUD_MAGIC);
    let fields: [u32; 21] = [
        CLOUD_VERSION,
        0, // flags
        width as u32,
        height as u32,
        bands as u32,
        scenes as u32,
        sample_format,
        8 * fmt.sample_bytes as u32,
        tile_size as u32,
        ((width + tile_size - 1) / tile_size) as u32,
        ((height + tile_size - 1) / tile_size) as u32,
        num_entries as u32,
        band_offset as u32,
        band_blob.len() as u32,
        scene_offset as u32,
        scene_blob.len() as u32,
        meta_offset as u32,
        meta_blob.len() as u32,
        meta_crc,
        payload_crc,
        0, // header crc placeholder
    ];
    for (i, f) in fields.iter().enumerate() {
        header[8 + i * 4..12 + i * 4].copy_from_slice(&f.to_le_bytes());
    }
    header[92..96].copy_from_slice(&(index_offset as u32).to_le_bytes());
    header[96..100].copy_from_slice(&(index_size as u32).to_le_bytes());
    header[100..104].copy_from_slice(&(payload_offset as u32).to_le_bytes());
    // The fixed predictor id (used by tiles without a row filter table).
    header[104..108].copy_from_slice(&(predictor.max(0) as u32).to_le_bytes());
    header[112..144].copy_from_slice(&digest);
    let hdr_crc = crc32(&header[0..HEADER_CRC_OFFSET]);
    header[88..92].copy_from_slice(&hdr_crc.to_le_bytes());

    let mut out = Vec::with_capacity(
        HEADER_SIZE + band_blob.len() + scene_blob.len() + meta_blob.len() + index_size
            + payload.len(),
    );
    out.extend_from_slice(&header);
    out.extend_from_slice(band_blob);
    out.extend_from_slice(scene_blob);
    out.extend_from_slice(meta_blob);
    out.extend_from_slice(&index);
    out.extend_from_slice(&payload);

    let len = out.len();
    let boxed = out.into_boxed_slice();
    let ptr = boxed.as_ptr() as *mut u8;
    std::mem::forget(boxed);
    *out_ptr = ptr;
    *out_len = len;
    0
}

// ----------------------------------------------------------------- decoder

struct CloudHeader {
    version: u32,
    flags: u32,
    width: usize,
    height: usize,
    bands: usize,
    scenes: usize,
    sample_format: i32,
    bits: u32,
    tile_size: usize,
    tiles_x: usize,
    tiles_y: usize,
    num_entries: usize,
    band_offset: usize,
    band_size: usize,
    scene_offset: usize,
    scene_size: usize,
    meta_offset: usize,
    meta_size: usize,
    meta_crc: u32,
    payload_crc: u32,
    index_offset: usize,
    index_size: usize,
    payload_offset: usize,
    predictor: i32,
    sha256: [u8; 32],
}

fn rd_u32(data: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([data[off], data[off + 1], data[off + 2], data[off + 3]])
}

fn parse_header(data: &[u8]) -> Result<CloudHeader, isize> {
    if data.len() < HEADER_SIZE {
        return Err(E_CLOUD_HEADER);
    }
    if data[0..8] != CLOUD_MAGIC {
        return Err(E_CLOUD_HEADER);
    }
    let version = rd_u32(data, 8);
    if version != CLOUD_VERSION {
        return Err(E_CLOUD_HEADER);
    }
    let header_crc = rd_u32(data, 88);
    if crc32(&data[0..HEADER_CRC_OFFSET]) != header_crc {
        return Err(E_CLOUD_CRC);
    }
    let width = rd_u32(data, 16) as usize;
    let height = rd_u32(data, 20) as usize;
    let bands = rd_u32(data, 24) as usize;
    let scenes = rd_u32(data, 28) as usize;
    let sample_format = rd_u32(data, 32) as i32;
    let bits = rd_u32(data, 36);
    let tile_size = rd_u32(data, 40) as usize;
    let tiles_x = rd_u32(data, 44) as usize;
    let tiles_y = rd_u32(data, 48) as usize;
    let num_entries = rd_u32(data, 52) as usize;
    let band_offset = rd_u32(data, 56) as usize;
    let band_size = rd_u32(data, 60) as usize;
    let scene_offset = rd_u32(data, 64) as usize;
    let scene_size = rd_u32(data, 68) as usize;
    let meta_offset = rd_u32(data, 72) as usize;
    let meta_size = rd_u32(data, 76) as usize;
    let meta_crc = rd_u32(data, 80);
    let payload_crc = rd_u32(data, 84);
    let index_offset = rd_u32(data, 92) as usize;
    let index_size = rd_u32(data, 96) as usize;
    let payload_offset = rd_u32(data, 100) as usize;
    let predictor = rd_u32(data, 104) as i32;
    if width == 0 || height == 0 || bands == 0 || scenes == 0 || tile_size == 0 {
        return Err(E_CLOUD_HEADER);
    }
    if num_entries != scenes * bands * tiles_x * tiles_y {
        return Err(E_CLOUD_HEADER);
    }
    if index_size != num_entries * INDEX_ENTRY_SIZE {
        return Err(E_CLOUD_HEADER);
    }
    let mut sha = [0u8; 32];
    sha.copy_from_slice(&data[112..144]);
    Ok(CloudHeader {
        version,
        flags: rd_u32(data, 12),
        width,
        height,
        bands,
        scenes,
        sample_format,
        bits,
        tile_size,
        tiles_x,
        tiles_y,
        num_entries,
        band_offset,
        band_size,
        scene_offset,
        scene_size,
        meta_offset,
        meta_size,
        meta_crc,
        payload_crc,
        index_offset,
        index_size,
        payload_offset,
        predictor,
        sha256: sha,
    })
}

/// Decode a PYRC container into the raw samples.
///
/// The samples are returned scene by scene, band by band, each band as a
/// plane of `width * height` little endian samples.
#[no_mangle]
pub unsafe extern "C" fn pyra_cloud_decode(
    data: *const u8,
    len: usize,
    out_ptr: *mut *mut u8,
    out_len: *mut usize,
    meta_out: *mut i32,
) -> isize {
    if data.is_null() {
        return E_CLOUD_HEADER;
    }
    let data = std::slice::from_raw_parts(data, len);
    let hdr = match parse_header(data) {
        Ok(h) => h,
        Err(e) => return e,
    };
    let fmt = match cloud_format(hdr.sample_format) {
        Some(f) => f,
        None => return E_CLOUD_FORMAT,
    };
    if hdr.bits != 8 * fmt.sample_bytes as u32 {
        return E_CLOUD_HEADER;
    }
    if hdr.meta_offset + hdr.meta_size > data.len() {
        return E_CLOUD_TRUNC;
    }
    let meta_blob = &data[hdr.meta_offset..hdr.meta_offset + hdr.meta_size];
    if crc32(meta_blob) != hdr.meta_crc {
        return E_CLOUD_CRC;
    }
    if hdr.index_offset + hdr.index_size > data.len() {
        return E_CLOUD_TRUNC;
    }
    let index = &data[hdr.index_offset..hdr.index_offset + hdr.index_size];

    let plane_bytes = hdr.width * hdr.height * fmt.sample_bytes;
    let total_bytes = plane_bytes * hdr.bands * hdr.scenes;
    let mut out: Vec<u8> = vec![0u8; total_bytes];
    let row_stride = hdr.width * fmt.sample_bytes;
    let grid = tile_grid(hdr.width, hdr.height, hdr.tile_size);
    let tiles_per_slice = grid.len();

    // The previous scene (for the TIME predictor) and the previous band of
    // the current scene (for the BAND predictor) are kept as raw bytes.
    let mut prev_bands: Vec<Vec<u8>> = Vec::new();
    let mut cur_bands: Vec<Vec<u8>> = Vec::new();

    for s in 0..hdr.scenes {
        cur_bands.clear();
        for b in 0..hdr.bands {
            let entry_base = (s * hdr.bands + b) * tiles_per_slice;
            let time_plane: Option<&Vec<u8>> = if s > 0 { prev_bands.get(b) } else { None };
            let band_plane: Option<&Vec<u8>> = if b > 0 { cur_bands.get(b - 1) } else { None };

            let decoded = parallel_map(tiles_per_slice, |t| -> Option<Result<Vec<u8>, isize>> {
                let geom = &grid[t];
                let eoff = (entry_base + t) * INDEX_ENTRY_SIZE;
                let tile_off = rd_u32(index, eoff) as usize;
                let tile_len = rd_u32(index, eoff + 4) as usize;
                let method = index[eoff + 8] as i32;
                let flags = index[eoff + 9] as u32;
                let crc = rd_u32(index, eoff + 12);
                if tile_off + tile_len > data.len() {
                    return Some(Err(E_CLOUD_TRUNC));
                }
                let body = &data[tile_off..tile_off + tile_len];
                if crc32(body) != crc {
                    return Some(Err(E_CLOUD_CRC));
                }
                if method != METHOD_RAW && method != METHOD_LZ {
                    return Some(Err(E_CLOUD_METHOD));
                }
                if fmt.is_float {
                    return Some(decode_tile_float(body, flags, method, geom.w, geom.h));
                }
                let base = geom.y * row_stride + geom.x * fmt.sample_bytes;
                let time_rows = time_plane.map(|p| {
                    canonical_rows(p, row_stride, base, geom.w, geom.h, &fmt)
                });
                let band_rows = band_plane.map(|p| {
                    canonical_rows(p, row_stride, base, geom.w, geom.h, &fmt)
                });
                let rows = match decode_tile_int(
                    body, flags, method, geom.w, geom.h, &fmt,
                    time_rows.as_deref(), band_rows.as_deref(), hdr.predictor,
                ) {
                    Ok(r) => r,
                    Err(e) => return Some(Err(e)),
                };
                // Turn the canonical rows back into raw sample bytes.
                let mut bytes = Vec::with_capacity(geom.w * geom.h * fmt.sample_bytes);
                for r in 0..geom.h {
                    for c in 0..geom.w {
                        let v = rows[r * geom.w + c];
                        bytes.extend_from_slice(&raw_of(v, &fmt));
                    }
                }
                Some(Ok(bytes))
            });

            // The tile bodies are row major *inside* a tile; the plane is
            // row major over the whole scene, so the rows are scattered.
            let mut band_bytes: Vec<u8> = vec![0u8; plane_bytes];
            for (t, item) in decoded.into_iter().enumerate() {
                match item {
                    Some(Ok(bytes)) => {
                        let geom = &grid[t];
                        let twb = geom.w * fmt.sample_bytes;
                        for r in 0..geom.h {
                            let src = &bytes[r * twb..(r + 1) * twb];
                            let dst = (geom.y + r) * row_stride + geom.x * fmt.sample_bytes;
                            band_bytes[dst..dst + twb].copy_from_slice(src);
                        }
                    }
                    Some(Err(e)) => return e,
                    None => return E_CLOUD_ALLOC,
                }
            }
            // The band plane must be contiguous and in raster order.
            let plane_off = (s * hdr.bands + b) * plane_bytes;
            out[plane_off..plane_off + plane_bytes].copy_from_slice(&band_bytes);
            cur_bands.push(band_bytes);
        }
        prev_bands = std::mem::take(&mut cur_bands);
    }

    // The payload CRC is computed over the tile bodies in container order.
    let mut payload: Vec<u8> = Vec::new();
    for e in 0..hdr.num_entries {
        let eoff = e * INDEX_ENTRY_SIZE;
        let tile_off = rd_u32(index, eoff) as usize;
        let tile_len = rd_u32(index, eoff + 4) as usize;
        if tile_off + tile_len <= data.len() {
            payload.extend_from_slice(&data[tile_off..tile_off + tile_len]);
        }
    }
    if crc32(&payload) != hdr.payload_crc {
        return E_CLOUD_CRC;
    }
    if sha256(&out) != hdr.sha256 {
        return E_CLOUD_CRC;
    }

    if !meta_out.is_null() {
        let meta = [
            hdr.version as i32,
            hdr.flags as i32,
            hdr.width as i32,
            hdr.height as i32,
            hdr.bands as i32,
            hdr.scenes as i32,
            hdr.sample_format,
            hdr.bits as i32,
            hdr.tile_size as i32,
            hdr.tiles_x as i32,
            hdr.tiles_y as i32,
            hdr.num_entries as i32,
            hdr.predictor,
            hdr.band_offset as i32,
            hdr.band_size as i32,
            hdr.scene_offset as i32,
            hdr.scene_size as i32,
            hdr.meta_offset as i32,
            hdr.meta_size as i32,
            hdr.index_offset as i32,
            hdr.index_size as i32,
        ];
        let out_slice = std::slice::from_raw_parts_mut(meta_out, meta.len());
        out_slice.copy_from_slice(&meta);
    }

    let len = out.len();
    let boxed = out.into_boxed_slice();
    let ptr = boxed.as_ptr() as *mut u8;
    std::mem::forget(boxed);
    *out_ptr = ptr;
    *out_len = len;
    0
}

/// Read the header of a container without decoding the samples.
#[no_mangle]
pub unsafe extern "C" fn pyra_cloud_info(
    data: *const u8,
    len: usize,
    meta_out: *mut i32,
) -> isize {
    if data.is_null() {
        return E_CLOUD_HEADER;
    }
    let data = std::slice::from_raw_parts(data, len);
    let hdr = match parse_header(data) {
        Ok(h) => h,
        Err(e) => return e,
    };
    if !meta_out.is_null() {
        let meta = [
            hdr.version as i32,
            hdr.flags as i32,
            hdr.width as i32,
            hdr.height as i32,
            hdr.bands as i32,
            hdr.scenes as i32,
            hdr.sample_format,
            hdr.bits as i32,
            hdr.tile_size as i32,
            hdr.tiles_x as i32,
            hdr.tiles_y as i32,
            hdr.num_entries as i32,
            hdr.predictor,
            hdr.band_offset as i32,
            hdr.band_size as i32,
            hdr.scene_offset as i32,
            hdr.scene_size as i32,
            hdr.meta_offset as i32,
            hdr.meta_size as i32,
            hdr.index_offset as i32,
            hdr.index_size as i32,
        ];
        let out_slice = std::slice::from_raw_parts_mut(meta_out, meta.len());
        out_slice.copy_from_slice(&meta);
    }
    0
}

/// Read one band of one scene out of a container.
///
/// Only the tiles that touch the band are decoded, so reading a single band
/// of a large cube is cheap.
#[no_mangle]
pub unsafe extern "C" fn pyra_cloud_read_band(
    data: *const u8,
    len: usize,
    scene: u32,
    band: u32,
    out_ptr: *mut *mut u8,
    out_len: *mut usize,
) -> isize {
    if data.is_null() {
        return E_CLOUD_HEADER;
    }
    let data = std::slice::from_raw_parts(data, len);
    let hdr = match parse_header(data) {
        Ok(h) => h,
        Err(e) => return e,
    };
    let fmt = match cloud_format(hdr.sample_format) {
        Some(f) => f,
        None => return E_CLOUD_FORMAT,
    };
    if scene as usize >= hdr.scenes || band as usize >= hdr.bands {
        return E_CLOUD_SHAPE;
    }
    if hdr.index_offset + hdr.index_size > data.len() {
        return E_CLOUD_TRUNC;
    }
    let index = &data[hdr.index_offset..hdr.index_offset + hdr.index_size];
    let grid = tile_grid(hdr.width, hdr.height, hdr.tile_size);
    let tiles_per_slice = grid.len();
    let plane_bytes = hdr.width * hdr.height * fmt.sample_bytes;
    let row_stride = hdr.width * fmt.sample_bytes;
    let mut plane: Vec<u8> = vec![0u8; plane_bytes];

    // The temporal context needs the previous scene, so that one is decoded
    // as well when it exists.
    let need_prev = scene > 0;
    let mut prev_band: Option<Vec<u8>> = None;
    if need_prev {
        // Decode the previous scene's band with a nested call.
        let mut tmp_ptr: *mut u8 = std::ptr::null_mut();
        let mut tmp_len: usize = 0;
        let rc = pyra_cloud_decode(data.as_ptr(), len, &mut tmp_ptr, &mut tmp_len, std::ptr::null_mut());
        if rc < 0 {
            return rc;
        }
        let all = Vec::from_raw_parts(tmp_ptr, tmp_len, tmp_len);
        let base = (scene as usize - 1) * hdr.bands * plane_bytes + band as usize * plane_bytes;
        prev_band = Some(all[base..base + plane_bytes].to_vec());
        drop(all);
    }
    let band_ctx: Option<Vec<u8>> = if band > 0 {
        let mut tmp_ptr: *mut u8 = std::ptr::null_mut();
        let mut tmp_len: usize = 0;
        let rc = pyra_cloud_decode(data.as_ptr(), len, &mut tmp_ptr, &mut tmp_len, std::ptr::null_mut());
        if rc < 0 {
            return rc;
        }
        let all = Vec::from_raw_parts(tmp_ptr, tmp_len, tmp_len);
        let base = scene as usize * hdr.bands * plane_bytes + (band as usize - 1) * plane_bytes;
        let v = all[base..base + plane_bytes].to_vec();
        drop(all);
        Some(v)
    } else {
        None
    };

    let entry_base = (scene as usize * hdr.bands + band as usize) * tiles_per_slice;
    let decoded = parallel_map(tiles_per_slice, |t| -> Option<Result<(usize, usize, usize, Vec<u8>), isize>> {
        let geom = &grid[t];
        let eoff = (entry_base + t) * INDEX_ENTRY_SIZE;
        let tile_off = rd_u32(index, eoff) as usize;
        let tile_len = rd_u32(index, eoff + 4) as usize;
        let method = index[eoff + 8] as i32;
        let flags = index[eoff + 9] as u32;
        let crc = rd_u32(index, eoff + 12);
        if tile_off + tile_len > data.len() {
            return Some(Err(E_CLOUD_TRUNC));
        }
        let body = &data[tile_off..tile_off + tile_len];
        if crc32(body) != crc {
            return Some(Err(E_CLOUD_CRC));
        }
        if method != METHOD_RAW && method != METHOD_LZ {
            return Some(Err(E_CLOUD_METHOD));
        }
        if fmt.is_float {
            let bytes = match decode_tile_float(body, flags, method, geom.w, geom.h) {
                Ok(b) => b,
                Err(e) => return Some(Err(e)),
            };
            return Some(Ok((geom.y, geom.x, geom.w, bytes)));
        }
        let base = geom.y * row_stride + geom.x * fmt.sample_bytes;
        let time_rows = prev_band.as_ref().map(|p| canonical_rows(p, row_stride, base, geom.w, geom.h, &fmt));
        let band_rows = band_ctx.as_ref().map(|p| canonical_rows(p, row_stride, base, geom.w, geom.h, &fmt));
        let rows = match decode_tile_int(
            body, flags, method, geom.w, geom.h, &fmt,
            time_rows.as_deref(), band_rows.as_deref(), hdr.predictor,
        ) {
            Ok(r) => r,
            Err(e) => return Some(Err(e)),
        };
        let mut bytes = Vec::with_capacity(geom.w * geom.h * fmt.sample_bytes);
        for r in 0..geom.h {
            for c in 0..geom.w {
                let v = rows[r * geom.w + c];
                bytes.extend_from_slice(&raw_of(v, &fmt));
            }
        }
        Some(Ok((geom.y, geom.x, geom.w, bytes)))
    });
    for item in decoded {
        match item {
            Some(Ok((ty, tx, tw, bytes))) => {
                let twb = tw * fmt.sample_bytes;
                for r in 0..bytes.len() / twb {
                    let src = &bytes[r * twb..(r + 1) * twb];
                    let dst = (ty + r) * row_stride + tx * fmt.sample_bytes;
                    plane[dst..dst + twb].copy_from_slice(src);
                }
            }
            Some(Err(e)) => return e,
            None => return E_CLOUD_ALLOC,
        }
    }

    let len = plane.len();
    let boxed = plane.into_boxed_slice();
    let ptr = boxed.as_ptr() as *mut u8;
    std::mem::forget(boxed);
    *out_ptr = ptr;
    *out_len = len;
    0
}
