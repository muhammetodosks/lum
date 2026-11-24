// LUMEN native core — Rust implementation of the hot-path compression stages.
//
// Stages ported (byte/token-identical to the pure-Python reference in
// compress.py — the container-equivalence test proves it through the
// entropy coder):
//
//   lum_pack_residuals    fixed-width little-endian residual packing
//   lum_unpack_residuals  inverse, with strict range validation
//   lum_rle_encode        byte-level RLE, greedy maximal runs
//   lum_rle_decode        inverse with all overshoot/truncation guards
//   lum_lz_encode         greedy longest-match LZ77, 3-byte hash buckets
//   lum_lz_decode_tokens  token-array LZ77 decode with all guards
//
// Error convention: every fn returns isize (or i64-style handle encoded in
// isize). >= 0 means success (bytes/tokens produced); < 0 is an error code
// that Python maps to CorruptedFileError / ValueError with the reference
// wording.
//
// Build: scripts/build_native.sh  ->  lum/_liblum_core.so

#![allow(clippy::needless_range_loop)]

use std::collections::{HashMap, VecDeque};

mod lum_image;
mod lum_cloud;
mod lum_med;
mod lum_lossy;

pub const TOKEN_MATCH: u32 = 256;
pub const TOKEN_EOF: u32 = 257;
pub const TOKEN_SYMBOLS: u32 = 258;
const MINMATCH: usize = 4;
const MAXMATCH: usize = 255; // len byte holds L - MINMATCH in [0,251]
const MAXDIST: usize = 65535;
const MAXRUN: usize = 255;
const BUCKET_MAX: usize = 32;

// Error codes (negative return values)
const E_RLE_TRUNC: isize = -1;
const E_RLE_ESCAPE_TRUNC: isize = -2;
const E_RLE_OVERSHOOT: isize = -3;
const E_RLE_LEN0: isize = -4;
const E_RLE_MISMATCH: isize = -5;
const E_PACK_RANGE: isize = -100;
const E_UNPACK_TRUNC: isize = -110;
const E_UNPACK_RANGE: isize = -111;
const E_LZ_NO_EOF: isize = -20;        // token buffer exhausted without EOF
const E_LZ_SYM_CAP: isize = -21;       // symbols exceed target_len + 1
const E_LZ_LIT_OVER: isize = -22;      // literal overshoots target
const E_LZ_BAD_SYM: isize = -23;       // token not literal/match/EOF
const E_LZ_MATCH_TRUNC: isize = -24;   // MATCH with fewer than 3 extra tokens
const E_LZ_BAD_EXTRA: isize = -25;     // extra symbol out of [0,255]
const E_LZ_DIST: isize = -26;          // back-reference distance beyond output
const E_LZ_MATCH_OVER: isize = -27;    // match overshoots target
const E_LZ_MISMATCH: isize = -28;      // output length mismatch (no overshoot guard hit)
const E_PRED_CHANNELS: isize = -200;   // channels == 0 passed to a row kernel
const E_PIPE_SHAPE: isize = -300;      // fused pipeline shape mismatch

// Color-space constants duplicated from constants.py (CS_RGB=2, CS_RGBA=3)
// so the native core stays dependency-free.
const CS_RGB_V: i32 = 2;
const CS_RGBA_V: i32 = 3;

// ------------------------------------------------------------- residuals

/// Pack residual symbols (each in [0, mod)) little-endian, `width` bytes each.
#[no_mangle]
pub unsafe extern "C" fn lum_pack_residuals(
    syms: *const u32, count: usize, width: usize, modv: u64, out: *mut u8,
) -> isize {
    let syms = std::slice::from_raw_parts(syms, count);
    let out = std::slice::from_raw_parts_mut(out, count * width);
    for (i, &v) in syms.iter().enumerate() {
        if v as u64 >= modv {
            return E_PACK_RANGE - i as isize; // negative -> Python raises
        }
        for b in 0..width {
            out[i * width + b] = (v >> (8 * b)) as u8;
        }
    }
    (count * width) as isize
}

/// Inverse of pack: decode exactly `count` fixed-width little-endian symbols,
/// validating each against `mod` (decompression-safety requirement).
#[no_mangle]
pub unsafe extern "C" fn lum_unpack_residuals(
    data: *const u8, len: usize, count: usize, width: usize, modv: u64, out: *mut u32,
) -> isize {
    if len < count * width {
        return E_UNPACK_TRUNC;
    }
    let data = std::slice::from_raw_parts(data, len);
    let out = std::slice::from_raw_parts_mut(out, count);
    for i in 0..count {
        let mut v: u64 = 0;
        for b in (0..width).rev() {
            v = (v << 8) | data[i * width + b] as u64;
        }
        if v >= modv {
            return E_UNPACK_RANGE - i as isize;
        }
        out[i] = v as u32;
    }
    count as isize
}

// ------------------------------------------------------------------ RLE

/// Byte-level RLE, greedy maximal runs. Deterministic; byte-identical to the
/// Python reference. Output buffer must be >= 2*len + 16.
#[no_mangle]
pub unsafe extern "C" fn lum_rle_encode(data: *const u8, len: usize, out: *mut u8) -> isize {
    let data = std::slice::from_raw_parts(data, len);
    let out = std::slice::from_raw_parts_mut(out, 2 * len + 16);
    let mut i = 0usize;
    let mut o = 0usize;
    while i < len {
        let b = data[i];
        let mut j = i;
        while j < len && data[j] == b && (j - i) < MAXRUN {
            j += 1;
        }
        let l = j - i;
        if b == 0xFF {
            out[o] = 0xFF;
            out[o + 1] = l as u8;
            o += 2;
            i = j;
        } else if b == 0xFE {
            if l == 1 {
                out[o] = 0xFE;
                out[o + 1] = 0;
                o += 2;
            } else {
                out[o] = 0xFE;
                out[o + 1] = 0xFF; // m = B+1 = 0xFF -> run of 0xFE
                out[o + 2] = l as u8;
                o += 3;
            }
            i = j;
        } else if l >= 3 {
            out[o] = 0xFE;
            out[o + 1] = b + 1; // marker encodes B+1 (zero runs use m=1)
            out[o + 2] = l as u8;
            o += 3;
            i = j;
        } else {
            out[o] = b;
            o += 1;
            i += 1;
        }
    }
    o as isize
}

/// Inverse of rle_encode; reconstructs exactly `target` bytes.
#[no_mangle]
pub unsafe extern "C" fn lum_rle_decode(
    data: *const u8, len: usize, target: usize, out: *mut u8,
) -> isize {
    let data = std::slice::from_raw_parts(data, len);
    let out = std::slice::from_raw_parts_mut(out, target);
    let mut i = 0usize;
    let mut o = 0usize;
    while o < target {
        if i >= len {
            return E_RLE_TRUNC;
        }
        let b = data[i];
        i += 1;
        if b != 0xFF && b != 0xFE {
            out[o] = b;
            o += 1;
        } else if b == 0xFF {
            if i >= len {
                return E_RLE_ESCAPE_TRUNC;
            }
            let c = data[i] as usize;
            i += 1;
            if c == 0 {
                out[o] = 0xFF;
                o += 1;
            } else {
                if o + c > target {
                    return E_RLE_OVERSHOOT;
                }
                out[o..o + c].fill(0xFF);
                o += c;
            }
        } else {
            if i >= len {
                return E_RLE_ESCAPE_TRUNC;
            }
            let m = data[i] as usize;
            i += 1;
            if m == 0 {
                out[o] = 0xFE;
                o += 1;
            } else {
                if i >= len {
                    return E_RLE_ESCAPE_TRUNC;
                }
                let l = data[i] as usize;
                i += 1;
                if l == 0 {
                    return E_RLE_LEN0;
                }
                let b_val = (m - 1) as u8;
                if o + l > target {
                    return E_RLE_OVERSHOOT;
                }
                out[o..o + l].fill(b_val);
                o += l;
            }
        }
    }
    if o != target {
        return E_RLE_MISMATCH;
    }
    o as isize
}

// ----------------------------------------------------------------- LZ77

/// Integer-only hasher for the LZ match table.
///
/// The default `HashMap` hasher is SipHash-1-3, which costs ~20 ns per
/// lookup; the table is probed once per input byte, so the hash was the
/// single most expensive part of the LZ encoder. This is the classic
/// multiply-shift hash: one multiply for the key, one for the mix.
#[derive(Default, Clone, Copy)]
struct FastHasher {
    hash: u64,
}

impl std::hash::Hasher for FastHasher {
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.hash = (self.hash.rotate_left(5) ^ b as u64).wrapping_mul(0x517cc1b727220a95);
        }
    }

    #[inline]
    fn write_u32(&mut self, v: u32) {
        self.hash = (v as u64).wrapping_mul(0x9E3779B97F4A7C15);
    }

    #[inline]
    fn finish(&self) -> u64 {
        self.hash
    }
}

#[derive(Default, Clone, Copy)]
struct FastBuild;

impl std::hash::BuildHasher for FastBuild {
    type Hasher = FastHasher;
    #[inline]
    fn build_hasher(&self) -> FastHasher {
        FastHasher::default()
    }
}

/// Common-prefix length of `data[cand..]` and `data[at..]`, capped at
/// `max_len`.
#[inline]
fn lz_match_len(data: &[u8], cand: usize, at: usize, max_len: usize) -> usize {
    // Word-at-a-time comparison: identical result to the byte loop, but
    // eight bytes per iteration. The first differing byte is located with
    // trailing_zeros on the XOR of two little-endian words.
    let mut l = 0usize;
    while l + 8 <= max_len {
        let a = unsafe { (data.as_ptr().add(cand + l) as *const u64).read_unaligned() };
        let b = unsafe { (data.as_ptr().add(at + l) as *const u64).read_unaligned() };
        if a != b {
            let x = (a ^ b).to_le();
            l += (x.trailing_zeros() / 8) as usize;
            return l;
        }
        l += 8;
    }
    while l < max_len && data[cand + l] == data[at + l] {
        l += 1;
    }
    l
}

/// Expand `l` bytes at offset `o` from the back-reference at distance
/// `dist` (1-based) inside the raw output buffer.
///
/// The reference implementation copies byte by byte, which also reproduces
/// self-overlapping patterns. The same bytes are produced here by copying
/// the `dist`-byte pattern and replicating it, which is exactly what the
/// byte loop computes as well.
#[inline]
unsafe fn lz_expand_raw(out: *mut u8, o: usize, dist: usize, l: usize) {
    let src = o - dist;
    if dist >= l {
        std::ptr::copy_nonoverlapping(out.add(src), out.add(o), l);
    } else {
        std::ptr::copy_nonoverlapping(out.add(src), out.add(o), dist);
        let mut w = o + dist;
        let mut rem = l - dist;
        while rem > 0 {
            let n = if rem < dist { rem } else { dist };
            std::ptr::copy_nonoverlapping(out.add(src), out.add(w), n);
            w += n;
            rem -= n;
        }
    }
}

/// `lz_expand_raw` for a growable output vector.
#[inline]
unsafe fn lz_expand_vec(out: &mut Vec<u8>, dist: usize, l: usize) {
    let base = out.len();
    out.reserve(l);
    lz_expand_raw(out.as_mut_ptr(), base, dist, l);
    out.set_len(base + l);
}

/// One LZ77 token produced by [`LzMatcher`].
enum LzTok {
    /// A literal byte.
    Lit(u8),
    /// A back-reference of `len` bytes at 1-based `dist`.
    Match { len: usize, dist: usize },
    /// End of the stream.
    Eof,
}

/// Greedy LZ77 matcher with 3-byte hash buckets.
///
/// The token sequence is identical to the Python reference: the newest
/// candidate wins ties, buckets are capped at `BUCKET_MAX` positions with
/// FIFO eviction, and a match must be at least `MINMATCH` bytes long.
/// Producing the tokens lazily lets a caller stop as soon as the encoded
/// stream is already larger than the best candidate found so far.
struct LzMatcher<'a> {
    data: &'a [u8],
    table: HashMap<u32, VecDeque<u32>, FastBuild>,
    pos: usize,
}

impl<'a> LzMatcher<'a> {
    fn new(data: &'a [u8]) -> Self {
        LzMatcher {
            data,
            table: HashMap::with_capacity_and_hasher(data.len() / 8 + 1, FastBuild),
            pos: 0,
        }
    }

    /// Remember the 3-byte key at `pos` (if a full key fits).
    fn add_key(&mut self, pos: usize) {
        let len = self.data.len();
        if pos + 3 <= len {
            let key = (self.data[pos] as u32)
                | ((self.data[pos + 1] as u32) << 8)
                | ((self.data[pos + 2] as u32) << 16);
            let bucket = self.table.entry(key).or_insert_with(VecDeque::new);
            bucket.push_back(pos as u32);
            if bucket.len() > BUCKET_MAX {
                bucket.pop_front();
            }
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
        if i + MINMATCH <= len {
            let key = (self.data[i] as u32)
                | ((self.data[i + 1] as u32) << 8)
                | ((self.data[i + 2] as u32) << 16);
            if let Some(positions) = self.table.get(&key) {
                let max_len = usize::min(len - i, MAXMATCH);
                for &cand in positions.iter().rev() {
                    let dist = i - cand as usize;
                    if dist == 0 || dist > MAXDIST {
                        break;
                    }
                    // verify; self-overlap handled identically by the decoder
                    let l = lz_match_len(self.data, cand as usize, i, max_len);
                    if l > best_len {
                        best_len = l;
                        best_dist = dist;
                        if l >= max_len {
                            break;
                        }
                    }
                }
            }
        }
        if best_len >= MINMATCH {
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

#[no_mangle]
pub unsafe extern "C" fn lum_lz_encode(
    data: *const u8, len: usize, tokens_out: *mut u32,
) -> isize {
    let data = std::slice::from_raw_parts(data, len);
    let out = std::slice::from_raw_parts_mut(tokens_out, len + 2);
    let mut t = 0usize;

    if len == 0 {
        out[0] = TOKEN_EOF;
        return 1;
    }

    let mut matcher = LzMatcher::new(data);
    loop {
        match matcher.next_token() {
            LzTok::Eof => {
                out[t] = TOKEN_EOF;
                t += 1;
                break;
            }
            LzTok::Lit(b) => {
                out[t] = b as u32;
                t += 1;
            }
            LzTok::Match { len, dist } => {
                out[t] = TOKEN_MATCH;
                out[t + 1] = (len - MINMATCH) as u32;
                let d0 = dist - 1; // zero-based distance
                out[t + 2] = (d0 & 0xFF) as u32;
                out[t + 3] = (d0 >> 8) as u32;
                t += 4;
            }
        }
    }
    t as isize
}

// ------------------------------------------------------------ predictors

/// Python-exact modular arithmetic: ((x % m) + m) % m on i64 so negative
/// intermediates map into [0, m) like Python's `%`.
///
/// Rust's `%` truncates toward zero exactly like Python, so the two-step
/// Python form collapses into one division plus a conditional fixup.
#[inline]
fn py_mod(x: i64, m: i64) -> i32 {
    let r = x % m;
    let r = if r < 0 { r + m } else { r };
    r as i32
}

#[inline]
fn paeth(a: i64, b: i64, c: i64) -> i64 {
    let p = a + b - c;
    let pa = (p - a).abs();
    let pb = (p - b).abs();
    let pc = (p - c).abs();
    if pa <= pb && pa <= pc { a } else if pb <= pc { b } else { c }
}

#[inline]
fn median(a: i64, b: i64, c: i64) -> i64 {
    if c >= a.max(b) { a.min(b) } else if c <= a.min(b) { a.max(b) } else { a + b - c }
}

#[inline]
fn pred_value(pt: i32, left: i64, up: i64, ul: i64) -> i64 {
    match pt {
        0 => 0,
        1 => left,
        2 => up,
        // Python (a + b) >> 1 floors; Rust i64 >> is arithmetic (same)
        3 => (left + up) >> 1,
        4 => paeth(left, up, ul),
        5 => left + up - ul,
        6 => median(left, up, ul),
        _ => 0,
    }
}

/// Shared predicates for the row kernels (kept inline via macro expansion).
macro_rules! row_kernel {
    ($name:ident, $recon:expr) => {
        #[no_mangle]
        pub unsafe extern "C" fn $name(
            row_in: *const i32, len: usize,
            prev_row: *const i32, prev_len: usize,
            pred_type: i32, channels: usize, modv: i64,
            out: *mut i32,
        ) -> isize {
            if channels == 0 {
                return E_PRED_CHANNELS;
            }
            let row = std::slice::from_raw_parts(row_in, len);
            let has_prev = !prev_row.is_null() && prev_len >= len;
            let prev: &[i32] = if has_prev {
                std::slice::from_raw_parts(prev_row, len)
            } else {
                &[]
            };
            let out = std::slice::from_raw_parts_mut(out, len);
            for i in 0..len {
                let x = i / channels;
                // apply: left context comes from the input row (original
                // values, like the Python reference); reconstruct: left
                // comes from the row being built.
                let left = if x > 0 {
                    if $recon { out[i - channels] as i64 } else { row[i - channels] as i64 }
                } else {
                    0
                };
                let up = if has_prev { prev[i] as i64 } else { 0 };
                let ul = if has_prev && x > 0 { prev[i - channels] as i64 } else { 0 };
                let pred = pred_value(pred_type, left, up, ul);
                out[i] = if $recon {
                    py_mod(row[i] as i64 + pred, modv)
                } else {
                    py_mod(row[i] as i64 - pred, modv)
                };
            }
            len as isize
        }
    };
}

row_kernel!(lum_apply_predictor_row, false);
row_kernel!(lum_reconstruct_row, true);

// ----------------------------------------------------------- YCoCg

/// Reversible RGB->YCoCg over an interleaved pixel list (passthrough for
/// non-RGB/RGBa color spaces, alpha untouched). Byte-identical to Python.
#[no_mangle]
pub unsafe extern "C" fn lum_transform_forward(
    vals: *const i32, n: usize, color_space: i32, out: *mut i32,
) -> isize {
    if color_space != CS_RGB_V && color_space != CS_RGBA_V {
        if n > 0 {
            std::ptr::copy_nonoverlapping(vals, out, n);
        }
        return n as isize; // passthrough (Python returns the same list)
    }
    let channels: usize = if color_space == CS_RGBA_V { 4 } else { 3 };
    let vals = std::slice::from_raw_parts(vals, n);
    let out = std::slice::from_raw_parts_mut(out, n);
    let mut i = 0usize;
    while i + channels <= n {
        let r = vals[i + 0] as i64;
        let g = vals[i + 1] as i64;
        let b = vals[i + 2] as i64;
        let co = r - b;
        let t = b + (co >> 1);
        let cg = g - t;
        let y = t + (cg >> 1);
        out[i + 0] = y as i32;
        out[i + 1] = co as i32;
        out[i + 2] = cg as i32;
        if channels == 4 {
            out[i + 3] = vals[i + 3];
        }
        i += channels;
    }
    // trailing partial pixel: Python would crash on index; keep parity by
    // copying the remainder unchanged (len < channels buffers are never
    // produced by the codec pipeline)
    while i < n {
        out[i] = vals[i];
        i += 1;
    }
    n as isize
}

/// Inverse of lum_transform_forward.
#[no_mangle]
pub unsafe extern "C" fn lum_transform_inverse(
    vals: *const i32, n: usize, color_space: i32, out: *mut i32,
) -> isize {
    if color_space != CS_RGB_V && color_space != CS_RGBA_V {
        if n > 0 {
            std::ptr::copy_nonoverlapping(vals, out, n);
        }
        return n as isize;
    }
    let channels: usize = if color_space == CS_RGBA_V { 4 } else { 3 };
    let vals = std::slice::from_raw_parts(vals, n);
    let out = std::slice::from_raw_parts_mut(out, n);
    let mut i = 0usize;
    while i + channels <= n {
        let y = vals[i + 0] as i64;
        let co = vals[i + 1] as i64;
        let cg = vals[i + 2] as i64;
        let t = y - (cg >> 1);
        let g = cg + t;
        let b = t - (co >> 1);
        let r = co + b;
        out[i + 0] = r as i32;
        out[i + 1] = g as i32;
        out[i + 2] = b as i32;
        if channels == 4 {
            out[i + 3] = vals[i + 3];
        }
        i += channels;
    }
    while i < n {
        out[i] = vals[i];
        i += 1;
    }
    n as isize
}

/// Token-array LZ77 decode: expands literals and back-references until EOF
/// or exactly `target` output bytes are produced. Token-identical to the
/// Python reference (compress.py equivalent pipeline).
#[no_mangle]
pub unsafe extern "C" fn lum_lz_decode_tokens(
    tokens: *const u32, ntok: usize, target: usize, out: *mut u8,
) -> isize {
    let tokens = std::slice::from_raw_parts(tokens, ntok);
    let out = std::slice::from_raw_parts_mut(out, target);
    let mut o = 0usize;
    let mut p = 0usize;
    let mut symbols = 0usize;
    let cap = target + 1;
    loop {
        if p >= ntok {
            return E_LZ_NO_EOF;
        }
        let sym = tokens[p];
        p += 1;
        symbols += 1;
        if symbols > cap {
            return E_LZ_SYM_CAP;
        }
        if sym == TOKEN_EOF {
            break;
        }
        if sym < TOKEN_MATCH {
            if o >= target {
                return E_LZ_LIT_OVER;
            }
            out[o] = sym as u8;
            o += 1;
            continue;
        }
        if sym != TOKEN_MATCH {
            return E_LZ_BAD_SYM;
        }
        // MATCH: 3 extra tokens
        if p + 3 > ntok {
            return E_LZ_MATCH_TRUNC;
        }
        let e0 = tokens[p];
        let e1 = tokens[p + 1];
        let e2 = tokens[p + 2];
        p += 3;
        symbols += 3;
        if e0 > 255 || e1 > 255 || e2 > 255 {
            return E_LZ_BAD_EXTRA;
        }
        let l = (e0 as usize) + MINMATCH;
        let d0 = (e1 as usize) | ((e2 as usize) << 8);
        let dist = 1 + d0;
        if dist > o {
            return E_LZ_DIST;
        }
        if l + o > target {
            return E_LZ_MATCH_OVER;
        }
        lz_expand_raw(out.as_mut_ptr(), o, dist, l);
        o += l;
    }
    if o != target {
        return E_LZ_MISMATCH;
    }
    o as isize
}

// ------------------------------------------------ pipeline fusion

// Fused codec front/back halves: one buffer conversion per call instead of
// one per stage (transform / per-row prediction / packing). Semantics are
// the exact composition of LumenCodec._residual_stream / decode_tile on the
// pure-Python side, proven by container-parity tests.

/// Forward half: YCoCg transform -> canonicalize (+offset) -> per-row
/// predictor residuals. Writes residuals into `out`; prediction contexts
/// come from an internal copy of the canonicalized input, mirroring the
/// Python reference (prev_row = canonicalized transformed slice above).
#[no_mangle]
pub unsafe extern "C" fn lum_pipe_forward(
    vals: *const i32, n: usize, stride: usize,
    color_space: i32, offset: i64, modv: i64,
    pred_type: i32, channels: usize, out: *mut i32, transform: i32,
) -> isize {
    if channels == 0 || modv <= 0 || n % stride != 0 {
        return E_PIPE_SHAPE;
    }
    let vals = std::slice::from_raw_parts(vals, n);
    let out = std::slice::from_raw_parts_mut(out, n);
    let mut tmp: Vec<i32> = vec![0; n];

    // 1. reversible color transform (passthrough for other spaces and for
    //    TRANSFORM_NONE, which predicts the channels as they are)
    let is_cs = (color_space == CS_RGB_V || color_space == CS_RGBA_V)
        && transform == 0;
    let ch: usize = if color_space == CS_RGBA_V { 4 } else { 3 };
    if is_cs {
        let mut i = 0usize;
        while i + ch <= n {
            let r = vals[i] as i64;
            let g = vals[i + 1] as i64;
            let b = vals[i + 2] as i64;
            let co = r - b;
            let t = b + (co >> 1);
            let cg = g - t;
            let y = t + (cg >> 1);
            tmp[i] = y as i32;
            tmp[i + 1] = co as i32;
            tmp[i + 2] = cg as i32;
            if ch == 4 {
                tmp[i + 3] = vals[i + 3];
            }
            i += ch;
        }
        while i < n {
            tmp[i] = vals[i];
            i += 1;
        }
    } else {
        tmp.copy_from_slice(vals);
    }

    // 2. canonicalize into the predictor domain (plain integer shift, no mod)
    if offset != 0 {
        for v in tmp.iter_mut() {
            *v = (*v as i64 + offset) as i32;
        }
    }

    // 3. predictor pass, row by row (left = canonicalized input row,
    //    up/upper-left = previous canonicalized row, none for row 0)
    let px = stride / channels;
    let mut r = 0usize;
    while r < n {
        let row_end = r + stride;
        let mut x = 0usize;
        while x < px {
            let base = r + x * channels;
            let mut c = 0usize;
            while c < channels {
                let i = base + c;
                let left = if x > 0 { tmp[i - channels] as i64 } else { 0 };
                let up = if r > 0 { tmp[i - stride] as i64 } else { 0 };
                let ul = if r > 0 && x > 0 { tmp[i - stride - channels] as i64 } else { 0 };
                let pred = pred_value(pred_type, left, up, ul);
                out[i] = py_mod(tmp[i] as i64 - pred, modv);
                c += 1;
            }
            x += 1;
        }
        r = row_end;
    }
    n as isize
}

/// Backward half: reconstruct rows from residuals (left from the row being
/// built, up/upper-left from already-reconstructed rows) -> descale
/// (-offset) -> inverse YCoCg. Bit-identical to the pure-python tail of
/// decode_tile.
#[no_mangle]
pub unsafe extern "C" fn lum_pipe_reconstruct(
    res: *const i32, n: usize, stride: usize,
    color_space: i32, offset: i64, modv: i64,
    pred_type: i32, channels: usize, out: *mut i32, transform: i32,
) -> isize {
    if channels == 0 || modv <= 0 || n % stride != 0 {
        return E_PIPE_SHAPE;
    }
    let res = std::slice::from_raw_parts(res, n);
    let out = std::slice::from_raw_parts_mut(out, n);

    // 1. predictor reconstruction, row by row
    let px = stride / channels;
    let mut r = 0usize;
    while r < n {
        let row_end = r + stride;
        let mut x = 0usize;
        while x < px {
            let base = r + x * channels;
            let mut c = 0usize;
            while c < channels {
                let i = base + c;
                let left = if x > 0 { out[i - channels] as i64 } else { 0 };
                let up = if r > 0 { out[i - stride] as i64 } else { 0 };
                let ul = if r > 0 && x > 0 { out[i - stride - channels] as i64 } else { 0 };
                let pred = pred_value(pred_type, left, up, ul);
                out[i] = py_mod(res[i] as i64 + pred, modv);
                c += 1;
            }
            x += 1;
        }
        r = row_end;
    }

    // 2. undo canonicalization (plain integer shift, no mod)
    if offset != 0 {
        for v in out.iter_mut() {
            *v = (*v as i64 - offset) as i32;
        }
    }

    // 3. inverse color transform in place (passthrough for other spaces)
    let is_cs = (color_space == CS_RGB_V || color_space == CS_RGBA_V)
        && transform == 0;
    let ch: usize = if color_space == CS_RGBA_V { 4 } else { 3 };
    if is_cs {
        let mut i = 0usize;
        while i + ch <= n {
            let y = out[i] as i64;
            let co = out[i + 1] as i64;
            let cg = out[i + 2] as i64;
            let t = y - (cg >> 1);
            let g = cg + t;
            let b = t - (co >> 1);
            let rr = co + b;
            out[i] = rr as i32;
            out[i + 1] = g as i32;
            out[i + 2] = b as i32;
            i += ch;
        }
    }
    n as isize
}

// ---------------------------------------------------------- range coder
// Adaptive arithmetic (range) coder, ported line-for-line from
// entropy.py: Witten-Neal-Cleary (E1/E2/E3) renormalization over a bit
// stream with the identical adaptive Fenwick frequency model — so native
// and pure-Python sides produce bit-identical container bytes.

const E_MODEL_SYMS: isize = -40;     // n_symbols < 1
const E_ENC_BUF: isize = -41;        // encoder output buffer too small
const E_ENC_SYM: isize = -42;        // encode symbol >= alphabet (defensive)
const E_ENC_ABORT: isize = -43;      // candidate grew past its byte budget
const E_DEC_HDR: isize = -45;        // LZ header symbol is not a byte

const HALF: i64 = 1 << 31;
const QUARTER: i64 = 1 << 30;
const THREE_QUARTERS: i64 = 3 << 30;

/// Fenwick tree over symbol frequencies, padded with zero-frequency
/// symbols up to the next power of two. State-identical to
/// entropy.FenwickCDF (including the freq>=1 halving policy).
struct Fenwick {
    n: usize,
    size: usize,
    freq: Vec<i64>,
    total: i64,
    cap: i64,
    tree: Vec<i64>,
    /// Cache of the most recently updated symbol: (cum, freq).
    /// Updating a symbol does not change the cumulative sum *below* it, so
    /// the cached values stay exact as long as no other symbol is touched.
    cache_sym: usize,
    cache_cum: i64,
    cache_freq: i64,
}

const NO_CACHE: usize = usize::MAX;

impl Fenwick {
    fn new(n: usize) -> Self {
        let mut size = 1usize;
        while size < n {
            size <<= 1;
        }
        let mut f = Fenwick {
            n,
            size,
            freq: vec![1i64; n],
            total: n as i64,
            // cap scales with alphabet size; halving keeps freq >= 1
            cap: std::cmp::max(1i64 << 15, 2 * n as i64),
            tree: vec![0i64; size + 1],
            cache_sym: NO_CACHE,
            cache_cum: 0,
            cache_freq: 0,
        };
        f.rebuild();
        f
    }

    /// Standard O(n) in-place Fenwick build.
    fn rebuild(&mut self) {
        let s = self.size;
        {
            let t = &mut self.tree;
            for i in 1..=s {
                t[i] = if i <= self.n { self.freq[i - 1] } else { 0 };
            }
        }
        for i in 1..=s {
            let parent = i + (i & i.wrapping_neg());
            if parent <= s {
                self.tree[parent] += self.tree[i];
            }
        }
    }

    /// halve all frequencies, keep >= 1, rebuild tree.
    fn halve(&mut self) {
        for f in self.freq.iter_mut() {
            *f = (*f + 1) >> 1;
        }
        self.total = self.freq.iter().sum();
        self.rebuild();
        // every frequency changed: the cache is no longer exact
        self.cache_sym = NO_CACHE;
    }

    fn add(&mut self, symbol: usize) {
        self.freq[symbol] += 1;
        self.total += 1;
        let mut i = symbol + 1;
        let s = self.size;
        while i <= s {
            self.tree[i] += 1;
            i += i & i.wrapping_neg();
        }
        if self.total > self.cap {
            self.halve();
        } else if self.cache_sym == symbol {
            // cum(symbol) is unaffected by its own update, freq grew by one
            self.cache_freq += 1;
        } else {
            self.cache_sym = NO_CACHE;
        }
    }

    /// Cumulative frequency strictly below `symbol` (0-based).
    /// Fills the single-symbol cache so a repeated symbol is cheap.
    fn cum(&mut self, symbol: usize) -> i64 {
        if self.cache_sym == symbol {
            return self.cache_cum;
        }
        let mut s = 0i64;
        let mut i = symbol;
        while i > 0 {
            s += self.tree[i];
            i -= i & i.wrapping_neg();
        }
        self.cache_sym = symbol;
        self.cache_cum = s;
        self.cache_freq = self.freq[symbol];
        s
    }

    /// Frequency of `symbol` (honours the cache).
    fn freq_of(&self, symbol: usize) -> i64 {
        if self.cache_sym == symbol {
            return self.cache_freq;
        }
        self.freq[symbol]
    }

    /// Find symbol s with cum(s) <= target < cum(s+1).
    /// Returns (symbol, cum_low, freq); binary descent, identical to
    /// FenwickCDF.find including the defensive last-symbol clamp.
    fn find(&mut self, target: i64) -> (usize, i64, i64) {
        // Fast path: the target falls inside the symbol that was updated
        // last (skewed distributions repeat the same symbol a lot).
        if self.cache_sym != NO_CACHE {
            let lo = self.cache_cum;
            let hi = lo + self.cache_freq;
            if target >= lo && target < hi {
                return (self.cache_sym, lo, self.cache_freq);
            }
        }
        let mut pos = 0usize;
        let mut remaining = target;
        let mut bitmask = self.size;
        while bitmask > 0 {
            let nxt = pos + bitmask;
            if nxt <= self.size && self.tree[nxt] <= remaining {
                pos = nxt;
                remaining -= self.tree[nxt];
            }
            bitmask >>= 1;
        }
        if pos >= self.n {
            let p = self.n - 1;
            // Python: pos = n - 1; cumlow = self.cum(pos)
            let cum = self.cum(p);
            let freq = self.freq_of(p);
            self.cache_sym = p;
            self.cache_cum = cum;
            self.cache_freq = freq;
            (p, cum, freq)
        } else {
            let cum = target - remaining;
            let freq = self.freq_of(pos);
            self.cache_sym = pos;
            self.cache_cum = cum;
            self.cache_freq = freq;
            (pos, cum, freq)
        }
    }
}

#[inline]
fn next_bit(data: &[u8], pos: &mut usize) -> i64 {
    let idx = *pos;
    *pos += 1;
    let byte_i = idx >> 3;
    if byte_i >= data.len() {
        return 0; // zero padding beyond the stream, like the reference
    }
    ((data[byte_i] >> (7 - (idx & 7))) & 1) as i64
}

/// Integer division with a 32-bit fast path.
///
/// The coder divides a span of at most 2^32 by a total that never exceeds
/// the alphabet cap, so the division fits into 32 bits almost always - and a
/// 32-bit division is markedly cheaper than a 64-bit one. The result is
/// identical either way (both truncate towards zero, both operands are
/// positive).
#[inline]
fn div_span(span: i64, total: i64) -> i64 {
    if span <= u32::MAX as i64 && total <= u32::MAX as i64 {
        (span as u32 / total as u32) as i64
    } else {
        span / total
    }
}

/// Streaming range encoder.
///
/// Symbols are pushed one at a time and the number of emitted bits can be
/// inspected, which lets a caller abandon a candidate that is already larger
/// than the best one found so far: the final size only ever grows, so an
/// abandoned candidate can never win.
struct RangeEnc {
    model: Fenwick,
    low: i64,
    high: i64,
    out: Vec<u8>,
    acc: u32,
    nbits: u32,
    pending: i64,
}

impl RangeEnc {
    fn new(n_symbols: usize, cap_hint: usize) -> Self {
        RangeEnc {
            model: Fenwick::new(n_symbols),
            low: 0,
            high: (1i64 << 32) - 1, // inclusive
            out: Vec::with_capacity(cap_hint / 8 + 16),
            acc: 0,
            nbits: 0,
            pending: 0,
        }
    }

    /// Append one bit (MSB first).
    #[inline]
    fn put_bit(&mut self, bit: u8) {
        self.acc = (self.acc << 1) | (bit as u32 & 1);
        self.nbits += 1;
        if self.nbits == 8 {
            self.out.push(self.acc as u8);
            self.acc = 0;
            self.nbits = 0;
        }
    }

    /// Append `n` copies of the same bit.
    #[inline]
    fn put_bits(&mut self, bit: u8, n: i64) {
        for _ in 0..n {
            self.put_bit(bit);
        }
    }

    /// Renormalisation with the E1/E2/E3 cases picked by masks.
    #[inline]
    fn renorm(&mut self) {
        loop {
            let e1 = (self.high < HALF) as i64;
            let e2 = ((self.low >= HALF) & (e1 == 0)) as i64;
            let e3 = ((self.low >= QUARTER) & (self.high < THREE_QUARTERS)
                & (e1 == 0) & (e2 == 0)) as i64;
            if (e1 | e2 | e3) == 0 {
                break;
            }
            let n = (e1 | e2) * (self.pending + 1);
            if n > 0 {
                // the leading bit is the E2 bit, the rest are E1 bits
                self.put_bit(e2 as u8);
                self.put_bits(e1 as u8, n - 1);
            }
            self.pending = e3 * (self.pending + 1);
            let off = e2 * HALF + e3 * QUARTER;
            self.low = (self.low - off) << 1;
            self.high = ((self.high - off) << 1) | 1;
        }
    }

    #[inline]
    fn push(&mut self, symbol: usize) {
        let cum = self.model.cum(symbol);
        let f = self.model.freq_of(symbol);
        let total = self.model.total;
        let span = self.high - self.low + 1;
        let mut r = div_span(span, total);
        if r < 1 {
            r = 1;
        }
        let cum_high = cum + f;
        let new_low = self.low + r * cum;
        let mut new_high = self.low + r * cum_high - 1;
        if new_high < new_low {
            new_high = new_low;
        }
        self.low = new_low;
        self.high = new_high;
        self.renorm();
        self.model.add(symbol);
    }

    /// Bits emitted so far; the flushed size is at least this rounded up to
    /// whole bytes.
    #[inline]
    fn bit_len(&self) -> usize {
        self.out.len() * 8 + self.nbits as usize
    }

    /// WNC flush plus the zero padded last byte.
    fn finish(mut self) -> Vec<u8> {
        self.pending += 1;
        if self.low < QUARTER {
            self.put_bit(0);
            self.put_bits(1, self.pending);
        } else {
            self.put_bit(1);
            self.put_bits(0, self.pending);
        }
        if self.nbits > 0 {
            self.out.push((self.acc << (8 - self.nbits)) as u8);
            self.acc = 0;
            self.nbits = 0;
        }
        self.out
    }
}

/// Encode a symbol list with a bit budget.
///
/// `max_bits` is the number of bits the candidate may use before it is
/// abandoned (`usize::MAX` means "no budget"). Abandoning is safe: the
/// flushed size of a stream is never smaller than the bits emitted so far,
/// so the candidate is provably larger than the budget owner.
fn encode_symbols_capped(
    syms: &[u32], n_symbols: usize, max_bits: usize,
) -> Result<Vec<u8>, isize> {
    if n_symbols == 0 {
        return Err(E_MODEL_SYMS);
    }
    let mut enc = RangeEnc::new(n_symbols, 4 * syms.len() + 64);
    for &s in syms.iter() {
        let symbol = s as usize;
        if symbol >= n_symbols {
            return Err(E_ENC_SYM);
        }
        enc.push(symbol);
        if enc.bit_len() > max_bits {
            return Err(E_ENC_ABORT);
        }
    }
    Ok(enc.finish())
}

/// Encode the LZ method (four-byte length header plus the token stream) with
/// a bit budget.
///
/// The tokens are produced and encoded in one pass, so an oversized
/// candidate is abandoned before the whole stream is tokenised.
fn encode_lz_method_capped(rle_raw: &[u8], max_bits: usize) -> Result<Vec<u8>, isize> {
    let mut enc = RangeEnc::new(TOKEN_SYMBOLS as usize, 4 * (rle_raw.len() + 2) + 64);
    let rle_len = rle_raw.len() as u32;
    for shift in [24u32, 16, 8, 0] {
        enc.push(((rle_len >> shift) & 0xFF) as usize);
    }
    let mut matcher = LzMatcher::new(rle_raw);
    loop {
        match matcher.next_token() {
            LzTok::Eof => {
                enc.push(TOKEN_EOF as usize);
                break;
            }
            LzTok::Lit(b) => {
                enc.push(b as usize);
            }
            LzTok::Match { len, dist } => {
                enc.push(TOKEN_MATCH as usize);
                enc.push(len - MINMATCH);
                let d0 = dist - 1; // zero-based distance
                enc.push((d0 & 0xFF) as usize);
                enc.push((d0 >> 8) as usize);
            }
        }
        if enc.bit_len() > max_bits {
            return Err(E_ENC_ABORT);
        }
    }
    Ok(enc.finish())
}

/// Encode `count` symbols as a packed bit string, byte-identical to
/// RangeEncoder(...).flush(). Output buffer >= 2*count + 256 bytes.
#[no_mangle]
pub unsafe extern "C" fn lum_range_encode(
    syms: *const u32, count: usize, n_symbols: usize,
    out: *mut u8, out_cap: usize,
) -> isize {
    // a null pointer with count == 0 is not a valid slice (UB)
    let syms: &[u32] = if count == 0 {
        &[]
    } else {
        std::slice::from_raw_parts(syms, count)
    };
    encode_symbols(syms, n_symbols, out, out_cap)
}

/// Stateful range decoder over a byte stream (zero padding past the end,
/// like the Python reference).
struct RangeDec<'a> {
    model: Fenwick,
    data: &'a [u8],
    pos: usize,
    low: i64,
    high: i64,
    value: i64,
}

impl<'a> RangeDec<'a> {
    fn new(data: &'a [u8], n_symbols: usize) -> Self {
        let mut d = RangeDec {
            model: Fenwick::new(n_symbols),
            data,
            pos: 0,
            low: 0,
            high: (1i64 << 32) - 1,
            value: 0,
        };
        for _ in 0..32 {
            d.value = (d.value << 1) | next_bit(d.data, &mut d.pos);
        }
        d
    }

    /// Decode one symbol; state-identical to RangeDecoder.decode_symbol.
    fn decode_symbol(&mut self) -> usize {
        let total = self.model.total;
        let span = self.high - self.low + 1;
        let mut r = div_span(span, total);
        if r < 1 {
            r = 1;
        }
        // Python floor division; the negative-target clamp makes the Rust
        // truncation difference immaterial
        let mut target = div_span(self.value - self.low, r);
        if target < 0 {
            target = 0;
        }
        if target >= total {
            target = total - 1;
        }
        let (sym, cumlow, f) = self.model.find(target);
        self.low += r * cumlow;
        self.high = self.low + r * f - 1;
        // renormalization with value refill
        loop {
            let e1 = (self.high < HALF) as i64;
            let e2 = ((self.low >= HALF) & (e1 == 0)) as i64;
            let e3 = ((self.low >= QUARTER) & (self.high < THREE_QUARTERS)
                & (e1 == 0) & (e2 == 0)) as i64;
            if (e1 | e2 | e3) == 0 {
                break;
            }
            let off = e2 * HALF + e3 * QUARTER;
            self.low = (self.low - off) << 1;
            self.high = ((self.high - off) << 1) | 1;
            self.value = ((self.value - off) << 1) | next_bit(self.data, &mut self.pos);
        }
        self.model.add(sym);
        sym
    }
}

/// Range encoder with one adaptive model per context.
///
/// The bit stream is shared by all models; only the frequency model depends
/// on the context. The medical container uses one model per byte plane: the
/// high planes of a wide sample are long runs of zero bytes, and a separate
/// model learns that immediately, while one shared model would have to
/// spend its adaptation on the noisy low plane.
struct CtxRangeEnc {
    models: Vec<Fenwick>,
    low: i64,
    high: i64,
    out: Vec<u8>,
    acc: u32,
    nbits: u32,
    pending: i64,
}

impl CtxRangeEnc {
    fn new(n_symbols: usize, n_contexts: usize, cap_hint: usize) -> Self {
        CtxRangeEnc {
            models: (0..n_contexts).map(|_| Fenwick::new(n_symbols)).collect(),
            low: 0,
            high: (1i64 << 32) - 1, // inclusive
            out: Vec::with_capacity(cap_hint / 8 + 16),
            acc: 0,
            nbits: 0,
            pending: 0,
        }
    }

    /// Append one bit (MSB first).
    #[inline]
    fn put_bit(&mut self, bit: u8) {
        self.acc = (self.acc << 1) | (bit as u32 & 1);
        self.nbits += 1;
        if self.nbits == 8 {
            self.out.push(self.acc as u8);
            self.acc = 0;
            self.nbits = 0;
        }
    }

    /// Append `n` copies of the same bit.
    #[inline]
    fn put_bits(&mut self, bit: u8, n: i64) {
        for _ in 0..n {
            self.put_bit(bit);
        }
    }

    /// Renormalisation with the E1/E2/E3 cases picked by masks.
    #[inline]
    fn renorm(&mut self) {
        loop {
            let e1 = (self.high < HALF) as i64;
            let e2 = ((self.low >= HALF) & (e1 == 0)) as i64;
            let e3 = ((self.low >= QUARTER) & (self.high < THREE_QUARTERS)
                & (e1 == 0) & (e2 == 0)) as i64;
            if (e1 | e2 | e3) == 0 {
                break;
            }
            let n = (e1 | e2) * (self.pending + 1);
            if n > 0 {
                self.put_bit(e2 as u8);
                self.put_bits(e1 as u8, n - 1);
            }
            self.pending = e3 * (self.pending + 1);
            let off = e2 * HALF + e3 * QUARTER;
            self.low = (self.low - off) << 1;
            self.high = ((self.high - off) << 1) | 1;
        }
    }

    /// Code one symbol with the model of `context`.
    #[inline]
    fn push(&mut self, symbol: usize, context: usize) {
        let (cum, f, total) = {
            let m = &mut self.models[context];
            let cum = m.cum(symbol);
            let f = m.freq_of(symbol);
            (cum, f, m.total)
        };
        let span = self.high - self.low + 1;
        let mut r = div_span(span, total);
        if r < 1 {
            r = 1;
        }
        let cum_high = cum + f;
        let new_low = self.low + r * cum;
        let mut new_high = self.low + r * cum_high - 1;
        if new_high < new_low {
            new_high = new_low;
        }
        self.low = new_low;
        self.high = new_high;
        self.renorm();
        self.models[context].add(symbol);
    }

    /// WNC flush plus the zero padded last byte.
    fn finish(mut self) -> Vec<u8> {
        self.pending += 1;
        if self.low < QUARTER {
            self.put_bit(0);
            self.put_bits(1, self.pending);
        } else {
            self.put_bit(1);
            self.put_bits(0, self.pending);
        }
        if self.nbits > 0 {
            self.out.push((self.acc << (8 - self.nbits)) as u8);
            self.acc = 0;
            self.nbits = 0;
        }
        self.out
    }
}

/// Range decoder with one adaptive model per context.
struct CtxRangeDec<'a> {
    models: Vec<Fenwick>,
    data: &'a [u8],
    pos: usize,
    low: i64,
    high: i64,
    value: i64,
}

impl<'a> CtxRangeDec<'a> {
    fn new(data: &'a [u8], n_symbols: usize, n_contexts: usize) -> Self {
        let mut d = CtxRangeDec {
            models: (0..n_contexts).map(|_| Fenwick::new(n_symbols)).collect(),
            data,
            pos: 0,
            low: 0,
            high: (1i64 << 32) - 1,
            value: 0,
        };
        for _ in 0..32 {
            d.value = (d.value << 1) | next_bit(d.data, &mut d.pos);
        }
        d
    }

    /// Decode one symbol with the model of `context`.
    fn decode_symbol(&mut self, context: usize) -> usize {
        let total = self.models[context].total;
        let span = self.high - self.low + 1;
        let mut r = div_span(span, total);
        if r < 1 {
            r = 1;
        }
        let mut target = div_span(self.value - self.low, r);
        if target < 0 {
            target = 0;
        }
        if target >= total {
            target = total - 1;
        }
        let (sym, cumlow, f) = self.models[context].find(target);
        self.low += r * cumlow;
        self.high = self.low + r * f - 1;
        loop {
            let e1 = (self.high < HALF) as i64;
            let e2 = ((self.low >= HALF) & (e1 == 0)) as i64;
            let e3 = ((self.low >= QUARTER) & (self.high < THREE_QUARTERS)
                & (e1 == 0) & (e2 == 0)) as i64;
            if (e1 | e2 | e3) == 0 {
                break;
            }
            let off = e2 * HALF + e3 * QUARTER;
            self.low = (self.low - off) << 1;
            self.high = ((self.high - off) << 1) | 1;
            self.value =
                ((self.value - off) << 1) | next_bit(self.data, &mut self.pos);
        }
        self.models[context].add(sym);
        sym
    }
}

/// Decode exactly `count` symbols, byte-identical to the RangeDecoder
/// reference (zeros beyond the stream end, same clamping policy).
#[no_mangle]
pub unsafe extern "C" fn lum_range_decode(
    data: *const u8, dlen: usize, n_symbols: usize, count: usize, out: *mut u32,
) -> isize {
    if n_symbols == 0 {
        return E_MODEL_SYMS;
    }
    let data = std::slice::from_raw_parts(data, dlen);
    let out: &mut [u32] = if count == 0 {
        &mut []
    } else {
        std::slice::from_raw_parts_mut(out, count)
    };
    let mut d = RangeDec::new(data, n_symbols);
    for i in 0..count {
        out[i] = d.decode_symbol() as u32;
    }
    count as isize
}

// ------------------------------------------- fused per-method decoders
// One native call per tile: adaptive range decode -> (RLE/LZ stage) ->
// residual unpacking. Semantics and error messages mirror the pure-Python
// reference in codec._decode_residuals exactly (same check order, same
// wording), so a corrupt stream fails identically in both modes.

const E_DEC_SYM_BYTE: isize = -50;     // RLE token symbol is not a byte
const E_DEC_LIMIT: isize = -51;        // RLE token stream exceeds length limit
const E_DEC_IMPLAUSIBLE: isize = -52;  // LZ header length implausible
const E_DEC_UNPACK_RANGE: isize = -53; // residual symbol out of range (fused)
const E_DEC_LZ_BAD_SYM: isize = -54;   // invalid LZ token symbol
const E_DEC_LZ_DIST: isize = -55;      // invalid LZ back-reference distance

/// Unpack fixed-width little-endian residual symbols with the range check.
unsafe fn unpack_into(
    packed: &[u8], count: usize, width: usize, modv: u64,
    out: &mut [u32], detail: *mut i32,
) -> isize {
    if packed.len() < count * width {
        return E_UNPACK_TRUNC;
    }
    for i in 0..count {
        let mut v: u64 = 0;
        for b in (0..width).rev() {
            v = (v << 8) | packed[i * width + b] as u64;
        }
        if v >= modv {
            *detail = v as i32;
            return E_DEC_UNPACK_RANGE;
        }
        out[i] = v as u32;
    }
    count as isize
}

/// METHOD_RLE decode: range-decode the token stream (byte literals + EOF)
/// with the reference's bounded-decode guard, then RLE-decode and unpack.
#[no_mangle]
pub unsafe extern "C" fn lum_decode_rle_method(
    data: *const u8, dlen: usize, n_symbols: usize,
    packed_len: usize, width: usize, modv: u64,
    count: usize, out_res: *mut u32, detail: *mut i32,
) -> isize {
    if n_symbols == 0 {
        return E_MODEL_SYMS;
    }
    let data = std::slice::from_raw_parts(data, dlen);
    let out_res: &mut [u32] = if count == 0 {
        &mut []
    } else {
        std::slice::from_raw_parts_mut(out_res, count)
    };
    let mut d = RangeDec::new(data, n_symbols);
    let limit = 2 * packed_len + 8;
    let mut reads = 0usize;
    let mut raw: Vec<u8> = Vec::with_capacity(packed_len + 16);
    loop {
        if reads > limit {
            return E_DEC_LIMIT;
        }
        let sym = d.decode_symbol();
        reads += 1;
        if sym == TOKEN_EOF as usize {
            break;
        }
        if sym > 255 {
            *detail = sym as i32;
            return E_DEC_SYM_BYTE;
        }
        raw.push(sym as u8);
    }
    let mut packed = vec![0u8; packed_len];
    let rc = lum_rle_decode(raw.as_ptr(), raw.len(), packed_len, packed.as_mut_ptr());
    if rc < 0 {
        return rc;
    }
    unpack_into(&packed, count, width, modv, out_res, detail)
}

/// METHOD_LZ decode: range-decode the 4-byte length header and the token
/// stream from ONE session, expand the LZ77 stream, RLE-decode and unpack.
/// Mirrors the reference's streaming LZ decoder, including its symbol cap
/// and check order.
#[no_mangle]
pub unsafe extern "C" fn lum_decode_lz_method(
    data: *const u8, dlen: usize, n_symbols: usize,
    max_rle_len: usize, packed_len: usize, width: usize, modv: u64,
    count: usize, out_res: *mut u32, rle_len_out: *mut u32, detail: *mut i32,
) -> isize {
    if n_symbols == 0 {
        return E_MODEL_SYMS;
    }
    let data = std::slice::from_raw_parts(data, dlen);
    let out_res: &mut [u32] = if count == 0 {
        &mut []
    } else {
        std::slice::from_raw_parts_mut(out_res, count)
    };
    let mut d = RangeDec::new(data, n_symbols);

    // 4-byte big-endian length header of the RLE byte stream
    let mut rle_len = 0usize;
    for _ in 0..4 {
        let s = d.decode_symbol();
        if s > 255 {
            return E_DEC_HDR;
        }
        rle_len = (rle_len << 8) | s;
    }
    *rle_len_out = rle_len as u32;
    if rle_len > max_rle_len {
        return E_DEC_IMPLAUSIBLE;
    }

    // streaming LZ77 expansion (same cap/check order as compress.lz_decode)
    let cap = rle_len + 1;
    let mut symbols = 0usize;
    let mut out: Vec<u8> = Vec::with_capacity(rle_len);
    loop {
        let sym = d.decode_symbol();
        symbols += 1;
        if symbols > cap {
            return E_LZ_SYM_CAP;
        }
        if sym == TOKEN_EOF as usize {
            break;
        }
        if sym < TOKEN_MATCH as usize {
            if out.len() >= rle_len {
                return E_LZ_LIT_OVER;
            }
            out.push(sym as u8);
            continue;
        }
        if sym != TOKEN_MATCH as usize {
            *detail = sym as i32;
            return E_DEC_LZ_BAD_SYM;
        }
        let mut extra = [0u32; 3];
        for k in 0..3 {
            let s = d.decode_symbol();
            if s > 255 {
                return E_LZ_BAD_EXTRA;
            }
            extra[k] = s as u32;
            symbols += 1;
        }
        let l = extra[0] as usize + MINMATCH;
        let d0 = (extra[1] as usize) | ((extra[2] as usize) << 8);
        let dist = 1 + d0;
        if dist > out.len() {
            *detail = dist as i32;
            return E_DEC_LZ_DIST;
        }
        if l + out.len() > rle_len {
            return E_LZ_MATCH_OVER;
        }
        lz_expand_vec(&mut out, dist, l);
    }
    if out.len() != rle_len {
        return E_LZ_MISMATCH;
    }

    let mut packed = vec![0u8; packed_len];
    let rc = lum_rle_decode(out.as_ptr(), out.len(), packed_len, packed.as_mut_ptr());
    if rc < 0 {
        return rc;
    }
    unpack_into(&packed, count, width, modv, out_res, detail)
}

// ------------------------------------------------------- canvas plumbing
// Zero-copy plumbing between the Python side and the native core: the
// caller owns the pixel buffers (array('i')) and the native side reads and
// writes them in place. This removes the per-tile list<->buffer copies that
// used to dominate the container codec.
//
// Every function here is a pure refactor of the existing tile pipeline:
// same arithmetic, same order of operations, same bytes.

/// Copy one tile out of a canvas into a contiguous tile buffer.
///
/// `src` points at the canvas origin, `src_stride` is the canvas row stride
/// in samples, the tile origin is (x, y) in pixels and the tile is
/// `tw` x `th` pixels with `channels` samples each.
#[no_mangle]
pub unsafe extern "C" fn lum_extract_tile(
    src: *const i32, src_stride: usize,
    x: usize, y: usize, tw: usize, th: usize, channels: usize,
    dst: *mut i32,
) -> isize {
    let row_len = tw * channels;
    for r in 0..th {
        let src_row = src.add((y + r) * src_stride + x * channels);
        let dst_row = dst.add(r * row_len);
        std::ptr::copy_nonoverlapping(src_row, dst_row, row_len);
    }
    (th * row_len) as isize
}

/// Reconstruct one tile straight into a canvas buffer.
///
/// `res` holds the tile's residual symbols (rows of `stride` samples),
/// `dst` points at the tile's first pixel inside the canvas and the canvas
/// row stride is `dst_stride` samples.
///
/// The reconstruction keeps the canonical (pre-descale) values in a private
/// row buffer, so the predictor context never sees the descale/inverse
/// transform output -- exactly like the reference implementation, which
/// reconstructs every row first and only then descales and un-transforms.
#[no_mangle]
pub unsafe extern "C" fn lum_pipe_reconstruct_into(
    res: *const i32, n: usize, stride: usize,
    color_space: i32, offset: i64, modv: i64,
    pred_type: i32, channels: usize,
    dst: *mut i32, dst_stride: usize, transform: i32,
) -> isize {
    if channels == 0 || modv <= 0 || n % stride != 0 {
        return E_PIPE_SHAPE;
    }
    let res = std::slice::from_raw_parts(res, n);
    let rows = n / stride;
    let px = stride / channels;
    let is_cs = (color_space == CS_RGB_V || color_space == CS_RGBA_V)
        && transform == 0;
    let ch: usize = if color_space == CS_RGBA_V { 4 } else { 3 };

    let mut cur: Vec<i32> = vec![0; stride];
    let mut prev: Vec<i32> = vec![0; stride];

    for r in 0..rows {
        let row = &res[r * stride..r * stride + stride];
        // 1. predictor reconstruction (canonical domain)
        let mut x = 0usize;
        while x < px {
            let base = x * channels;
            let mut c = 0usize;
            while c < channels {
                let i = base + c;
                let left = if x > 0 { cur[i - channels] as i64 } else { 0 };
                let up = if r > 0 { prev[i] as i64 } else { 0 };
                let ul = if r > 0 && x > 0 { prev[i - channels] as i64 } else { 0 };
                let pred = pred_value(pred_type, left, up, ul);
                cur[i] = py_mod(row[i] as i64 + pred, modv);
                c += 1;
            }
            x += 1;
        }

        // 2. descale + inverse color transform straight into the canvas row
        let dst_row = std::slice::from_raw_parts_mut(dst.add(r * dst_stride), stride);
        if is_cs {
            let mut p = 0usize;
            while p + ch <= stride {
                let y = (cur[p] as i64) - offset;
                let co = (cur[p + 1] as i64) - offset;
                let cg = (cur[p + 2] as i64) - offset;
                let t = y - (cg >> 1);
                let g = cg + t;
                let b = t - (co >> 1);
                let rr = co + b;
                dst_row[p] = rr as i32;
                dst_row[p + 1] = g as i32;
                dst_row[p + 2] = b as i32;
                if ch == 4 {
                    dst_row[p + 3] = (cur[p + 3] as i64 - offset) as i32;
                }
                p += ch;
            }
            while p < stride {
                dst_row[p] = (cur[p] as i64 - offset) as i32;
                p += 1;
            }
        } else {
            let mut i = 0usize;
            while i < stride {
                dst_row[i] = (cur[i] as i64 - offset) as i32;
                i += 1;
            }
        }

        std::mem::swap(&mut prev, &mut cur);
    }
    n as isize
}

/// Read the per-row predictor table (3 bits per row, MSB first).
/// Missing bytes read as zero, exactly like the Python reference.
unsafe fn read_filter_bits(data: &[u8], rows: usize) -> Vec<i32> {
    let mut out: Vec<i32> = Vec::with_capacity(rows);
    let mut acc: u32 = 0;
    let mut nbits: u32 = 0;
    let mut pos = 0usize;
    for _ in 0..rows {
        while nbits < 3 {
            if pos >= data.len() {
                acc <<= 3;
                nbits += 3;
                break;
            }
            acc = (acc << 8) | data[pos] as u32;
            pos += 1;
            nbits += 8;
        }
        out.push(((acc >> (nbits - 3)) & 7) as i32);
        nbits -= 3;
        acc &= (1u32 << nbits) - 1;
    }
    out
}

/// Adaptive front half: colour transform -> canonicalization -> per-row
/// predictor selection (seven candidates scored by the modular L1 distance).
/// Writes the residuals and the packed 3-bit-per-row predictor table.
/// Modular distance of a residual from zero: `min(r, m - r)` where
/// `r = ((d % m) + m) % m`.
///
/// The common case (`-m < d < m`) avoids the division entirely.
#[inline]
fn residual_score(d: i64, modv: i64) -> i64 {
    let r = if d >= 0 {
        if d < modv {
            d
        } else {
            d % modv
        }
    } else if d > -modv {
        d + modv
    } else {
        let mut x = d % modv;
        if x < 0 {
            x += modv;
        }
        x
    };
    if r <= modv - r {
        r
    } else {
        modv - r
    }
}

#[no_mangle]
pub unsafe extern "C" fn lum_pipe_forward_adaptive_into(
    vals: *const i32, n: usize, stride: usize,
    color_space: i32, offset: i64, modv: i64,
    channels: usize, transform: i32,
    out_res: *mut i32, out_bits: *mut u8,
) -> isize {
    if channels == 0 || modv <= 0 || n % stride != 0 {
        return E_PIPE_SHAPE;
    }
    let vals = std::slice::from_raw_parts(vals, n);
    let out = std::slice::from_raw_parts_mut(out_res, n);
    let rows = n / stride;
    let px = stride / channels;
    let is_cs = (color_space == CS_RGB_V || color_space == CS_RGBA_V)
        && transform == 0;
    let ch: usize = if color_space == CS_RGBA_V { 4 } else { 3 };

    // 1. reversible color transform
    let mut tmp: Vec<i32> = vec![0; n];
    if is_cs {
        let mut i = 0usize;
        while i + ch <= n {
            let r = vals[i] as i64;
            let g = vals[i + 1] as i64;
            let b = vals[i + 2] as i64;
            let co = r - b;
            let t = b + (co >> 1);
            let cg = g - t;
            let y = t + (cg >> 1);
            tmp[i] = y as i32;
            tmp[i + 1] = co as i32;
            tmp[i + 2] = cg as i32;
            if ch == 4 {
                tmp[i + 3] = vals[i + 3];
            }
            i += ch;
        }
        while i < n {
            tmp[i] = vals[i];
            i += 1;
        }
    } else {
        tmp.copy_from_slice(vals);
    }

    // 2. canonicalize into the predictor domain
    if offset != 0 {
        for v in tmp.iter_mut() {
            *v = (*v as i64 + offset) as i32;
        }
    }

    // 3. per-row predictor selection
    let mut bits: Vec<u8> = Vec::with_capacity((rows * 3 + 7) / 8);
    let mut acc: u32 = 0;
    let mut nbits: u32 = 0;
    for r in 0..rows {
        let row = &tmp[r * stride..r * stride + stride];
        let has_prev = r > 0;
        let prev: &[i32] = if has_prev { &tmp[(r - 1) * stride..r * stride] } else { &[] };

        // Single pass: score all seven candidates at once. The reference
        // implementation evaluates one candidate per pass, which re-reads
        // the neighbour context seven times and pays seven divisions per
        // sample; here the context is loaded once and the division is only
        // needed in the rare out-of-range case.
        let mut scores = [0i64; 7];
        let mut x = 0usize;
        while x < px {
            let base = x * channels;
            let mut c = 0usize;
            while c < channels {
                let i = base + c;
                let left = if x > 0 { row[i - channels] as i64 } else { 0 };
                let up = if has_prev { prev[i] as i64 } else { 0 };
                let ul = if has_prev && x > 0 { prev[i - channels] as i64 } else { 0 };
                let v = row[i] as i64;
                scores[0] += residual_score(v, modv);
                scores[1] += residual_score(v - left, modv);
                scores[2] += residual_score(v - up, modv);
                scores[3] += residual_score(v - ((left + up) >> 1), modv);
                scores[4] += residual_score(v - paeth(left, up, ul), modv);
                scores[5] += residual_score(v - (left + up - ul), modv);
                scores[6] += residual_score(v - median(left, up, ul), modv);
                c += 1;
            }
            x += 1;
        }

        let mut best_score: i64 = -1;
        let mut best_pt: i32 = 0;
        for pt in 0..7 {
            if best_score < 0 || scores[pt] < best_score {
                best_score = scores[pt];
                best_pt = pt as i32;
            }
        }

        let mut x = 0usize;
        while x < px {
            let base = x * channels;
            let mut c = 0usize;
            while c < channels {
                let i = base + c;
                let left = if x > 0 { row[i - channels] as i64 } else { 0 };
                let up = if has_prev { prev[i] as i64 } else { 0 };
                let ul = if has_prev && x > 0 { prev[i - channels] as i64 } else { 0 };
                let pred = pred_value(best_pt, left, up, ul);
                out[r * stride + i] = py_mod(row[i] as i64 - pred, modv);
                c += 1;
            }
            x += 1;
        }

        acc = (acc << 3) | (best_pt as u32 & 7);
        nbits += 3;
        if nbits >= 8 {
            bits.push(((acc >> (nbits - 8)) & 0xFF) as u8);
            nbits -= 8;
            acc &= (1u32 << nbits) - 1;
        }
    }
    if nbits > 0 {
        bits.push(((acc << (8 - nbits)) & 0xFF) as u8);
    }
    std::ptr::copy_nonoverlapping(bits.as_ptr(), out_bits, bits.len());
    n as isize
}

/// Adaptive back half: rebuild every row with the predictor it was encoded
/// with -> descale -> inverse YCoCg, straight into the canvas.
#[no_mangle]
pub unsafe extern "C" fn lum_pipe_reconstruct_adaptive_into(
    res: *const i32, n: usize, stride: usize,
    color_space: i32, offset: i64, modv: i64,
    channels: usize, transform: i32,
    filter_bits: *const u8, filter_len: usize,
    dst: *mut i32, dst_stride: usize,
) -> isize {
    if channels == 0 || modv <= 0 || n % stride != 0 {
        return E_PIPE_SHAPE;
    }
    let res = std::slice::from_raw_parts(res, n);
    let rows = n / stride;
    let px = stride / channels;
    let is_cs = (color_space == CS_RGB_V || color_space == CS_RGBA_V)
        && transform == 0;
    let ch: usize = if color_space == CS_RGBA_V { 4 } else { 3 };

    let bits: &[u8] = if filter_bits.is_null() { &[] } else { std::slice::from_raw_parts(filter_bits, filter_len) };
    let preds = read_filter_bits(bits, rows);

    let mut cur: Vec<i32> = vec![0; stride];
    let mut prev: Vec<i32> = vec![0; stride];

    for r in 0..rows {
        let row = &res[r * stride..r * stride + stride];
        let pred_type = preds[r];
        let mut x = 0usize;
        while x < px {
            let base = x * channels;
            let mut c = 0usize;
            while c < channels {
                let i = base + c;
                let left = if x > 0 { cur[i - channels] as i64 } else { 0 };
                let up = if r > 0 { prev[i] as i64 } else { 0 };
                let ul = if r > 0 && x > 0 { prev[i - channels] as i64 } else { 0 };
                let pred = pred_value(pred_type, left, up, ul);
                cur[i] = py_mod(row[i] as i64 + pred, modv);
                c += 1;
            }
            x += 1;
        }

        let dst_row = std::slice::from_raw_parts_mut(dst.add(r * dst_stride), stride);
        if is_cs {
            let mut p = 0usize;
            while p + ch <= stride {
                let y = (cur[p] as i64) - offset;
                let co = (cur[p + 1] as i64) - offset;
                let cg = (cur[p + 2] as i64) - offset;
                let t = y - (cg >> 1);
                let g = cg + t;
                let b = t - (co >> 1);
                let rr = co + b;
                dst_row[p] = rr as i32;
                dst_row[p + 1] = g as i32;
                dst_row[p + 2] = b as i32;
                if ch == 4 {
                    dst_row[p + 3] = (cur[p + 3] as i64 - offset) as i32;
                }
                p += ch;
            }
            while p < stride {
                dst_row[p] = (cur[p] as i64 - offset) as i32;
                p += 1;
            }
        } else {
            let mut i = 0usize;
            while i < stride {
                dst_row[i] = (cur[i] as i64 - offset) as i32;
                i += 1;
            }
        }

        std::mem::swap(&mut prev, &mut cur);
    }
    n as isize
}

/// Encode a symbol sequence with the adaptive range coder into `out`.
/// Shared by lum_range_encode and the fused method encoders below.
unsafe fn encode_symbols(
    syms: &[u32], n_symbols: usize, out: *mut u8, out_cap: usize,
) -> isize {
    if n_symbols == 0 {
        return E_MODEL_SYMS;
    }
    let mut enc = RangeEnc::new(n_symbols, 4 * syms.len() + 64);
    for &s in syms.iter() {
        let symbol = s as usize;
        if symbol >= n_symbols {
            return E_ENC_SYM;
        }
        enc.push(symbol);
    }
    let packed = enc.finish();
    if packed.len() > out_cap {
        return E_ENC_BUF;
    }
    std::ptr::copy_nonoverlapping(packed.as_ptr(), out, packed.len());
    packed.len() as isize
}

/// METHOD_RAW encoder: residual symbols straight into the range coder.
/// `res` holds `count` residual symbols (each < modv).
#[no_mangle]
pub unsafe extern "C" fn lum_encode_raw_method(
    res: *const i32, count: usize, modv: u64, out: *mut u8, out_cap: usize,
) -> isize {
    let res = std::slice::from_raw_parts(res, count);
    let mut syms: Vec<u32> = Vec::with_capacity(count);
    for &v in res.iter() {
        if v as u64 >= modv {
            return E_ENC_SYM;
        }
        syms.push(v as u32);
    }
    encode_symbols(&syms, modv as usize, out, out_cap)
}

/// METHOD_RLE encoder: RLE byte stream as literal tokens + EOF terminator,
/// range coded in one pass.
#[no_mangle]
pub unsafe extern "C" fn lum_encode_rle_method(
    rle: *const u8, len: usize, out: *mut u8, out_cap: usize,
) -> isize {
    let rle = std::slice::from_raw_parts(rle, len);
    let mut syms: Vec<u32> = Vec::with_capacity(len + 1);
    for &b in rle.iter() {
        syms.push(b as u32);
    }
    syms.push(TOKEN_EOF as u32);
    encode_symbols(&syms, TOKEN_SYMBOLS as usize, out, out_cap)
}

/// METHOD_LZ encoder: 4-byte big-endian RLE-stream-length header (so the
/// decoder knows the LZ output target) followed by the LZ77 token stream
/// (already EOF-terminated), range coded in one pass.
#[no_mangle]
pub unsafe extern "C" fn lum_encode_lz_method(
    rle: *const u8, len: usize, out: *mut u8, out_cap: usize,
) -> isize {
    let rle = std::slice::from_raw_parts(rle, len);
    let mut tokens: Vec<u32> = vec![0; len + 2];
    let n = lum_lz_encode(rle.as_ptr(), len, tokens.as_mut_ptr());
    if n < 0 {
        return n;
    }
    let n = n as usize;
    let mut syms: Vec<u32> = Vec::with_capacity(4 + n);
    let v = len as u32;
    syms.push((v >> 24) & 0xFF);
    syms.push((v >> 16) & 0xFF);
    syms.push((v >> 8) & 0xFF);
    syms.push(v & 0xFF);
    syms.extend_from_slice(&tokens[..n]);
    encode_symbols(&syms, TOKEN_SYMBOLS as usize, out, out_cap)
}

// --------------------------------------------------------------- planner

const PRED_CANDIDATES_V: [i32; 7] = [0, 1, 2, 3, 4, 5, 6];
const TRANSFORM_YCOCG_V: i32 = 0;
const TRANSFORM_NONE_V: i32 = 1;
const FILTER_GLOBAL_V: i32 = 0;
const FILTER_ADAPTIVE_V: i32 = 1;

/// In-place YCoCg on one row (alpha untouched). Passthrough for the NONE
/// transform and for non-RGB/RGBA colour spaces.
unsafe fn transform_row_inplace(row: &mut [i32], color_space: i32, transform: i32) {
    if transform == TRANSFORM_NONE_V
        || (color_space != CS_RGB_V && color_space != CS_RGBA_V) {
        return;
    }
    let channels: usize = if color_space == CS_RGBA_V { 4 } else { 3 };
    let n = row.len();
    let mut i = 0usize;
    while i + channels <= n {
        let r = row[i] as i64;
        let g = row[i + 1] as i64;
        let b = row[i + 2] as i64;
        let co = r - b;
        let t = b + (co >> 1);
        let cg = g - t;
        let y = t + (cg >> 1);
        row[i] = y as i32;
        row[i + 1] = co as i32;
        row[i + 2] = cg as i32;
        i += channels;
    }
}

/// Residual row for one predictor (apply direction, Python semantics).
unsafe fn predict_row_apply(
    row: &[i32], prev: Option<&[i32]>, pred_type: i32, channels: usize,
    modv: i64, out: &mut Vec<u32>,
) {
    out.clear();
    let len = row.len();
    for i in 0..len {
        let x = i / channels;
        let left = if x > 0 { row[i - channels] as i64 } else { 0 };
        let up = match prev { Some(p) => p[i] as i64, None => 0 };
        let ul = match prev {
            Some(p) => if x > 0 { p[i - channels] as i64 } else { 0 },
            None => 0,
        };
        let pred = pred_value(pred_type, left, up, ul);
        out.push(py_mod(row[i] as i64 - pred, modv) as u32);
    }
}

/// Pick the row predictor with the smallest modular L1 distance (ties go to
/// the first candidate, like the Python reference).
unsafe fn adaptive_row(
    row: &[i32], prev: Option<&[i32]>, channels: usize, modv: i64,
    out: &mut Vec<u32>, scratch: &mut Vec<u32>,
) -> i32 {
    let mut best_score: i64 = -1;
    let mut best_pred: i32 = 0;
    for &pt in PRED_CANDIDATES_V.iter() {
        predict_row_apply(row, prev, pt, channels, modv, scratch);
        let mut score: i64 = 0;
        for &r in scratch.iter() {
            let rr = r as i64;
            score += if rr <= modv - rr { rr } else { modv - rr };
        }
        if best_score < 0 || score < best_score {
            best_score = score;
            best_pred = pt;
            out.clear();
            out.extend_from_slice(scratch);
        }
    }
    best_pred
}

/// Cost of a candidate plan: the number of bytes the real range coder needs
/// for the sampled rows.
unsafe fn plan_cost(syms: &[u32], n_symbols: usize) -> i64 {
    let cap = syms.len() * 4 + 128;
    let mut buf: Vec<u8> = Vec::with_capacity(cap);
    let r = encode_symbols(syms, n_symbols, buf.as_mut_ptr(), cap);
    if r < 0 {
        return i64::MAX;
    }
    r as i64
}

/// Cost of one planner candidate: transform the sampled rows, predict them
/// with the candidate predictor and measure the real range-coded length.
unsafe fn plan_candidate_cost(
    px: &[i32], width: usize, height: usize, channels: usize, bit_depth: u32,
    color_space: i32, rows: usize, transform: i32, filter: i32, pred: i32,
) -> i64 {
    let row_len = width * channels;
    let wide = (color_space == CS_RGB_V || color_space == CS_RGBA_V)
        && transform == TRANSFORM_YCOCG_V;
    let modv: i64 = if wide { 1i64 << (bit_depth + 3) } else { 1i64 << bit_depth };
    let offset: i64 = if wide { modv >> 1 } else { 0 };
    let n_symbols = modv as usize;

    // transformed sample rows + their real up-context row
    let mut trows: Vec<Vec<i32>> = Vec::with_capacity(rows);
    let mut prevs: Vec<Vec<i32>> = Vec::with_capacity(rows);
    for i in 0..rows {
        let mut y = (i * height) / rows;
        if y >= height {
            y = height - 1;
        }
        let mut tr: Vec<i32> = px[y * row_len..(y + 1) * row_len].to_vec();
        transform_row_inplace(&mut tr, color_space, transform);
        if offset != 0 {
            for v in tr.iter_mut() {
                *v += offset as i32;
            }
        }
        trows.push(tr);
        if y > 0 {
            let mut pr: Vec<i32> = px[(y - 1) * row_len..y * row_len].to_vec();
            transform_row_inplace(&mut pr, color_space, transform);
            if offset != 0 {
                for v in pr.iter_mut() {
                    *v += offset as i32;
                }
            }
            prevs.push(pr);
        } else {
            prevs.push(Vec::new());
        }
    }

    let mut syms: Vec<u32> = Vec::with_capacity(rows * row_len);
    let mut scratch: Vec<u32> = Vec::with_capacity(row_len);
    let mut scratch2: Vec<u32> = Vec::with_capacity(row_len);

    if filter == FILTER_ADAPTIVE_V {
        for (k, tr) in trows.iter().enumerate() {
            let pv = if prevs[k].is_empty() { None } else { Some(prevs[k].as_slice()) };
            adaptive_row(tr, pv, channels, modv, &mut scratch, &mut scratch2);
            syms.extend_from_slice(&scratch);
        }
    } else {
        for (k, tr) in trows.iter().enumerate() {
            let pv = if prevs[k].is_empty() { None } else { Some(prevs[k].as_slice()) };
            predict_row_apply(tr, pv, pred, channels, modv, &mut scratch);
            syms.extend_from_slice(&scratch);
        }
    }
    plan_cost(&syms, n_symbols)
}

/// Choose (transform, filter_mode, pred_type) for an image.
///
/// The decision is taken on a spread sample of rows; every sampled row is
/// measured with the *real* range coder (byte length), so the cost matches
/// what the encoder will actually produce. The pure-Python reference
/// planner uses the same rows and the same coder, so both backends always
/// agree on the plan.
///
/// The candidates are independent, so they are measured in parallel; the
/// selection afterwards walks them in the reference order with a strict
/// `<`, which keeps the tie-breaking identical.
#[no_mangle]
pub unsafe extern "C" fn lum_plan(
    pixels: *const i32, count: usize, width: usize, height: usize,
    channels: usize, bit_depth: u32, color_space: i32, sample_rows: usize,
    fixed_transform: i32, fixed_filter: i32, fixed_pred: i32,
    out_plan: *mut i32,
) -> isize {
    if width == 0 || height == 0 || channels == 0 || sample_rows == 0 {
        return E_PIPE_SHAPE;
    }
    if count < width * height * channels {
        return E_PIPE_SHAPE;
    }
    let px = std::slice::from_raw_parts(pixels, count);

    let transforms: &[i32] = if fixed_transform >= 0 {
        if fixed_transform == TRANSFORM_YCOCG_V {
            &[TRANSFORM_YCOCG_V][..]
        } else {
            &[TRANSFORM_NONE_V][..]
        }
    } else if color_space == CS_RGB_V || color_space == CS_RGBA_V {
        &[TRANSFORM_YCOCG_V, TRANSFORM_NONE_V][..]
    } else {
        &[TRANSFORM_NONE_V][..]
    };

    let rows = if height < sample_rows { height } else { sample_rows };

    // candidates in reference order: per transform, the global predictors
    // first, then the adaptive row filter
    let mut cands: Vec<(i32, i32, i32)> = Vec::new();
    for &transform in transforms.iter() {
        if fixed_filter != FILTER_ADAPTIVE_V {
            let preds: &[i32] = if fixed_pred >= 0 {
                &[fixed_pred][..]
            } else {
                &PRED_CANDIDATES_V[..]
            };
            for &pt in preds.iter() {
                cands.push((transform, FILTER_GLOBAL_V, pt));
            }
        }
        if fixed_filter != FILTER_GLOBAL_V {
            cands.push((transform, FILTER_ADAPTIVE_V, 0));
        }
    }
    if cands.is_empty() {
        return E_PIPE_SHAPE;
    }

    let costs: Vec<i64> = std::thread::scope(|scope| {
        let mut handles = Vec::with_capacity(cands.len());
        for cand in cands.iter() {
            let px_ref = &px;
            handles.push(scope.spawn(move || {
                plan_candidate_cost(
                    px_ref, width, height, channels, bit_depth, color_space,
                    rows, cand.0, cand.1, cand.2,
                )
            }));
        }
        let mut out = Vec::with_capacity(cands.len());
        for h in handles {
            match h.join() {
                Ok(c) => out.push(c),
                Err(_) => out.push(i64::MAX),
            }
        }
        out
    });

    let mut best_cost: i64 = i64::MAX;
    let mut best_plan: (i32, i32, i32) = (0, 0, 0);
    let mut have = false;
    for (i, cand) in cands.iter().enumerate() {
        let cost = costs[i];
        if !have || cost < best_cost {
            have = true;
            best_cost = cost;
            best_plan = *cand;
        }
    }

    if !have {
        return E_PIPE_SHAPE;
    }
    let out = std::slice::from_raw_parts_mut(out_plan, 3);
    out[0] = best_plan.0;
    out[1] = best_plan.1;
    out[2] = best_plan.2;
    3
}
