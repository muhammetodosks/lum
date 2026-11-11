// PYRA native core — Rust implementation of the hot-path compression stages.
//
// Stages ported (byte/token-identical to the pure-Python reference in
// compress.py — the container-equivalence test proves it through the
// entropy coder):
//
//   pyra_pack_residuals    fixed-width little-endian residual packing
//   pyra_unpack_residuals  inverse, with strict range validation
//   pyra_rle_encode        byte-level RLE, greedy maximal runs
//   pyra_rle_decode        inverse with all overshoot/truncation guards
//   pyra_lz_encode         greedy longest-match LZ77, 3-byte hash buckets
//   pyra_lz_decode_tokens  token-array LZ77 decode with all guards
//
// Error convention: every fn returns isize (or i64-style handle encoded in
// isize). >= 0 means success (bytes/tokens produced); < 0 is an error code
// that Python maps to CorruptedFileError / ValueError with the reference
// wording.
//
// Build: scripts/build_native.sh  ->  pyra/_libpyra_core.so

#![allow(clippy::needless_range_loop)]

use std::collections::HashMap;

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
pub unsafe extern "C" fn pyra_pack_residuals(
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
pub unsafe extern "C" fn pyra_unpack_residuals(
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
pub unsafe extern "C" fn pyra_rle_encode(data: *const u8, len: usize, out: *mut u8) -> isize {
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
pub unsafe extern "C" fn pyra_rle_decode(
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
                for _ in 0..c {
                    out[o] = 0xFF;
                    o += 1;
                }
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
                for _ in 0..l {
                    out[o] = b_val;
                    o += 1;
                }
            }
        }
    }
    if o != target {
        return E_RLE_MISMATCH;
    }
    o as isize
}

// ----------------------------------------------------------------- LZ77

/// Greedy longest-match LZ77, 3-byte hash buckets (newest-first, bounded
/// depth 32, ties -> smallest distance). Byte/token-identical to Python.
/// Token buffer must hold >= len + 2 entries.
#[no_mangle]
pub unsafe extern "C" fn pyra_lz_encode(
    data: *const u8, len: usize, tokens_out: *mut u32,
) -> isize {
    let data = std::slice::from_raw_parts(data, len);
    let out = std::slice::from_raw_parts_mut(tokens_out, len + 2);
    let mut t = 0usize;

    if len == 0 {
        out[0] = TOKEN_EOF;
        return 1;
    }

    let mut table: HashMap<u32, Vec<u32>> = HashMap::with_capacity(len / 8 + 1);

    macro_rules! add_key {
        ($pos:expr) => {{
            let pos: usize = $pos;
            if pos + 3 <= len {
                let key = (data[pos] as u32)
                    | ((data[pos + 1] as u32) << 8)
                    | ((data[pos + 2] as u32) << 16);
                let bucket = table.entry(key).or_insert_with(Vec::new);
                bucket.push(pos as u32);
                if bucket.len() > BUCKET_MAX {
                    bucket.remove(0);
                }
            }
        }};
    }

    let mut i = 0usize;
    while i < len {
        let mut best_len = 0usize;
        let mut best_dist = 0usize;
        if i + MINMATCH <= len {
            let key = (data[i] as u32)
                | ((data[i + 1] as u32) << 8)
                | ((data[i + 2] as u32) << 16);
            if let Some(positions) = table.get(&key) {
                let max_len = usize::min(len - i, MAXMATCH);
                for &cand in positions.iter().rev() {
                    let dist = i - cand as usize;
                    if dist == 0 || dist > MAXDIST {
                        break;
                    }
                    // verify; self-overlap handled identically by the decoder
                    let mut l = 0usize;
                    while l < max_len && data[cand as usize + l] == data[i + l] {
                        l += 1;
                    }
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
            out[t] = TOKEN_MATCH;
            out[t + 1] = (best_len - MINMATCH) as u32;
            let d0 = best_dist - 1; // zero-based distance
            out[t + 2] = (d0 & 0xFF) as u32;
            out[t + 3] = (d0 >> 8) as u32;
            t += 4;
            for k in i..i + best_len {
                add_key!(k);
            }
            i += best_len;
        } else {
            out[t] = data[i] as u32;
            t += 1;
            add_key!(i);
            i += 1;
        }
    }
    out[t] = TOKEN_EOF;
    t += 1;
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

row_kernel!(pyra_apply_predictor_row, false);
row_kernel!(pyra_reconstruct_row, true);

// ----------------------------------------------------------- YCoCg

/// Reversible RGB->YCoCg over an interleaved pixel list (passthrough for
/// non-RGB/RGBa color spaces, alpha untouched). Byte-identical to Python.
#[no_mangle]
pub unsafe extern "C" fn pyra_transform_forward(
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

/// Inverse of pyra_transform_forward.
#[no_mangle]
pub unsafe extern "C" fn pyra_transform_inverse(
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
pub unsafe extern "C" fn pyra_lz_decode_tokens(
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
        for _ in 0..l {
            out[o] = out[o - dist];
            o += 1;
        }
    }
    if o != target {
        return E_LZ_MISMATCH;
    }
    o as isize
}

// ------------------------------------------------ pipeline fusion

// Fused codec front/back halves: one buffer conversion per call instead of
// one per stage (transform / per-row prediction / packing). Semantics are
// the exact composition of PyraCodec._residual_stream / decode_tile on the
// pure-Python side, proven by container-parity tests.

/// Forward half: YCoCg transform -> canonicalize (+offset) -> per-row
/// predictor residuals. Writes residuals into `out`; prediction contexts
/// come from an internal copy of the canonicalized input, mirroring the
/// Python reference (prev_row = canonicalized transformed slice above).
#[no_mangle]
pub unsafe extern "C" fn pyra_pipe_forward(
    vals: *const i32, n: usize, stride: usize,
    color_space: i32, offset: i64, modv: i64,
    pred_type: i32, channels: usize, out: *mut i32,
) -> isize {
    if channels == 0 || modv <= 0 || n % stride != 0 {
        return E_PIPE_SHAPE;
    }
    let vals = std::slice::from_raw_parts(vals, n);
    let out = std::slice::from_raw_parts_mut(out, n);
    let mut tmp: Vec<i32> = vec![0; n];

    // 1. reversible color transform (passthrough for other spaces)
    let is_cs = color_space == CS_RGB_V || color_space == CS_RGBA_V;
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
pub unsafe extern "C" fn pyra_pipe_reconstruct(
    res: *const i32, n: usize, stride: usize,
    color_space: i32, offset: i64, modv: i64,
    pred_type: i32, channels: usize, out: *mut i32,
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
    let is_cs = color_space == CS_RGB_V || color_space == CS_RGBA_V;
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
}

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
        }
    }

    /// Cumulative frequency strictly below `symbol` (0-based).
    fn cum(&self, symbol: usize) -> i64 {
        let mut s = 0i64;
        let mut i = symbol;
        while i > 0 {
            s += self.tree[i];
            i -= i & i.wrapping_neg();
        }
        s
    }

    /// Find symbol s with cum(s) <= target < cum(s+1).
    /// Returns (symbol, cum_low, freq); binary descent, identical to
    /// FenwickCDF.find including the defensive last-symbol clamp.
    fn find(&self, target: i64) -> (usize, i64, i64) {
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
            (p, self.cum(p), self.freq[p])
        } else {
            (pos, target - remaining, self.freq[pos])
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

#[inline]
fn enc_renorm(low: &mut i64, high: &mut i64, bits: &mut Vec<u8>, pending: &mut i64) {
    loop {
        if *high < HALF {
            // E1: output 0 + pending 1s
            bits.push(0);
            while *pending > 0 {
                bits.push(1);
                *pending -= 1;
            }
            *low <<= 1;
            *high = (*high << 1) | 1;
        } else if *low >= HALF {
            // E2: output 1 + pending 0s
            bits.push(1);
            while *pending > 0 {
                bits.push(0);
                *pending -= 1;
            }
            *low = (*low - HALF) << 1;
            *high = ((*high - HALF) << 1) | 1;
        } else if *low >= QUARTER && *high < THREE_QUARTERS {
            // E3: straddle, pending bit (span doubles back each time,
            // so pending stays small and i64 cannot overflow)
            *pending += 1;
            *low = (*low - QUARTER) << 1;
            *high = ((*high - QUARTER) << 1) | 1;
        } else {
            break;
        }
    }
}

/// Encode `count` symbols as a packed bit string, byte-identical to
/// RangeEncoder(...).flush(). Output buffer >= 2*count + 256 bytes.
#[no_mangle]
pub unsafe extern "C" fn pyra_range_encode(
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
        let mut r = span / total;
        if r < 1 {
            r = 1;
        }
        // Python floor division; the negative-target clamp makes the Rust
        // truncation difference immaterial
        let mut target = (self.value - self.low) / r;
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
            if self.high < HALF {
                self.low <<= 1;
                self.high = (self.high << 1) | 1;
                self.value = (self.value << 1) | next_bit(self.data, &mut self.pos);
            } else if self.low >= HALF {
                self.low = (self.low - HALF) << 1;
                self.high = ((self.high - HALF) << 1) | 1;
                self.value = ((self.value - HALF) << 1) | next_bit(self.data, &mut self.pos);
            } else if self.low >= QUARTER && self.high < THREE_QUARTERS {
                self.low = (self.low - QUARTER) << 1;
                self.high = ((self.high - QUARTER) << 1) | 1;
                self.value = ((self.value - QUARTER) << 1) | next_bit(self.data, &mut self.pos);
            } else {
                break;
            }
        }
        self.model.add(sym);
        sym
    }
}

/// Decode exactly `count` symbols, byte-identical to the RangeDecoder
/// reference (zeros beyond the stream end, same clamping policy).
#[no_mangle]
pub unsafe extern "C" fn pyra_range_decode(
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
pub unsafe extern "C" fn pyra_decode_rle_method(
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
    let rc = pyra_rle_decode(raw.as_ptr(), raw.len(), packed_len, packed.as_mut_ptr());
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
pub unsafe extern "C" fn pyra_decode_lz_method(
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
        for _ in 0..l {
            let b = out[out.len() - dist];
            out.push(b);
        }
    }
    if out.len() != rle_len {
        return E_LZ_MISMATCH;
    }

    let mut packed = vec![0u8; packed_len];
    let rc = pyra_rle_decode(out.as_ptr(), out.len(), packed_len, packed.as_mut_ptr());
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
pub unsafe extern "C" fn pyra_extract_tile(
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
pub unsafe extern "C" fn pyra_pipe_reconstruct_into(
    res: *const i32, n: usize, stride: usize,
    color_space: i32, offset: i64, modv: i64,
    pred_type: i32, channels: usize,
    dst: *mut i32, dst_stride: usize,
) -> isize {
    if channels == 0 || modv <= 0 || n % stride != 0 {
        return E_PIPE_SHAPE;
    }
    let res = std::slice::from_raw_parts(res, n);
    let rows = n / stride;
    let px = stride / channels;
    let is_cs = color_space == CS_RGB_V || color_space == CS_RGBA_V;
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

/// Encode a symbol sequence with the adaptive range coder into `out`.
/// Shared by pyra_range_encode and the fused method encoders below.
unsafe fn encode_symbols(
    syms: &[u32], n_symbols: usize, out: *mut u8, out_cap: usize,
) -> isize {
    if n_symbols == 0 {
        return E_MODEL_SYMS;
    }
    let mut model = Fenwick::new(n_symbols);
    let mut low: i64 = 0;
    let mut high: i64 = (1i64 << 32) - 1; // inclusive
    let mut bits: Vec<u8> = Vec::with_capacity(4 * syms.len() + 64);
    let mut pending: i64 = 0;

    for &s in syms.iter() {
        let symbol = s as usize;
        if symbol >= n_symbols {
            return E_ENC_SYM;
        }
        let cum = model.cum(symbol);
        let f = model.freq[symbol];
        let total = model.total;
        let span = high - low + 1;
        let mut r = span / total;
        if r < 1 {
            r = 1;
        }
        let cum_high = cum + f;
        let new_low = low + r * cum;
        let mut new_high = low + r * cum_high - 1;
        if new_high < new_low {
            new_high = new_low;
        }
        low = new_low;
        high = new_high;
        enc_renorm(&mut low, &mut high, &mut bits, &mut pending);
        model.add(symbol);
    }

    // WNC flush: pending+1 ones (low in lower half) or zeros
    pending += 1;
    if low < QUARTER {
        bits.push(0);
        for _ in 0..pending {
            bits.push(1);
        }
    } else {
        bits.push(1);
        for _ in 0..pending {
            bits.push(0);
        }
    }

    // MSB-first bit packing, zero-padded last byte
    let packed_len = (bits.len() + 7) / 8;
    if packed_len > out_cap {
        return E_ENC_BUF;
    }
    let out = std::slice::from_raw_parts_mut(out, packed_len);
    let mut byte = 0u32;
    let mut nb = 0u32;
    let mut o = 0usize;
    for &b in bits.iter() {
        byte = (byte << 1) | b as u32;
        nb += 1;
        if nb == 8 {
            out[o] = byte as u8;
            o += 1;
            byte = 0;
            nb = 0;
        }
    }
    if nb > 0 {
        out[o] = (byte << (8 - nb)) as u8;
    }
    packed_len as isize
}

/// METHOD_RAW encoder: residual symbols straight into the range coder.
/// `res` holds `count` residual symbols (each < modv).
#[no_mangle]
pub unsafe extern "C" fn pyra_encode_raw_method(
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
pub unsafe extern "C" fn pyra_encode_rle_method(
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
pub unsafe extern "C" fn pyra_encode_lz_method(
    rle: *const u8, len: usize, out: *mut u8, out_cap: usize,
) -> isize {
    let rle = std::slice::from_raw_parts(rle, len);
    let mut tokens: Vec<u32> = vec![0; len + 2];
    let n = pyra_lz_encode(rle.as_ptr(), len, tokens.as_mut_ptr());
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
