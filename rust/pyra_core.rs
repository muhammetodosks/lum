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

/// Token-array LZ77 decode with the reference guard set. `tokens` holds the
/// full symbol stream (0..255 literal, 256 MATCH + 3, 257 EOF); reconstructs
/// exactly `target` bytes.
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
