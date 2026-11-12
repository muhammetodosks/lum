//! PYRM (PYRA Medical) native core.
//!
//! The container is a 3D lossless image format: volume of slices, each slice
//! tiled, every tile predicted with a per-row filter choice and entropy
//! coded. This module is the native twin of the pure-Python reference in
//! `pyra/medical/codec.py`; both produce byte-identical containers.
//!
//! Layout of a tile body:
//!
//! ```text
//! [row filter table]   (only when the tile uses the adaptive filter)
//! [entropy stream]     (range coded symbols)
//! ```

use super::pyra_image::crc32;
use super::RangeDec;
use super::RangeEnc;
use std::collections::HashMap;

/// Python-style modulo for 64 bit values: the result is always in [0, m).
#[inline]
fn med_mod(x: i64, m: i64) -> i64 {
    let r = x % m;
    if r < 0 {
        r + m
    } else {
        r
    }
}

// ------------------------------------------------------------------ format

const MED_MAGIC: [u8; 8] = [0x50, 0x59, 0x52, 0x4D, 0x0D, 0x0A, 0x1A, 0x0A];
const MED_VERSION: u32 = 1;
const MED_HEADER_SIZE: usize = 144;
const MED_HEADER_CRC_OFFSET: usize = 72;
const MED_INDEX_ENTRY_SIZE: usize = 16;

const SFMT_U8: i32 = 0;
const SFMT_U16: i32 = 1;
const SFMT_U32: i32 = 2;
const SFMT_I16: i32 = 3;
const SFMT_I32: i32 = 4;
const SFMT_F32: i32 = 5;

const PRED_NONE: i32 = 0;
const PRED_SLICE: i32 = 7;
const CANDIDATES_2D: [i32; 7] = [0, 1, 2, 3, 4, 5, 6];
const CANDIDATES_3D: [i32; 8] = [0, 1, 2, 3, 4, 5, 6, 7];

const FILTER_BITS_PER_ROW: u32 = 4;

const METHOD_RAW: i32 = 0;
const METHOD_LZ: i32 = 1;

const TILE_FLAG_SPLIT: u8 = 0x01;
const TILE_FLAG_ADAPTIVE: u8 = 0x02;

const LZ_MIN_MATCH: usize = 4;
const LZ_MAX_MATCH: usize = 259;
const LZ_MAX_DIST: usize = 65536;
const LZ_TOKEN_MATCH: u32 = 256;
const LZ_TOKEN_EOF: u32 = 257;
const LZ_TOKEN_SYMBOLS: usize = 258;
const LZ_CHAIN_LIMIT: usize = 64;

const ENTROPY_SYMBOLS_RAW: usize = 256;
const ENTROPY_SYMBOLS_LZ: usize = LZ_TOKEN_SYMBOLS;

// error codes
const E_MED_SHAPE: isize = -500;
const E_MED_FORMAT: isize = -501;
const E_MED_HEADER: isize = -502;
const E_MED_CRC: isize = -503;
const E_MED_METHOD: isize = -504;
const E_MED_LZ: isize = -505;
const E_MED_ALLOC: isize = -506;
const E_MED_TRUNC: isize = -507;

/// Coding parameters of a sample format.
#[derive(Clone, Copy)]
struct MedFormat {
    sample_bytes: usize,
    values_per_sample: usize,
    value_bytes: usize,
    modulus: i64,
    canon_offset: i64,
    signed: bool,
    is_float: bool,
}

fn med_format(sample_format: i32) -> Option<MedFormat> {
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
        Some(MedFormat {
            sample_bytes,
            values_per_sample: 4,
            value_bytes: 1,
            modulus: 256,
            canon_offset: 0,
            signed: false,
            is_float: true,
        })
    } else {
        let bits = 8 * sample_bytes as u32;
        let modulus = 1i64 << bits;
        let canon_offset = if signed { modulus >> 1 } else { 0 };
        Some(MedFormat {
            sample_bytes,
            values_per_sample: 1,
            value_bytes: sample_bytes,
            modulus,
            canon_offset,
            signed,
            is_float: false,
        })
    }
}

// --------------------------------------------------------------- predictors

fn med_paeth(a: i64, b: i64, c: i64) -> i64 {
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

fn med_median(a: i64, b: i64, c: i64) -> i64 {
    if c >= a.max(b) {
        a.min(b)
    } else if c <= a.min(b) {
        a.max(b)
    } else {
        a + b - c
    }
}

fn med_predict(pred: i32, left: i64, up: i64, upper_left: i64, slice_val: i64) -> i64 {
    match pred {
        0 => 0,                       // NONE
        1 => left,                    // LEFT
        2 => up,                      // UP
        3 => (left + up) >> 1,        // AVERAGE
        4 => med_paeth(left, up, upper_left),
        5 => left + up - upper_left,  // GRADIENT
        6 => med_median(left, up, upper_left),
        7 => slice_val,              // SLICE (volumetric)
        _ => 0,
    }
}

/// Residuals of one row for one predictor (modular alphabet).
fn row_residuals(
    row: &[i64],
    prev: Option<&[i64]>,
    slice_row: Option<&[i64]>,
    channels: usize,
    modulus: i64,
    pred: i32,
) -> Vec<i64> {
    let n = row.len();
    let mut out = vec![0i64; n];
    for i in 0..n {
        let x = i / channels;
        let left = if x > 0 { row[i - channels] } else { 0 };
        let up = match prev {
            Some(p) => p[i],
            None => 0,
        };
        let upper_left = if x > 0 {
            match prev {
                Some(p) => p[i - channels],
                None => 0,
            }
        } else {
            0
        };
        let slice_val = match slice_row {
            Some(s) => s[i],
            None => 0,
        };
        let p = med_predict(pred, left, up, upper_left, slice_val);
        out[i] = med_mod(row[i] - p, modulus);
    }
    out
}

/// Circular distance of the residuals from zero.
fn residual_score(res: &[i64], modulus: i64) -> i64 {
    let mut score = 0i64;
    for &r in res {
        let d = modulus - r;
        score += if r <= d { r } else { d };
    }
    score
}

/// Pick the cheapest predictor for one row; ties keep the first candidate.
fn best_row_predictor(
    row: &[i64],
    prev: Option<&[i64]>,
    slice_row: Option<&[i64]>,
    channels: usize,
    modulus: i64,
    candidates: &[i32],
) -> (i32, Vec<i64>) {
    let mut best_pred = candidates[0];
    let mut best_res: Vec<i64> = Vec::new();
    let mut best_score = -1i64;
    for &pred in candidates.iter() {
        let res = row_residuals(row, prev, slice_row, channels, modulus, pred);
        let score = residual_score(&res, modulus);
        if best_score < 0 || score < best_score {
            best_score = score;
            best_res = res;
            best_pred = pred;
        }
    }
    (best_pred, best_res)
}

/// Pack one predictor id per row (4 bits, MSB first).
fn pack_filter_bits(preds: &[i32]) -> Vec<u8> {
    let mut out = Vec::with_capacity((preds.len() + 1) / 2);
    let mut acc: u32 = 0;
    let mut nbits: u32 = 0;
    for &p in preds {
        acc = (acc << FILTER_BITS_PER_ROW) | ((p as u32) & 0xF);
        nbits += FILTER_BITS_PER_ROW;
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
        while nbits < FILTER_BITS_PER_ROW {
            if pos >= data.len() {
                acc <<= FILTER_BITS_PER_ROW;
                nbits += FILTER_BITS_PER_ROW;
                break;
            }
            acc = (acc << 8) | (data[pos] as u32);
            pos += 1;
            nbits += 8;
        }
        out.push(((acc >> (nbits - FILTER_BITS_PER_ROW)) & 0xF) as i32);
        nbits -= FILTER_BITS_PER_ROW;
        acc &= (1u32 << nbits) - 1;
    }
    out
}

/// Split packed residuals into byte planes (low plane first).
fn split_planes(res: &[i64], value_bytes: usize) -> Vec<u8> {
    if value_bytes == 1 {
        return res.iter().map(|&r| (r & 0xFF) as u8).collect();
    }
    let mut out = Vec::with_capacity(res.len() * value_bytes);
    for plane in 0..value_bytes {
        let shift = 8 * plane;
        for &r in res {
            out.push(((r >> shift) & 0xFF) as u8);
        }
    }
    out
}

/// Inverse of `split_planes`.
fn merge_planes(data: &[u8], count: usize, value_bytes: usize) -> Vec<i64> {
    if value_bytes == 1 {
        return data[..count].iter().map(|&b| b as i64).collect();
    }
    let mut out = vec![0i64; count];
    for plane in 0..value_bytes {
        let base = plane * count;
        let shift = 8 * plane;
        for i in 0..count {
            out[i] |= (data[base + i] as i64) << shift;
        }
    }
    out
}

// ------------------------------------------------------------------ samples

/// Read `count` samples and map them into the unsigned code domain.
fn canonical_row(src: &[u8], offset: usize, count: usize, fmt: &MedFormat) -> Vec<i64> {
    let mut out = Vec::with_capacity(count * fmt.values_per_sample);
    if fmt.is_float {
        for i in 0..count {
            let base = offset + i * 4;
            for plane in 0..4 {
                out.push(src[base + plane] as i64);
            }
        }
        return out;
    }
    let sb = fmt.sample_bytes;
    let bits = 8 * sb as u32;
    for i in 0..count {
        let mut v: i64 = 0;
        for k in 0..sb {
            v |= (src[offset + i * sb + k] as i64) << (8 * k);
        }
        if fmt.signed && bits < 64 && (v & (1i64 << (bits - 1))) != 0 {
            v -= 1i64 << bits;
        }
        out.push(med_mod(v + fmt.canon_offset, fmt.modulus));
    }
    out
}

/// Inverse of `canonical_row`.
fn decode_row(values: &[i64], fmt: &MedFormat) -> Vec<u8> {
    if fmt.is_float {
        return values.iter().map(|&v| (v & 0xFF) as u8).collect();
    }
    let sb = fmt.sample_bytes;
    let mut out = Vec::with_capacity(values.len() * sb);
    for &v in values {
        let x = med_mod(v - fmt.canon_offset, fmt.modulus);
        for k in 0..sb {
            out.push(((x >> (8 * k)) & 0xFF) as u8);
        }
    }
    out
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

// ---------------------------------------------------------------------- lz

/// Tokenise `data` into the LZ77 symbol stream (without the EOF marker).
fn lz_encode(data: &[u8]) -> Vec<u32> {
    let n = data.len();
    let mut out: Vec<u32> = Vec::new();
    let mut table: HashMap<u32, Vec<u32>> = HashMap::new();
    let mut i = 0usize;
    while i < n {
        let mut advance = 1usize;
        if i + LZ_MIN_MATCH <= n {
            let key = ((data[i] as u32) << 16) | ((data[i + 1] as u32) << 8) | data[i + 2] as u32;
            let mut best_len = 0usize;
            let mut best_dist = 0usize;
            if let Some(chain) = table.get(&key) {
                let limit = if LZ_MAX_MATCH < n - i { LZ_MAX_MATCH } else { n - i };
                for &pos in chain.iter().rev() {
                    let dist = i - (pos as usize);
                    if dist > LZ_MAX_DIST {
                        break;
                    }
                    let cand = pos as usize;
                    let mut length = 0usize;
                    while length < limit && data[cand + length] == data[i + length] {
                        length += 1;
                    }
                    if length > best_len {
                        best_len = length;
                        best_dist = dist;
                        if best_len == limit {
                            break;
                        }
                    }
                }
            }
            if best_len >= LZ_MIN_MATCH {
                out.push(LZ_TOKEN_MATCH);
                out.push((best_len - LZ_MIN_MATCH) as u32);
                out.push(((best_dist - 1) >> 8) as u32);
                out.push(((best_dist - 1) & 0xFF) as u32);
                advance = best_len;
            } else {
                out.push(data[i] as u32);
            }
        } else {
            out.push(data[i] as u32);
        }
        // remember every position covered by this step
        let cap = n.saturating_sub(LZ_MIN_MATCH - 1);
        let mut stop = i + advance;
        if stop > cap {
            stop = cap;
        }
        let mut j = i;
        while j < stop {
            let key = ((data[j] as u32) << 16) | ((data[j + 1] as u32) << 8) | data[j + 2] as u32;
            let chain = table.entry(key).or_insert_with(Vec::new);
            chain.push(j as u32);
            if chain.len() > LZ_CHAIN_LIMIT {
                chain.remove(0);
            }
            j += 1;
        }
        i += advance;
    }
    out
}

/// Rebuild the byte stream from an LZ77 symbol list.
fn lz_decode(symbols: &[u32], target_len: usize) -> Result<Vec<u8>, isize> {
    let mut out: Vec<u8> = Vec::with_capacity(target_len);
    let mut pos = 0usize;
    let n = symbols.len();
    while pos < n {
        let sym = symbols[pos];
        pos += 1;
        if sym == LZ_TOKEN_EOF {
            break;
        }
        if sym < 256 {
            if out.len() >= target_len {
                return Err(E_MED_LZ);
            }
            out.push(sym as u8);
            continue;
        }
        if sym != LZ_TOKEN_MATCH {
            return Err(E_MED_LZ);
        }
        if pos + 3 > n {
            return Err(E_MED_LZ);
        }
        let len_extra = symbols[pos];
        let dist_hi = symbols[pos + 1];
        let dist_lo = symbols[pos + 2];
        pos += 3;
        if len_extra > 255 || dist_hi > 255 || dist_lo > 255 {
            return Err(E_MED_LZ);
        }
        let length = LZ_MIN_MATCH + len_extra as usize;
        let dist = (dist_hi * 256 + dist_lo + 1) as usize;
        if dist > out.len() {
            return Err(E_MED_LZ);
        }
        if out.len() + length > target_len {
            return Err(E_MED_LZ);
        }
        for _ in 0..length {
            let b = out[out.len() - dist];
            out.push(b);
        }
    }
    if out.len() != target_len {
        return Err(E_MED_LZ);
    }
    Ok(out)
}

// -------------------------------------------------------------------- tile

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

/// Encode one tile; returns (body, method, flags).
fn encode_tile(
    rows: &[Vec<i64>],
    channels: usize,
    fmt: &MedFormat,
    slice_rows: Option<&[Vec<i64>]>,
    predictor: i32,
    method: i32,
) -> (Vec<u8>, i32, u32) {
    let mut flags: u32 = 0;
    let candidates: &[i32] = if slice_rows.is_some() {
        &CANDIDATES_3D
    } else {
        &CANDIDATES_2D
    };
    let mut table: Vec<u8> = Vec::new();
    let mut pred_ids: Vec<i32> = Vec::new();
    let mut residuals: Vec<i64> = Vec::new();
    for (r, row) in rows.iter().enumerate() {
        let prev = if r > 0 { Some(rows[r - 1].as_slice()) } else { None };
        let slice_row = slice_rows.map(|s| s[r].as_slice());
        if predictor < 0 {
            let (pred_id, res) =
                best_row_predictor(row, prev, slice_row, channels, fmt.modulus, candidates);
            pred_ids.push(pred_id);
            residuals.extend_from_slice(&res);
        } else {
            let res = row_residuals(row, prev, slice_row, channels, fmt.modulus, predictor);
            residuals.extend_from_slice(&res);
        }
    }
    if predictor < 0 {
        table = pack_filter_bits(&pred_ids);
        flags |= TILE_FLAG_ADAPTIVE as u32;
    }

    let packed = if fmt.value_bytes > 1 {
        flags |= TILE_FLAG_SPLIT as u32;
        split_planes(&residuals, fmt.value_bytes)
    } else {
        residuals.iter().map(|&r| (r & 0xFF) as u8).collect()
    };

    let stream = if method == METHOD_LZ {
        let mut tokens = lz_encode(&packed);
        tokens.push(LZ_TOKEN_EOF);
        entropy_encode(&tokens, ENTROPY_SYMBOLS_LZ)
    } else {
        let symbols: Vec<u32> = packed.iter().map(|&b| b as u32).collect();
        entropy_encode(&symbols, ENTROPY_SYMBOLS_RAW)
    };

    let mut body = table;
    body.extend_from_slice(&stream);
    (body, method, flags)
}

/// Decode one tile into canonical rows.
fn decode_tile(
    body: &[u8],
    flags: u32,
    method: i32,
    tw: usize,
    th: usize,
    channels: usize,
    fmt: &MedFormat,
    slice_rows: Option<&[Vec<i64>]>,
    predictor: i32,
) -> Result<Vec<Vec<i64>>, isize> {
    let (stream, pred_ids): (&[u8], Vec<i32>) = if flags & TILE_FLAG_ADAPTIVE as u32 != 0 {
        let nbytes = (th + 1) / 2;
        if body.len() < nbytes {
            return Err(E_MED_TRUNC);
        }
        (&body[nbytes..], unpack_filter_bits(&body[..nbytes], th))
    } else {
        (body, vec![predictor; th])
    };
    if method != METHOD_RAW && method != METHOD_LZ {
        return Err(E_MED_METHOD);
    }

    let per_row = tw * channels * fmt.values_per_sample;
    let count = per_row * th;
    let packed: Vec<u8> = if method == METHOD_LZ {
        let mut dec = RangeDec::new(stream, ENTROPY_SYMBOLS_LZ);
        let mut tokens: Vec<u32> = Vec::new();
        let limit = count * fmt.value_bytes * 2 + 64;
        loop {
            let sym = dec.decode_symbol() as u32;
            tokens.push(sym);
            if sym == LZ_TOKEN_EOF {
                break;
            }
            if tokens.len() > limit {
                return Err(E_MED_LZ);
            }
        }
        lz_decode(&tokens, count * fmt.value_bytes)?
    } else {
        entropy_decode(stream, ENTROPY_SYMBOLS_RAW, count * fmt.value_bytes)
            .iter()
            .map(|&b| b as u8)
            .collect()
    };

    let residuals = if flags & TILE_FLAG_SPLIT as u32 != 0 {
        merge_planes(&packed, count, fmt.value_bytes)
    } else {
        packed[..count].iter().map(|&b| b as i64).collect()
    };

    let mut rows: Vec<Vec<i64>> = Vec::with_capacity(th);
    for r in 0..th {
        let row_res = &residuals[r * per_row..(r + 1) * per_row];
        let prev: Option<&[i64]> = if r > 0 { Some(rows[r - 1].as_slice()) } else { None };
        let slice_row = slice_rows.map(|s| s[r].as_slice());
        let mut row = vec![0i64; per_row];
        for i in 0..per_row {
            let x = i / channels;
            let left = if x > 0 { row[i - channels] } else { 0 };
            let up = match prev {
                Some(p) => p[i],
                None => 0,
            };
            let upper_left = if x > 0 {
                match prev {
                    Some(p) => p[i - channels],
                    None => 0,
                }
            } else {
                0
            };
            let slice_val = match slice_row {
                Some(s) => s[i],
                None => 0,
            };
            let p = med_predict(pred_ids[r], left, up, upper_left, slice_val);
            row[i] = med_mod(row_res[i] + p, fmt.modulus);
        }
        rows.push(row);
    }
    Ok(rows)
}

// ------------------------------------------------------------------ sha-256

const SHA_K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
    0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
    0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
    0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
    0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
    0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
    0xc67178f2,
];

/// SHA-256 of `data` (FIPS 180-4).
fn sha256(data: &[u8]) -> [u8; 32] {
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
    ];
    let bitlen = (data.len() as u64).wrapping_mul(8);
    let mut msg = Vec::with_capacity(data.len() + 72);
    msg.extend_from_slice(data);
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bitlen.to_be_bytes());
    for chunk in msg.chunks_exact(64) {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([
                chunk[4 * i],
                chunk[4 * i + 1],
                chunk[4 * i + 2],
                chunk[4 * i + 3],
            ]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let (mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh) =
            (h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7]);
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let t1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(SHA_K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }
    let mut out = [0u8; 32];
    for i in 0..8 {
        out[4 * i..4 * i + 4].copy_from_slice(&h[i].to_be_bytes());
    }
    out
}

// ----------------------------------------------------------------- encoder

/// Encode a whole medical volume and return the container bytes.
#[no_mangle]
pub unsafe extern "C" fn pyra_med_encode(
    src: *const u8,
    len: usize,
    width: u32,
    height: u32,
    depth: u32,
    channels: u32,
    sample_format: u32,
    meta: *const u8,
    meta_len: usize,
    tile_size: usize,
    predictor: i32,
    method: i32,
    out_ptr: *mut *mut u8,
    out_len: *mut usize,
) -> isize {
    let fmt = match med_format(sample_format as i32) {
        Some(f) => f,
        None => return E_MED_FORMAT,
    };
    if width == 0 || height == 0 || depth == 0 || channels == 0 || tile_size == 0 {
        return E_MED_SHAPE;
    }
    if method != -1 && method != METHOD_RAW && method != METHOD_LZ {
        return E_MED_METHOD;
    }
    if predictor < -1 || predictor > PRED_SLICE {
        return E_MED_METHOD;
    }
    let (width, height, depth, channels) = (
        width as usize,
        height as usize,
        depth as usize,
        channels as usize,
    );
    let total_bytes = width * height * depth * channels * fmt.sample_bytes;
    if src.is_null() || len < total_bytes {
        return E_MED_SHAPE;
    }
    let samples = std::slice::from_raw_parts(src, total_bytes);
    let meta_blob: &[u8] = if meta_len == 0 || meta.is_null() {
        &[]
    } else {
        std::slice::from_raw_parts(meta, meta_len)
    };

    // float samples are shuffled, not predicted
    let predictor = if fmt.is_float { PRED_NONE } else { predictor };

    let grid = tile_grid(width, height, tile_size);
    let tiles_per_slice = grid.len();

    let mut bodies: Vec<Vec<u8>> = Vec::with_capacity(tiles_per_slice * depth);
    let mut methods: Vec<u32> = Vec::with_capacity(tiles_per_slice * depth);
    let mut flags_v: Vec<u32> = Vec::with_capacity(tiles_per_slice * depth);

    let row_values = width * channels * fmt.values_per_sample;
    let mut prev_slice: Option<Vec<Vec<i64>>> = None;

    for z in 0..depth {
        let slice_offset = z * width * height * channels * fmt.sample_bytes;
        let mut slice_rows: Vec<Vec<i64>> = Vec::with_capacity(height);
        for y in 0..height {
            let base = slice_offset + y * width * channels * fmt.sample_bytes;
            slice_rows.push(canonical_row(samples, base, width * channels, &fmt));
        }
        // encode the tiles of this slice in parallel
        let prev_ref: Option<&Vec<Vec<i64>>> = prev_slice.as_ref();
        let encoded: Vec<Option<(Vec<u8>, u32, u32)>> = std::thread::scope(|scope| {
            let mut handles = Vec::with_capacity(grid.len());
            for geom in grid.iter() {
                let rows_ref = &slice_rows;
                let prev_ref = prev_ref;
                handles.push(scope.spawn(move || {
                    let lo = geom.x * channels * fmt.values_per_sample;
                    let hi = lo + geom.w * channels * fmt.values_per_sample;
                    let rows: Vec<Vec<i64>> = (0..geom.h)
                        .map(|r| rows_ref[geom.y + r][lo..hi].to_vec())
                        .collect();
                    let slice_ctx: Option<Vec<Vec<i64>>> = prev_ref.map(|p| {
                        (0..geom.h)
                            .map(|r| p[geom.y + r][lo..hi].to_vec())
                            .collect()
                    });
                    let slice_ctx_ref = slice_ctx.as_deref();
                    let (raw_body, _raw_method, raw_flags) =
                        encode_tile(&rows, channels, &fmt, slice_ctx_ref, predictor, METHOD_RAW);
                    if method == -1 {
                        let (lz_body, _, lz_flags) = encode_tile(
                            &rows,
                            channels,
                            &fmt,
                            slice_ctx_ref,
                            predictor,
                            METHOD_LZ,
                        );
                        if lz_body.len() < raw_body.len() {
                            Some((lz_body, METHOD_LZ as u32, lz_flags))
                        } else {
                            Some((raw_body, METHOD_RAW as u32, raw_flags))
                        }
                    } else if method == METHOD_LZ {
                        let (lz_body, _, lz_flags) = encode_tile(
                            &rows,
                            channels,
                            &fmt,
                            slice_ctx_ref,
                            predictor,
                            METHOD_LZ,
                        );
                        Some((lz_body, METHOD_LZ as u32, lz_flags))
                    } else {
                        Some((raw_body, METHOD_RAW as u32, raw_flags))
                    }
                }));
            }
            let mut out = Vec::with_capacity(handles.len());
            for h in handles {
                match h.join() {
                    Ok(v) => out.push(v),
                    Err(_) => out.push(None),
                }
            }
            out
        });
        for item in encoded {
            match item {
                Some((body, m, f)) => {
                    bodies.push(body);
                    methods.push(m);
                    flags_v.push(f);
                }
                None => return E_MED_ALLOC,
            }
        }
        prev_slice = Some(slice_rows);
    }
    let _ = row_values;

    // ---- assemble
    let meta_crc = crc32(meta_blob);
    let index_size = bodies.len() * MED_INDEX_ENTRY_SIZE;
    let meta_offset = MED_HEADER_SIZE;
    let index_offset = meta_offset + meta_blob.len();
    let payload_offset = index_offset + index_size;

    let mut index = Vec::with_capacity(index_size);
    let mut payload: Vec<u8> = Vec::new();
    let mut offset = payload_offset;
    for (i, body) in bodies.iter().enumerate() {
        index.extend_from_slice(&(offset as u32).to_le_bytes());
        index.extend_from_slice(&(body.len() as u32).to_le_bytes());
        index.push(methods[i] as u8);
        index.push(flags_v[i] as u8);
        index.extend_from_slice(&0u16.to_le_bytes());
        index.extend_from_slice(&crc32(body).to_le_bytes());
        payload.extend_from_slice(body);
        offset += body.len();
    }
    let payload_crc = crc32(&payload);
    let digest = sha256(samples);

    let mut header = vec![0u8; MED_HEADER_SIZE];
    header[0..8].copy_from_slice(&MED_MAGIC);
    let fields: [u32; 17] = [
        MED_VERSION,
        0, // flags
        width as u32,
        height as u32,
        depth as u32,
        channels as u32,
        sample_format,
        8 * fmt.sample_bytes as u32,
        tile_size as u32,
        bodies.len() as u32,
        index_offset as u32,
        index_size as u32,
        meta_offset as u32,
        meta_blob.len() as u32,
        meta_crc,
        payload_crc,
        0, // header crc placeholder
    ];
    for (i, f) in fields.iter().enumerate() {
        header[8 + i * 4..12 + i * 4].copy_from_slice(&f.to_le_bytes());
    }
    let predictor_id = if predictor >= 0 { predictor } else { 0 };
    header[76..80].copy_from_slice(&(predictor_id as u32).to_le_bytes());
    header[80..112].copy_from_slice(&digest);
    let hdr_crc = crc32(&header[0..MED_HEADER_CRC_OFFSET]);
    header[72..76].copy_from_slice(&hdr_crc.to_le_bytes());

    let mut out = Vec::with_capacity(
        MED_HEADER_SIZE + meta_blob.len() + index_size + payload.len(),
    );
    out.extend_from_slice(&header);
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

struct MedHeader {
    width: usize,
    height: usize,
    depth: usize,
    channels: usize,
    sample_format: i32,
    bits: u32,
    tile_size: usize,
    num_tiles: usize,
    index_offset: usize,
    index_size: usize,
    meta_offset: usize,
    meta_size: usize,
    meta_crc: u32,
    payload_crc: u32,
    predictor: i32,
    sha256: [u8; 32],
}

fn rd_u32(data: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([data[off], data[off + 1], data[off + 2], data[off + 3]])
}

fn parse_header(data: &[u8]) -> Result<MedHeader, isize> {
    if data.len() < MED_HEADER_SIZE {
        return Err(E_MED_HEADER);
    }
    if data[0..8] != MED_MAGIC {
        return Err(E_MED_HEADER);
    }
    let version = rd_u32(data, 8);
    if version != MED_VERSION {
        return Err(E_MED_HEADER);
    }
    let width = rd_u32(data, 16) as usize;
    let height = rd_u32(data, 20) as usize;
    let depth = rd_u32(data, 24) as usize;
    let channels = rd_u32(data, 28) as usize;
    let sample_format = rd_u32(data, 32) as i32;
    let bits = rd_u32(data, 36);
    let tile_size = rd_u32(data, 40) as usize;
    let num_tiles = rd_u32(data, 44) as usize;
    let index_offset = rd_u32(data, 48) as usize;
    let index_size = rd_u32(data, 52) as usize;
    let meta_offset = rd_u32(data, 56) as usize;
    let meta_size = rd_u32(data, 60) as usize;
    let meta_crc = rd_u32(data, 64);
    let payload_crc = rd_u32(data, 68);
    let header_crc = rd_u32(data, 72);
    let predictor = rd_u32(data, 76) as i32;
    if width == 0 || height == 0 || depth == 0 || channels == 0 || tile_size == 0 {
        return Err(E_MED_HEADER);
    }
    if crc32(&data[0..MED_HEADER_CRC_OFFSET]) != header_crc {
        return Err(E_MED_CRC);
    }
    if index_size != num_tiles * MED_INDEX_ENTRY_SIZE {
        return Err(E_MED_HEADER);
    }
    let mut sha = [0u8; 32];
    sha.copy_from_slice(&data[80..112]);
    Ok(MedHeader {
        width,
        height,
        depth,
        channels,
        sample_format,
        bits,
        tile_size,
        num_tiles,
        index_offset,
        index_size,
        meta_offset,
        meta_size,
        meta_crc,
        payload_crc,
        predictor,
        sha256: sha,
    })
}

/// Decode a whole container into the raw sample bytes.
#[no_mangle]
pub unsafe extern "C" fn pyra_med_decode(
    src: *const u8,
    len: usize,
    out_ptr: *mut *mut u8,
    out_len: *mut usize,
) -> isize {
    if src.is_null() {
        return E_MED_HEADER;
    }
    let data = std::slice::from_raw_parts(src, len);
    let hdr = match parse_header(data) {
        Ok(h) => h,
        Err(e) => return e,
    };
    let fmt = match med_format(hdr.sample_format) {
        Some(f) => f,
        None => return E_MED_FORMAT,
    };
    if hdr.bits != 8 * fmt.sample_bytes as u32 {
        return E_MED_HEADER;
    }
    // metadata block
    if hdr.meta_offset + hdr.meta_size > data.len() {
        return E_MED_TRUNC;
    }
    let meta_blob = &data[hdr.meta_offset..hdr.meta_offset + hdr.meta_size];
    if crc32(meta_blob) != hdr.meta_crc {
        return E_MED_CRC;
    }
    // tile index
    if hdr.index_offset + hdr.index_size > data.len() {
        return E_MED_TRUNC;
    }
    let index = &data[hdr.index_offset..hdr.index_offset + hdr.index_size];
    let grid = tile_grid(hdr.width, hdr.height, hdr.tile_size);
    let tiles_per_slice = grid.len();
    if hdr.num_tiles != tiles_per_slice * hdr.depth {
        return E_MED_HEADER;
    }

    let total_bytes = hdr.width * hdr.height * hdr.depth * hdr.channels * fmt.sample_bytes;
    let mut out: Vec<u8> = vec![0u8; total_bytes];
    let row_bytes = hdr.width * hdr.channels * fmt.sample_bytes;
    let mut payload: Vec<u8> = Vec::new();
    let mut prev_slice: Option<Vec<Vec<i64>>> = None;

    for z in 0..hdr.depth {
        let mut slice_rows: Vec<Vec<i64>> =
            vec![vec![0i64; hdr.width * hdr.channels * fmt.values_per_sample]; hdr.height];
        let prev_ref: Option<&Vec<Vec<i64>>> = prev_slice.as_ref();
        let decoded: Vec<Option<Result<Vec<Vec<i64>>, isize>>> = std::thread::scope(|scope| {
            let mut handles = Vec::with_capacity(grid.len());
            for (t, geom) in grid.iter().enumerate() {
                let entry_off = (z * tiles_per_slice + t) * MED_INDEX_ENTRY_SIZE;
                let tile_off = rd_u32(index, entry_off) as usize;
                let tile_len = rd_u32(index, entry_off + 4) as usize;
                let method = index[entry_off + 8] as i32;
                let flags = index[entry_off + 9] as u32;
                let crc = rd_u32(index, entry_off + 12);
                let prev_ref = prev_ref;
                handles.push(scope.spawn(move || {
                    if tile_off + tile_len > data.len() {
                        return Err(E_MED_TRUNC);
                    }
                    let body = &data[tile_off..tile_off + tile_len];
                    if crc32(body) != crc {
                        return Err(E_MED_CRC);
                    }
                    let prev_ref = prev_ref;
                    let lo = geom.x * hdr.channels * fmt.values_per_sample;
                    let hi = lo + geom.w * hdr.channels * fmt.values_per_sample;
                    let slice_ctx: Option<Vec<Vec<i64>>> = prev_ref.map(|p| {
                        (0..geom.h)
                            .map(|r| p[geom.y + r][lo..hi].to_vec())
                            .collect()
                    });
                    decode_tile(
                        body,
                        flags,
                        method,
                        geom.w,
                        geom.h,
                        hdr.channels,
                        &fmt,
                        slice_ctx.as_deref(),
                        hdr.predictor,
                    )
                }));
            }
            let mut res = Vec::with_capacity(handles.len());
            for h in handles {
                match h.join() {
                    Ok(v) => res.push(Some(v)),
                    Err(_) => res.push(None),
                }
            }
            res
        });
        for (t, item) in decoded.iter().enumerate() {
            let geom = &grid[t];
            let item = match item {
                Some(r) => r,
                None => return E_MED_ALLOC,
            };
            let rows = match item {
                Ok(r) => r,
                Err(e) => return *e,
            };
            let lo = geom.x * hdr.channels * fmt.values_per_sample;
            for (r, row) in rows.iter().enumerate() {
                slice_rows[geom.y + r][lo..lo + row.len()].copy_from_slice(row);
                let byte_off =
                    (z * hdr.height + geom.y + r) * row_bytes + geom.x * hdr.channels * fmt.sample_bytes;
                let bytes = decode_row(row, &fmt);
                out[byte_off..byte_off + bytes.len()].copy_from_slice(&bytes);
            }
        }
        // keep the payload CRC over the tile bodies in container order
        for (t, _geom) in grid.iter().enumerate() {
            let entry_off = (z * tiles_per_slice + t) * MED_INDEX_ENTRY_SIZE;
            let tile_off = rd_u32(index, entry_off) as usize;
            let tile_len = rd_u32(index, entry_off + 4) as usize;
            if tile_off + tile_len <= data.len() {
                payload.extend_from_slice(&data[tile_off..tile_off + tile_len]);
            }
        }
        prev_slice = Some(slice_rows);
    }

    if crc32(&payload) != hdr.payload_crc {
        return E_MED_CRC;
    }
    if sha256(&out) != hdr.sha256 {
        return E_MED_CRC;
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
pub unsafe extern "C" fn pyra_med_info(
    src: *const u8,
    len: usize,
    meta_out: *mut i32,
) -> isize {
    if src.is_null() {
        return E_MED_HEADER;
    }
    let data = std::slice::from_raw_parts(src, len);
    let hdr = match parse_header(data) {
        Ok(h) => h,
        Err(e) => return e,
    };
    let out = std::slice::from_raw_parts_mut(meta_out, 8);
    out[0] = hdr.width as i32;
    out[1] = hdr.height as i32;
    out[2] = hdr.depth as i32;
    out[3] = hdr.channels as i32;
    out[4] = hdr.sample_format;
    out[5] = hdr.bits as i32;
    out[6] = hdr.tile_size as i32;
    out[7] = hdr.num_tiles as i32;
    0
}
