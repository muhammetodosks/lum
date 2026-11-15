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
//!
//! Performance notes
//! -----------------
//! * Every tile of every slice is an independent job: the slice context of
//!   the `slice` predictor is derived from the *input* samples, so the
//!   encoder needs no state from the previous slice and can spread all
//!   tiles of the whole volume over the cores.
//! * The decoder splits into two phases. Phase one decodes the entropy
//!   streams of all tiles (no context needed) in parallel; phase two
//!   reconstructs the rows slice by slice, which is cheap.
//! * The two method candidates of a tile share the residual stream, and the
//!   LZ candidate is encoded under a byte budget: as soon as it grows past
//!   the raw candidate it is provably larger and is dropped.
//! * The predictor candidates are scored in a single pass over the row and
//!   the modular arithmetic avoids the hardware division.

use super::pyra_image::crc32;
use super::{CtxRangeDec, CtxRangeEnc};
use super::FastBuild;
use super::RangeDec;
use super::RangeEnc;
use std::collections::HashMap;

// ------------------------------------------------------------------ format

const MED_MAGIC: [u8; 8] = [0x50, 0x59, 0x52, 0x4D, 0x0D, 0x0A, 0x1A, 0x0A];
const MED_VERSION: u32 = 3;
const MED_VERSION_V2: u32 = 2;
const MED_VERSION_V1: u32 = 1;
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
const PRED_MED: i32 = 8;
const PRED_COUNT: i32 = 9;
const CANDIDATES_2D: [i32; 8] = [0, 1, 2, 3, 4, 5, 6, 8];
const CANDIDATES_3D: [i32; 9] = [0, 1, 2, 3, 4, 5, 6, 8, 7];

const FILTER_BITS_PER_ROW: u32 = 4;

const METHOD_RAW: i32 = 0;
const METHOD_LZ: i32 = 1;

const TILE_FLAG_SPLIT: u8 = 0x01;
const TILE_FLAG_ADAPTIVE: u8 = 0x02;
const TILE_FLAG_ESCAPE: u8 = 0x04;
const TILE_FLAG_CONTEXT: u8 = 0x08;

/// Wide samples are coded with one symbol per sample: the centred residual
/// is zig-zag encoded into 0..127 and the rare large residuals escape into
/// a fixed number of 7 bit chunks.
const ESCAPE_SYMBOL: u32 = 128;
const ESCAPE_ALPHABET: usize = 129;
const ZIGZAG_LIMIT: i64 = 63;
const ESCAPE_CHUNK_BITS: u32 = 7;

const LZ_MIN_MATCH: usize = 4;
const LZ_MAX_MATCH: usize = 259;
const LZ_MAX_DIST: usize = 65536;
const LZ_TOKEN_MATCH: u32 = 256;
const LZ_TOKEN_EOF: u32 = 257;
const LZ_TOKEN_SYMBOLS: usize = 258;
const LZ_CHAIN_LIMIT: usize = 16;

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
    /// Wide integer samples use the zig-zag + escape coding.
    escape: bool,
    /// Number of 7 bit chunks an escaped residual is made of.
    escape_chunks: usize,
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
            escape: false,
            escape_chunks: 0,
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
            escape: sample_bytes > 1,
            escape_chunks: ((8 * sample_bytes as u32 + ESCAPE_CHUNK_BITS - 1)
                / ESCAPE_CHUNK_BITS) as usize,
        })
    }
}

// ------------------------------------------------------- modular arithmetic

/// Normalise a value into `[0, m)`.
///
/// The residual modulus is always a power of two (2^bits), so the reduction
/// is a mask: no division and, more importantly, no data dependent branch —
/// the sign of a residual is data dependent, and a mispredicted branch costs
/// more than the whole arithmetic.
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

#[inline]
fn med_median(a: i64, b: i64, c: i64) -> i64 {
    if c >= a.max(b) {
        a.min(b)
    } else if c <= a.min(b) {
        a.max(b)
    } else {
        a + b - c
    }
}

/// Predicted value for one sample.
///
/// `left`/`up`/`upper_left` come from the same row/column inside the tile,
/// `slice_val` is the same sample in the previous slice (0 for the first
/// slice).
#[inline]
/// Distance of two code values on the modular ring (0 and modulus-1 are
/// neighbours, so the plain difference is not the distance).
#[inline]
fn circular_distance(a: i64, b: i64, modulus: i64) -> i64 {
    let d = (a - b).abs();
    if d > modulus - d {
        modulus - d
    } else {
        d
    }
}

/// The median edge detector of LOCO-I / JPEG-LS: the neighbour whose
/// difference to the other two is the smallest wins, so the prediction
/// follows an edge instead of averaging across it.
#[inline]
fn med_edge(left: i64, up: i64, upper_left: i64, modulus: i64) -> i64 {
    let ma = circular_distance(left, upper_left, modulus);
    let mb = circular_distance(up, upper_left, modulus);
    let mc = circular_distance(up, left, modulus);
    if ma < mb && ma < mc {
        left
    } else if mb < mc {
        up
    } else {
        upper_left
    }
}

fn med_predict(pred: i32, left: i64, up: i64, upper_left: i64, slice_val: i64,
               modulus: i64) -> i64 {
    match pred {
        0 => 0,                       // NONE
        1 => left,                    // LEFT
        2 => up,                      // UP
        3 => (left + up) >> 1,        // AVERAGE
        4 => med_paeth(left, up, upper_left),
        5 => left + up - upper_left,  // GRADIENT
        6 => med_median(left, up, upper_left),
        7 => slice_val,              // SLICE (volumetric)
        8 => med_edge(left, up, upper_left, modulus),
        _ => 0,
    }
}

/// Score every predictor candidate of a row in a single pass.
///
/// The neighbour context is loaded once per sample instead of once per
/// candidate, and no residual array is written: the winner is computed
/// afterwards. Ties keep the first candidate, exactly like the reference.
fn score_candidates(
    row: &[u32],
    prev: Option<&[u32]>,
    slice_row: Option<&[u32]>,
    channels: usize,
    modulus: i64,
    candidates: &[i32],
) -> (i32, [i64; 9]) {
    let mut scores = [0i64; 9];
    let n = row.len();
    let per_pixel = n / channels.max(1);
    let mut x = 0usize;
    for _ in 0..per_pixel {
        let mut c = 0usize;
        while c < channels {
            let i = x * channels + c;
            let left = if x > 0 { row[i - channels] as i64 } else { 0 };
            let up = match prev {
                Some(p) => p[i] as i64,
                None => 0,
            };
            let upper_left = if x > 0 {
                match prev {
                    Some(p) => p[i - channels] as i64,
                    None => 0,
                }
            } else {
                0
            };
            let slice_val = match slice_row {
                Some(s) => s[i] as i64,
                None => 0,
            };
            let value = row[i] as i64;
            for (k, &pred) in candidates.iter().enumerate() {
                let p = mod_norm(med_predict(pred, left, up, upper_left, slice_val, modulus), modulus);
                scores[k] += residual_distance(mod_norm(value - p, modulus), modulus);
            }
            c += 1;
        }
        x += 1;
    }
    let mut best = candidates[0];
    let mut best_score = -1i64;
    for (k, &pred) in candidates.iter().enumerate() {
        if best_score < 0 || scores[k] < best_score {
            best_score = scores[k];
            best = pred;
        }
    }
    (best, scores)
}

/// Residuals of one row for one predictor (modular alphabet).
fn row_residuals(
    row: &[u32],
    prev: Option<&[u32]>,
    slice_row: Option<&[u32]>,
    channels: usize,
    modulus: i64,
    pred: i32,
) -> Vec<u32> {
    let n = row.len();
    let mut out = vec![0u32; n];
    let per_pixel = n / channels.max(1);
    let mut x = 0usize;
    for _ in 0..per_pixel {
        let mut c = 0usize;
        while c < channels {
            let i = x * channels + c;
            let left = if x > 0 { row[i - channels] as i64 } else { 0 };
            let up = match prev {
                Some(p) => p[i] as i64,
                None => 0,
            };
            let upper_left = if x > 0 {
                match prev {
                    Some(p) => p[i - channels] as i64,
                    None => 0,
                }
            } else {
                0
            };
            let slice_val = match slice_row {
                Some(s) => s[i] as i64,
                None => 0,
            };
            let p = mod_norm(med_predict(pred, left, up, upper_left, slice_val, modulus), modulus);
            out[i] = mod_norm(row[i] as i64 - p, modulus) as u32;
            c += 1;
        }
        x += 1;
    }
    out
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
fn split_planes(res: &[u32], value_bytes: usize) -> Vec<u8> {
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

// ------------------------------------------------------------------ samples

/// Read `count` samples and map them into the unsigned code domain.
fn canonical_range(src: &[u8], offset: usize, count: usize, fmt: &MedFormat) -> Vec<u32> {
    let mut out = Vec::with_capacity(count * fmt.values_per_sample);
    if fmt.is_float {
        for i in 0..count {
            let b = offset + i * 4;
            for plane in 0..4 {
                out.push(src[b + plane] as u32);
            }
        }
        return out;
    }
    let sb = fmt.sample_bytes;
    let canon = fmt.canon_offset;
    let bits = 8 * sb;
    for i in 0..count {
        let b = offset + i * sb;
        let mut raw: u64 = 0;
        for k in 0..sb {
            raw |= (src[b + k] as u64) << (8 * k);
        }
        let value: i64 = if fmt.signed && (raw & (1u64 << (bits - 1))) != 0 {
            (raw as i64) - (1i64 << bits)
        } else {
            raw as i64
        };
        out.push(((value + canon) as u64 & 0xFFFF_FFFF) as u32);
    }
    out
}

/// Inverse of `canonical_range`.
fn decode_row(values: &[u32], fmt: &MedFormat) -> Vec<u8> {
    if fmt.is_float {
        return values.iter().map(|&v| (v & 0xFF) as u8).collect();
    }
    let sb = fmt.sample_bytes;
    let canon = fmt.canon_offset;
    let mask: u64 = if sb == 8 {
        u64::MAX
    } else {
        (1u64 << (8 * sb)) - 1
    };
    let mut out = Vec::with_capacity(values.len() * sb);
    for &v in values {
        let bits = ((v as i64 - canon) as u64) & mask;
        for k in 0..sb {
            out.push(((bits >> (8 * k)) & 0xFF) as u8);
        }
    }
    out
}

/// Decide whether the escape coding beats the byte plane coding.
///
/// The escape coding needs one symbol per sample, but a residual that does
/// not fit into +-63 costs one marker plus `escape_chunks` chunk symbols.
/// It only pays off when the residuals are concentrated around zero, which
/// is the normal case for real medical data. For noisy data the byte planes
/// are cheaper, so the encoder measures the escape rate and picks the
/// better of the two. The rule is a fixed fraction, so both backends agree.
fn escape_worth_it(residuals: &[u32], fmt: &MedFormat) -> bool {
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
///
/// A residual that fits into +-63 is zig-zag encoded into 0..127. Anything
/// larger escapes: the marker 128 is followed by a fixed number of 7 bit
/// chunks that carry the two's complement bit pattern of the value.
fn escape_symbols(residuals: &[u32], fmt: &MedFormat) -> Vec<u32> {
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
    fmt: &MedFormat,
) -> Result<Vec<u32>, isize> {
    let mut out: Vec<u32> = Vec::with_capacity(count);
    let bits = 8 * fmt.sample_bytes as u32;
    let sign_bit = 1u64 << (bits - 1);
    let modulus = 1u64 << bits;
    while out.len() < count {
        let sym = match it.next() {
            Some(s) => s,
            None => return Err(E_MED_TRUNC),
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
                    None => return Err(E_MED_TRUNC),
                };
                if part >= ESCAPE_SYMBOL {
                    return Err(E_MED_LZ);
                }
                raw |= (part as u64) << (ESCAPE_CHUNK_BITS as usize * k);
            }
            if raw & sign_bit != 0 {
                (raw as i64) - (modulus as i64)
            } else {
                raw as i64
            }
        } else {
            return Err(E_MED_LZ);
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

/// Decode `count` symbols into bytes (the raw method only ever produces
/// byte sized symbols).
/// Code a byte stream with one adaptive model per byte plane.
///
/// The high planes of a wide sample are long runs of zero bytes; a separate
/// model per plane learns that immediately. The decoder knows the plane
/// boundaries, so the stream stays self-describing.
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

/// Inverse of [`entropy_encode_planes`].
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

fn entropy_decode_bytes(data: &[u8], n_symbols: usize, count: usize) -> Vec<u8> {
    let mut dec = RangeDec::new(data, n_symbols);
    let mut out = Vec::with_capacity(count);
    for _ in 0..count {
        out.push(dec.decode_symbol() as u8);
    }
    out
}

// ---------------------------------------------------------------------- lz

enum LzTok {
    Lit(u8),
    Match { len: usize, dist: usize },
    Eof,
}

/// Greedy LZ77 matcher with 3 byte hash chains.
///
/// The token sequence is identical to the pure-Python reference: the newest
/// The hash table of the LZ matcher: a fixed size array of the most recent
/// position per key hash plus a "previous position with the same hash" array
/// (the classic zlib layout).
///
/// The map used to be a `HashMap` with a heap allocated chain per key: for a
/// medical volume that is millions of allocations and a table far larger
/// than the cache. The fixed table has no allocation at all and its working
/// set stays small.
///
/// The chain order and the FIFO eviction are unchanged, so the token stream
/// is bit-for-bit the same as the one the Python reference produces.
const LZ_HASH_BITS: u32 = 16;
const LZ_HASH_SIZE: usize = 1 << LZ_HASH_BITS;

/// Fibonacci hashing of a 24 bit key into the table.
#[inline]
fn lz_hash(key: u32) -> usize {
    (key.wrapping_mul(2654435761) >> (32 - LZ_HASH_BITS)) as usize
}

/// candidate wins ties, buckets are capped at `LZ_CHAIN_LIMIT` positions
/// with FIFO eviction and a match must be at least four bytes long.
struct MedLz<'a> {
    data: &'a [u8],
    /// most recent position per key hash (-1 = empty)
    heads: Vec<i32>,
    /// previous position with the same hash
    prev: Vec<i32>,
    pos: usize,
}

impl<'a> MedLz<'a> {
    fn new(data: &'a [u8]) -> Self {
        MedLz {
            data,
            heads: vec![-1; LZ_HASH_SIZE],
            prev: vec![-1; data.len() + 1],
            pos: 0,
        }
    }

    #[inline]
    fn key_at(&self, pos: usize) -> u32 {
        ((self.data[pos] as u32) << 16)
            | ((self.data[pos + 1] as u32) << 8)
            | (self.data[pos + 2] as u32)
    }

    /// Remember the 3 byte key at `pos` (if a full key fits).
    #[inline]
    fn add_key(&mut self, pos: usize) {
        if pos + 3 <= self.data.len() {
            let h = lz_hash(self.key_at(pos));
            self.prev[pos] = self.heads[h];
            self.heads[h] = pos as i32;
        }
    }

    /// The next token of the stream.
    fn next_token(&mut self) -> LzTok {
        let len = self.data.len();
        let i = self.pos;
        if i >= len {
            return LzTok::Eof;
        }
        let mut best_len = 0usize;
        let mut best_dist = 0usize;
        if i + LZ_MIN_MATCH <= len {
            let h = lz_hash(self.key_at(i));
            let max_len = if LZ_MAX_MATCH < len - i { LZ_MAX_MATCH } else { len - i };
            let mut cand = self.heads[h];
            let mut chain = 0usize;
            while cand >= 0 && chain < LZ_CHAIN_LIMIT {
                let c = cand as usize;
                let dist = i - c;
                if dist == 0 || dist > LZ_MAX_DIST {
                    break;
                }
                let l = super::lz_match_len(self.data, c, i, max_len);
                if l > best_len {
                    best_len = l;
                    best_dist = dist;
                    if l >= max_len {
                        break;
                    }
                }
                cand = self.prev[c];
                chain += 1;
            }
        }
        if best_len >= LZ_MIN_MATCH {
            for k in i..i + best_len {
                self.add_key(k);
            }
            self.pos = i + best_len;
            LzTok::Match {
                len: best_len,
                dist: best_dist,
            }
        } else {
            self.add_key(i);
            self.pos = i + 1;
            LzTok::Lit(self.data[i])
        }
    }
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

/// Encode the LZ method (token stream plus end marker) with a byte budget.
///
/// `max_bits` is the number of bits the candidate may use. The token stream
/// is produced and coded in one pass, so an oversized candidate is dropped
/// before the whole stream is tokenised. The final size of a stream is never
/// smaller than the bits emitted so far, so an abandoned candidate can never
/// become the winner.
fn encode_lz_capped(packed: &[u8], max_bits: usize) -> Option<Vec<u8>> {
    let mut enc = RangeEnc::new(ENTROPY_SYMBOLS_LZ, packed.len() * 2 + 64);
    let mut matcher = MedLz::new(packed);
    loop {
        match matcher.next_token() {
            LzTok::Eof => {
                enc.push(LZ_TOKEN_EOF as usize);
                break;
            }
            LzTok::Lit(b) => enc.push(b as usize),
            LzTok::Match { len, dist } => {
                enc.push(LZ_TOKEN_MATCH as usize);
                enc.push(len - LZ_MIN_MATCH);
                enc.push((dist - 1) >> 8);
                enc.push((dist - 1) & 0xFF);
            }
        }
        if enc.bit_len() > max_bits {
            return None;
        }
    }
    Some(enc.finish())
}

/// Rebuild the byte stream from an LZ77 symbol list, decoding until the
/// end-of-stream marker. `max_len` bounds the output.
///
/// The escape coding uses this variant because the length of the symbol
/// stream is only known while it is being decoded.
fn lz_decode_bounded(symbols: &[u32], max_len: usize) -> Result<Vec<u8>, isize> {
    let mut out: Vec<u8> = Vec::with_capacity(max_len.min(1 << 20));
    let mut pos = 0usize;
    let n = symbols.len();
    while pos < n {
        let sym = symbols[pos];
        pos += 1;
        if sym == LZ_TOKEN_EOF {
            break;
        }
        if sym < 256 {
            if out.len() >= max_len {
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
        if out.len() + length > max_len {
            return Err(E_MED_LZ);
        }
        for _ in 0..length {
            let b = out[out.len() - dist];
            out.push(b);
        }
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

/// The predictor choice and the residual stream of one tile.
struct TilePrep {
    residuals: Vec<u32>,
    pred_ids: Vec<i32>,
    table: Vec<u8>,
    flags: u32,
}

/// Predict one tile and pack its residuals.
fn prepare_tile(
    rows: &[u32],
    per_row: usize,
    th: usize,
    channels: usize,
    fmt: &MedFormat,
    slice_rows: Option<&[u32]>,
    predictor: i32,
) -> TilePrep {
    let mut flags: u32 = 0;
    let candidates: &[i32] = if slice_rows.is_some() {
        &CANDIDATES_3D
    } else {
        &CANDIDATES_2D
    };
    let mut table: Vec<u8> = Vec::new();
    let mut pred_ids: Vec<i32> = Vec::new();
    let mut residuals: Vec<u32> = Vec::with_capacity(per_row * th);
    for r in 0..th {
        let row = &rows[r * per_row..(r + 1) * per_row];
        let prev: Option<&[u32]> = if r > 0 {
            Some(&rows[(r - 1) * per_row..r * per_row])
        } else {
            None
        };
        let slice_row = slice_rows.map(|s| &s[r * per_row..(r + 1) * per_row]);
        if predictor < 0 {
            let (pred_id, _) = score_candidates(row, prev, slice_row, channels,
                                               fmt.modulus, candidates);
            pred_ids.push(pred_id);
            let res = row_residuals(row, prev, slice_row, channels, fmt.modulus, pred_id);
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
    TilePrep {
        residuals,
        pred_ids,
        table,
        flags,
    }
}

/// Rebuild the canonical rows of one tile from its residuals.
fn reconstruct_tile(
    residuals: &[u32],
    pred_ids: &[i32],
    per_row: usize,
    th: usize,
    channels: usize,
    fmt: &MedFormat,
    slice_rows: Option<&[u32]>,
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
        let slice_row = slice_rows.map(|s| &s[r * per_row..(r + 1) * per_row]);
        let pred = pred_ids[r];
        let mut x = 0usize;
        let per_pixel = per_row / channels.max(1);
        for _ in 0..per_pixel {
            let mut c = 0usize;
            while c < channels {
                let i = x * channels + c;
                let left = if x > 0 { row_out[i - channels] as i64 } else { 0 };
                let up = match prev {
                    Some(p) => p[i] as i64,
                    None => 0,
                };
                let upper_left = if x > 0 {
                    match prev {
                        Some(p) => p[i - channels] as i64,
                        None => 0,
                    }
                } else {
                    0
                };
                let slice_val = match slice_row {
                    Some(s) => s[i] as i64,
                    None => 0,
                };
                let p = med_predict(pred, left, up, upper_left, slice_val, fmt.modulus);
                row_out[i] = mod_norm(row_res[i] as i64 + p, fmt.modulus) as u32;
                c += 1;
            }
            x += 1;
        }
    }
    rows
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
pub fn sha256(data: &[u8]) -> [u8; 32] {
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

// --------------------------------------------------------------- parallelism

/// Run `f` over `0..n` on the available cores, preserving the order.
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

/// One encoded tile of the volume.
struct TileBody {
    body: Vec<u8>,
    method: u32,
    flags: u32,
}

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
    if predictor < -1 || predictor >= PRED_COUNT {
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
    let vps = fmt.values_per_sample;

    // Every tile of every slice is an independent job: the slice context is
    // taken from the input samples, so no state crosses a slice boundary.
    let tasks = depth * tiles_per_slice;
    let encoded: Vec<TileBody> = parallel_map(tasks, |task| {
        let z = task / tiles_per_slice;
        let t = task % tiles_per_slice;
        let geom = &grid[t];
        let per_row = geom.w * channels * vps;
        let slice_stride = width * height * channels * fmt.sample_bytes;
        let row_stride = width * channels * fmt.sample_bytes;

        // the canonical rows of this slice (tile columns only)
        let mut rows: Vec<u32> = Vec::with_capacity(per_row * geom.h);
        for r in 0..geom.h {
            let base = z * slice_stride + (geom.y + r) * row_stride
                + geom.x * channels * fmt.sample_bytes;
            rows.extend_from_slice(&canonical_range(
                samples,
                base,
                geom.w * channels,
                &fmt,
            ));
        }

        // the slice context of the previous slice (only needed when the
        // volumetric predictor is a candidate or is used)
        let slice_ctx: Option<Vec<u32>> = if z > 0
            && (predictor < 0 || predictor == PRED_SLICE)
        {
            let mut ctx: Vec<u32> = Vec::with_capacity(per_row * geom.h);
            for r in 0..geom.h {
                let base = (z - 1) * slice_stride + (geom.y + r) * row_stride
                    + geom.x * channels * fmt.sample_bytes;
                ctx.extend_from_slice(&canonical_range(
                    samples,
                    base,
                    geom.w * channels,
                    &fmt,
                ));
            }
            Some(ctx)
        } else {
            None
        };

        let prep = prepare_tile(
            &rows,
            per_row,
            geom.h,
            channels,
            &fmt,
            slice_ctx.as_deref(),
            predictor,
        );
        let use_escape = fmt.escape && escape_worth_it(&prep.residuals, &fmt);
        // the escape candidate: one symbol per sample (zig-zag plus escape)
        let mut escape_syms: Vec<u32> = Vec::new();
        let mut escape_bytes: Vec<u8> = Vec::new();
        if use_escape {
            escape_syms = escape_symbols(&prep.residuals, &fmt);
            escape_bytes = escape_syms.iter().map(|&s| s as u8).collect();
        }
        // the byte plane candidate (only needed when the escape coding is
        // not used)
        let mut plane_bytes: Vec<u8> = Vec::new();
        let mut plane_flags: u32 = 0;
        if !use_escape {
            if fmt.value_bytes > 1 {
                plane_bytes = split_planes(&prep.residuals, fmt.value_bytes);
                plane_flags = TILE_FLAG_SPLIT as u32;
            } else {
                plane_bytes = prep
                    .residuals
                    .iter()
                    .map(|&r| (r & 0xFF) as u8)
                    .collect();
            }
        }

        // The raw candidate: the escape coding when the residuals are
        // concentrated around zero, the byte planes with one model per
        // plane otherwise.
        let mut raw_stream: Vec<u8> = Vec::new();
        let mut raw_flags: u32 = 0;
        if method != METHOD_LZ {
            if use_escape {
                raw_stream = entropy_encode(&escape_syms, ESCAPE_ALPHABET);
                raw_flags = TILE_FLAG_ESCAPE as u32;
            } else {
                raw_stream = entropy_encode_planes(
                    &plane_bytes,
                    ENTROPY_SYMBOLS_RAW,
                    fmt.value_bytes,
                );
                raw_flags = plane_flags | TILE_FLAG_CONTEXT as u32;
            }
        }

        // the bytes the LZ candidate compresses
        let lz_source: Vec<u8> = if use_escape {
            escape_bytes
        } else {
            plane_bytes
        };
        let lz_flags: u32 = if use_escape {
            TILE_FLAG_ESCAPE as u32
        } else {
            plane_flags
        };
        // the LZ candidate; with the automatic method it only gets a byte
        // budget of the raw candidate, because a bigger stream can never win
        let lz_stream: Option<Vec<u8>> = if method == METHOD_RAW {
            None
        } else if method == METHOD_LZ {
            encode_lz_capped(&lz_source, usize::MAX)
        } else {
            encode_lz_capped(&lz_source, raw_stream.len().saturating_mul(8))
        };

        let (stream, used_method, extra_flag) = if method == METHOD_LZ {
            (lz_stream.unwrap_or_default(), METHOD_LZ, lz_flags)
        } else if method == METHOD_RAW {
            (raw_stream, METHOD_RAW, raw_flags)
        } else {
            match lz_stream {
                Some(lz) if lz.len() < raw_stream.len() => (lz, METHOD_LZ, lz_flags),
                _ => (raw_stream, METHOD_RAW, raw_flags),
            }
        };
        let flags = prep.flags | extra_flag;

        let mut body = prep.table;
        body.extend_from_slice(&stream);
        TileBody {
            body,
            method: used_method as u32,
            flags,
        }
    });

    // ---- assemble
    let meta_crc = crc32(meta_blob);
    let index_size = encoded.len() * MED_INDEX_ENTRY_SIZE;
    let meta_offset = MED_HEADER_SIZE;
    let index_offset = meta_offset + meta_blob.len();
    let payload_offset = index_offset + index_size;

    let mut index = Vec::with_capacity(index_size);
    let mut payload: Vec<u8> = Vec::new();
    let mut offset = payload_offset;
    for item in encoded.iter() {
        index.extend_from_slice(&(offset as u32).to_le_bytes());
        index.extend_from_slice(&(item.body.len() as u32).to_le_bytes());
        index.push(item.method as u8);
        index.push(item.flags as u8);
        index.extend_from_slice(&0u16.to_le_bytes());
        index.extend_from_slice(&crc32(&item.body).to_le_bytes());
        payload.extend_from_slice(&item.body);
        offset += item.body.len();
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
        encoded.len() as u32,
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
    if version != MED_VERSION && version != MED_VERSION_V2 && version != MED_VERSION_V1 {
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
    let vps = fmt.values_per_sample;

    let total_bytes = hdr.width * hdr.height * hdr.depth * hdr.channels * fmt.sample_bytes;
    let mut out: Vec<u8> = vec![0u8; total_bytes];
    let row_bytes = hdr.width * hdr.channels * fmt.sample_bytes;

    // ---- phase one: decode every entropy stream (no context needed)
    let tasks = hdr.depth * tiles_per_slice;
    let decoded: Vec<Result<(Vec<u32>, Vec<i32>), isize>> = parallel_map(tasks, |task| {
        let z = task / tiles_per_slice;
        let t = task % tiles_per_slice;
        let geom = &grid[t];
        let entry_off = task * MED_INDEX_ENTRY_SIZE;
        let tile_off = rd_u32(index, entry_off) as usize;
        let tile_len = rd_u32(index, entry_off + 4) as usize;
        let method = index[entry_off + 8] as i32;
        let flags = index[entry_off + 9] as u32;
        let crc = rd_u32(index, entry_off + 12);
        if tile_off + tile_len > data.len() {
            return Err(E_MED_TRUNC);
        }
        let body = &data[tile_off..tile_off + tile_len];
        if crc32(body) != crc {
            return Err(E_MED_CRC);
        }
        let _ = z;
        let (stream, pred_ids): (&[u8], Vec<i32>) =
            if flags & TILE_FLAG_ADAPTIVE as u32 != 0 {
                let nbytes = (geom.h + 1) / 2;
                if body.len() < nbytes {
                    return Err(E_MED_TRUNC);
                }
                (&body[nbytes..], unpack_filter_bits(&body[..nbytes], geom.h))
            } else {
                (body, vec![hdr.predictor; geom.h])
            };
        if method != METHOD_RAW && method != METHOD_LZ {
            return Err(E_MED_METHOD);
        }
        let per_row = geom.w * hdr.channels * vps;
        let count = per_row * geom.h;
        let residuals: Vec<u32> = if flags & TILE_FLAG_ESCAPE as u32 != 0 {
            // one symbol per sample: zig-zag values plus escaped residuals
            if method == METHOD_LZ {
                let mut dec = RangeDec::new(stream, ENTROPY_SYMBOLS_LZ);
                let mut tokens: Vec<u32> = Vec::new();
                let limit = count * (1 + fmt.escape_chunks) + 64;
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
                let bytes = lz_decode_bounded(&tokens, limit)?;
                escape_residuals(bytes.iter().map(|&b| b as u32), count, &fmt)?
            } else {
                let mut dec = RangeDec::new(stream, ESCAPE_ALPHABET);
                let iter = std::iter::from_fn(|| Some(dec.decode_symbol() as u32));
                escape_residuals(iter, count, &fmt)?
            }
        } else {
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
                match lz_decode(&tokens, count * fmt.value_bytes) {
                    Ok(v) => v,
                    Err(e) => return Err(e),
                }
            } else if flags & TILE_FLAG_CONTEXT as u32 != 0 {
                entropy_decode_planes(
                    stream,
                    ENTROPY_SYMBOLS_RAW,
                    fmt.value_bytes,
                    count,
                )
            } else {
                entropy_decode_bytes(stream, ENTROPY_SYMBOLS_RAW, count * fmt.value_bytes)
            };
            if flags & TILE_FLAG_SPLIT as u32 != 0 {
                merge_planes(&packed, count, fmt.value_bytes)
            } else {
                packed[..count].iter().map(|&b| b as u32).collect()
            }
        };
        Ok((residuals, pred_ids))
    });

    for item in decoded.iter() {
        match item {
            Ok(_) => {}
            Err(e) => return *e,
        }
    }
    if decoded.len() != tasks {
        return E_MED_ALLOC;
    }

    // ---- phase two: reconstruct the rows slice by slice
    let mut payload: Vec<u8> = Vec::new();
    let mut prev_slice: Option<Vec<u32>> = None;
    for z in 0..hdr.depth {
        let mut slice_rows: Vec<u32> =
            vec![0u32; hdr.width * hdr.channels * vps * hdr.height];
        let base_task = z * tiles_per_slice;
        let rows_per_slice: Vec<Vec<u32>> = parallel_map(tiles_per_slice, |t| {
            let geom = &grid[t];
            let per_row = geom.w * hdr.channels * vps;
            let (residuals, pred_ids) = match &decoded[base_task + t] {
                Ok(v) => v,
                Err(_) => return Vec::new(),
            };
            let slice_ctx: Option<Vec<u32>> = prev_slice.as_ref().map(|p| {
                let mut ctx: Vec<u32> = Vec::with_capacity(per_row * geom.h);
                for r in 0..geom.h {
                    let src = (geom.y + r) * (hdr.width * hdr.channels * vps)
                        + geom.x * hdr.channels * vps;
                    ctx.extend_from_slice(&p[src..src + geom.w * hdr.channels * vps]);
                }
                ctx
            });
            reconstruct_tile(
                residuals,
                pred_ids,
                per_row,
                geom.h,
                hdr.channels,
                &fmt,
                slice_ctx.as_deref(),
            )
        });
        for (t, rows) in rows_per_slice.iter().enumerate() {
            let geom = &grid[t];
            let per_row = geom.w * hdr.channels * vps;
            for r in 0..geom.h {
                let row = &rows[r * per_row..(r + 1) * per_row];
                let lo = geom.x * hdr.channels * vps;
                let slice_row = (geom.y + r) * (hdr.width * hdr.channels * vps) + lo;
                slice_rows[slice_row..slice_row + row.len()].copy_from_slice(row);
                let byte_off = (z * hdr.height + geom.y + r) * row_bytes
                    + geom.x * hdr.channels * fmt.sample_bytes;
                let bytes = decode_row(row, &fmt);
                out[byte_off..byte_off + bytes.len()].copy_from_slice(&bytes);
            }
        }
        // keep the payload CRC over the tile bodies in container order
        for t in 0..tiles_per_slice {
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
