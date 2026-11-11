//! Whole-image codec: tile orchestration, parallel method candidates and
//! container assembly.
//!
//! Everything the Python driver used to do tile by tile (extract, transform,
//! predict, pack, RLE, LZ, entropy-encode, index, CRC) happens here in one
//! call. Tiles are independent, so the work is spread over the available
//! cores with `std::thread::scope`; the result is bit-for-bit identical to the
//! sequential reference because no tile depends on another.

use super::*;

// ------------------------------------------------------------------ crc32

/// CRC-32 (IEEE 802.3) table, built at compile time.
const fn crc_table() -> [u32; 256] {
    let mut t = [0u32; 256];
    let mut i = 0usize;
    while i < 256 {
        let mut c = i as u32;
        let mut k = 0;
        while k < 8 {
            c = if c & 1 != 0 {
                0xEDB8_8320 ^ (c >> 1)
            } else {
                c >> 1
            };
            k += 1;
        }
        t[i] = c;
        i += 1;
    }
    t
}

static CRC_TABLE: [u32; 256] = crc_table();

/// CRC-32 identical to `zlib.crc32`.
pub fn crc32(data: &[u8]) -> u32 {
    let mut c: u32 = 0xFFFF_FFFF;
    for &b in data {
        c = CRC_TABLE[((c ^ b as u32) & 0xFF) as usize] ^ (c >> 8);
    }
    c ^ 0xFFFF_FFFF
}

// --------------------------------------------------------------- geometry

const E_IMG_SHAPE: isize = -400;
const E_IMG_HEADER: isize = -401;
const E_IMG_CRC: isize = -402;
const E_IMG_METHOD: isize = -403;
const E_IMG_ALLOC: isize = -404;
const E_IMG_PLAN: isize = -405;

const METHOD_RAW_V: i32 = 0;
const METHOD_RLE_V: i32 = 1;
const METHOD_LZ_V: i32 = 2;
const METHOD_MIX_V: i32 = 3;

#[inline]
fn residual_mod(color_space: i32, bit_depth: u32, transform: i32) -> u64 {
    let extra = if (color_space == CS_RGB_V || color_space == CS_RGBA_V)
        && transform == TRANSFORM_YCOCG_V
    {
        3
    } else {
        0
    };
    1u64 << (bit_depth + extra)
}

#[inline]
fn canonical_offset(color_space: i32, bit_depth: u32, transform: i32) -> i64 {
    if (color_space == CS_RGB_V || color_space == CS_RGBA_V)
        && transform == TRANSFORM_YCOCG_V
    {
        (residual_mod(color_space, bit_depth, transform) >> 1) as i64
    } else {
        0
    }
}

/// Bytes per packed residual (matches the Python reference exactly).
#[inline]
fn pack_width(modv: u64) -> usize {
    (64 - modv.leading_zeros() as usize + 7) / 8
}

/// Packed residual width for the v3 format: only as many bytes as the
/// residual alphabet actually needs (`((mod - 1).bit_length() + 7) / 8`).
/// v1/v2 files used the wider `pack_width` above, so decoding has to pick
/// the right one based on the container version.
#[inline]
fn pack_width_v3(modv: u64) -> usize {
    if modv <= 1 {
        1
    } else {
        (64 - (modv - 1).leading_zeros() as usize + 7) / 8
    }
}

/// Packed residual width for the given container version.
#[inline]
fn pack_width_for_version(modv: u64, version: i32) -> usize {
    if version >= 3 {
        pack_width_v3(modv)
    } else {
        pack_width(modv)
    }
}

/// Entropy-encode a symbol stream with the adaptive range coder.
///
/// The output buffer grows until the coder fits; the worst case is a few
/// bytes per symbol, so the first guess is already close.
fn encode_symbols_grow(syms: &[u32], n_symbols: usize) -> Vec<u8> {
    let mut cap = syms.len() * 2 + 256;
    loop {
        let mut out = vec![0u8; cap];
        let n = unsafe { encode_symbols(syms, n_symbols, out.as_mut_ptr(), cap) };
        if n >= 0 {
            out.truncate(n as usize);
            return out;
        }
        if n != E_ENC_BUF {
            return Vec::new();
        }
        cap *= 2;
        if cap > 1 << 31 {
            return Vec::new();
        }
    }
}

// ------------------------------------------------------------ tile encode

struct TileGeom {
    x: usize,
    y: usize,
    w: usize,
    h: usize,
}

/// The three method candidates of one tile plus the adaptive row table.
struct TileCandidates {
    raw: Vec<u8>,
    rle: Vec<u8>,
    lz: Vec<u8>,
    filter_bits: Vec<u8>,
}

unsafe fn encode_tile_candidates(
    canvas: &[i32],
    src_stride: usize,
    geom: &TileGeom,
    channels: usize,
    color_space: i32,
    offset: i64,
    modv: i64,
    pred_type: i32,
    transform: i32,
    filter_mode: i32,
    wanted: &[i32],
) -> Option<TileCandidates> {
    let stride = geom.w * channels;
    let count = stride * geom.h;
    let mut tile: Vec<i32> = vec![0; count];
    for r in 0..geom.h {
        let src = (geom.y + r) * src_stride + geom.x * channels;
        std::ptr::copy_nonoverlapping(
            canvas.as_ptr().add(src),
            tile.as_mut_ptr().add(r * stride),
            stride,
        );
    }

    let mut residuals: Vec<i32> = vec![0; count];
    let mut filter_bits: Vec<u8> = Vec::new();
    if filter_mode == FILTER_ADAPTIVE_V {
        filter_bits = vec![0u8; (geom.h * 3 + 7) / 8];
        let rc = pyra_pipe_forward_adaptive_into(
            tile.as_ptr(),
            count,
            stride,
            color_space,
            offset,
            modv,
            channels,
            transform,
            residuals.as_mut_ptr(),
            filter_bits.as_mut_ptr(),
        );
        if rc < 0 {
            return None;
        }
    } else {
        let rc = pyra_pipe_forward(
            tile.as_ptr(),
            count,
            stride,
            color_space,
            offset,
            modv,
            pred_type,
            channels,
            residuals.as_mut_ptr(),
            transform,
        );
        if rc < 0 {
            return None;
        }
    }

    let width = pack_width_v3(modv as u64);
    let mut packed: Vec<u8> = vec![0; count * width];
    let rc = pyra_pack_residuals(
        residuals.as_ptr() as *const u32,
        count,
        width,
        modv as u64,
        packed.as_mut_ptr(),
    );
    if rc < 0 {
        return None;
    }

    // RAW candidate: residual symbols straight into the entropy coder.
    let raw = if wanted.contains(&METHOD_RAW_V) {
        let syms: Vec<u32> = residuals.iter().map(|&v| v as u32).collect();
        encode_symbols_grow(&syms, modv as usize)
    } else {
        Vec::new()
    };

    // RLE / LZ candidates share the packed byte stream.
    let mut rle: Vec<u8> = Vec::new();
    let mut lz: Vec<u8> = Vec::new();
    if wanted.contains(&METHOD_RLE_V) || wanted.contains(&METHOD_LZ_V) {
        let mut rle_raw = vec![0u8; packed.len() * 2 + 16];
        let n = pyra_rle_encode(packed.as_ptr(), packed.len(), rle_raw.as_mut_ptr());
        if n < 0 {
            return None;
        }
        rle_raw.truncate(n as usize);

        if wanted.contains(&METHOD_RLE_V) {
            let mut syms: Vec<u32> = rle_raw.iter().map(|&b| b as u32).collect();
            syms.push(TOKEN_EOF);
            rle = encode_symbols_grow(&syms, TOKEN_SYMBOLS as usize);
        }

        if wanted.contains(&METHOD_LZ_V) {
            let mut tokens: Vec<u32> = vec![0u32; rle_raw.len() + 2];
            let n = pyra_lz_encode(rle_raw.as_ptr(), rle_raw.len(), tokens.as_mut_ptr());
            if n < 0 {
                return None;
            }
            tokens.truncate(n as usize);
            let rle_len = rle_raw.len() as u32;
            let mut syms: Vec<u32> = Vec::with_capacity(tokens.len() + 4);
            syms.push((rle_len >> 24) & 0xFF);
            syms.push((rle_len >> 16) & 0xFF);
            syms.push((rle_len >> 8) & 0xFF);
            syms.push(rle_len & 0xFF);
            syms.extend_from_slice(&tokens);
            lz = encode_symbols_grow(&syms, TOKEN_SYMBOLS as usize);
        }
    }

    Some(TileCandidates {
        raw,
        rle,
        lz,
        filter_bits,
    })
}

// ----------------------------------------------------------- whole encode

/// Encode a whole image and return the container bytes.
///
/// `pred_type`, `transform` and `filter_mode` may be -1 ("auto"), in which
/// case the planner runs first. The result is byte-identical to the Python
/// reference path.
#[no_mangle]
pub unsafe extern "C" fn pyra_encode_image(
    pixels: *const i32,
    count: usize,
    width: usize,
    height: usize,
    channels: usize,
    bit_depth: u32,
    color_space: i32,
    pred_type: i32,
    method: i32,
    tile_size: usize,
    transform: i32,
    filter_mode: i32,
    sample_rows: usize,
    out_ptr: *mut *mut u8,
    out_len: *mut usize,
) -> isize {
    if width == 0 || height == 0 || channels == 0 || tile_size == 0 {
        return E_IMG_SHAPE;
    }
    if count < width * height * channels {
        return E_IMG_SHAPE;
    }
    if method != METHOD_MIX_V && method != METHOD_RAW_V && method != METHOD_RLE_V
        && method != METHOD_LZ_V && method != -1
    {
        return E_IMG_METHOD;
    }
    let px = std::slice::from_raw_parts(pixels, count);

    // 1. planning (auto transform / filter / predictor)
    let mut transform = transform;
    let mut filter_mode = filter_mode;
    let mut pred_type = pred_type;
    if pred_type < 0 || transform < 0 || filter_mode < 0 {
        let mut plan = [0i32; 3];
        let rc = pyra_plan(
            pixels,
            count,
            width,
            height,
            channels,
            bit_depth,
            color_space,
            sample_rows,
            transform,
            filter_mode,
            pred_type,
            plan.as_mut_ptr(),
        );
        if rc < 0 {
            return E_IMG_PLAN;
        }
        if transform < 0 {
            transform = plan[0];
        }
        if filter_mode < 0 {
            filter_mode = plan[1];
        }
        if pred_type < 0 {
            pred_type = plan[2];
        }
    }
    if transform != TRANSFORM_YCOCG_V && transform != TRANSFORM_NONE_V {
        return E_IMG_SHAPE;
    }
    if filter_mode != FILTER_GLOBAL_V && filter_mode != FILTER_ADAPTIVE_V {
        return E_IMG_SHAPE;
    }
    if filter_mode == FILTER_ADAPTIVE_V {
        // the per-row table replaces the global predictor
        pred_type = 0;
    }

    let modv = residual_mod(color_space, bit_depth, transform) as i64;
    let offset = canonical_offset(color_space, bit_depth, transform);
    let src_stride = width * channels;

    // 2. tile geometry
    let mut geoms: Vec<TileGeom> = Vec::new();
    let mut ty = 0usize;
    while ty < height {
        let th = std::cmp::min(tile_size, height - ty);
        let mut tx = 0usize;
        while tx < width {
            let tw = std::cmp::min(tile_size, width - tx);
            geoms.push(TileGeom { x: tx, y: ty, w: tw, h: th });
            tx += tile_size;
        }
        ty += tile_size;
    }

    // 3. per-tile candidates (parallel over tiles)
    let wanted: Vec<i32> = if method == METHOD_MIX_V || method == -1 {
        vec![METHOD_RAW_V, METHOD_RLE_V, METHOD_LZ_V]
    } else {
        vec![method]
    };
    let ntiles = geoms.len();
    let cands: Vec<Option<TileCandidates>> = std::thread::scope(|scope| {
        let mut handles = Vec::with_capacity(ntiles);
        for geom in geoms.iter() {
            let px_ref = &px;
            let wanted_ref = &wanted;
            handles.push(scope.spawn(move || {
                encode_tile_candidates(
                    px_ref,
                    src_stride,
                    geom,
                    channels,
                    color_space,
                    offset,
                    modv,
                    pred_type,
                    transform,
                    filter_mode,
                    wanted_ref,
                )
            }));
        }
        let mut out = Vec::with_capacity(ntiles);
        for h in handles {
            match h.join() {
                Ok(v) => out.push(v),
                Err(_) => out.push(None),
            }
        }
        out
    });
    let mut tiles: Vec<TileCandidates> = Vec::with_capacity(ntiles);
    for c in cands {
        match c {
            Some(c) => tiles.push(c),
            None => return E_IMG_ALLOC,
        }
    }

    // 4. method selection (identical to the reference `_select_candidates`)
    let mut chosen_method = method;
    let mut bodies: Vec<Vec<u8>> = Vec::with_capacity(ntiles);
    if method == METHOD_MIX_V {
        for t in tiles.iter() {
            let mut best_id = METHOD_RAW_V;
            let mut best_len = usize::MAX;
            for (mid, data) in [
                (METHOD_RAW_V, &t.raw),
                (METHOD_RLE_V, &t.rle),
                (METHOD_LZ_V, &t.lz),
            ] {
                if data.len() < best_len || (data.len() == best_len && mid < best_id) {
                    best_len = data.len();
                    best_id = mid;
                }
            }
            let mut body = Vec::with_capacity(t.filter_bits.len() + best_len + 1);
            body.push(best_id as u8);
            body.extend_from_slice(&t.filter_bits);
            match best_id {
                METHOD_RAW_V => body.extend_from_slice(&t.raw),
                METHOD_RLE_V => body.extend_from_slice(&t.rle),
                _ => body.extend_from_slice(&t.lz),
            }
            bodies.push(body);
        }
    } else {
        let pick = if method == -1 {
            let mut totals = [0usize; 3];
            for t in tiles.iter() {
                totals[0] += t.raw.len();
                totals[1] += t.rle.len();
                totals[2] += t.lz.len();
            }
            let mut best = 0usize;
            for m in 1..3 {
                if totals[m] < totals[best] {
                    best = m;
                }
            }
            chosen_method = best as i32;
            best as i32
        } else {
            method
        };
        for t in tiles.iter() {
            let mut body = Vec::with_capacity(t.filter_bits.len() + t.raw.len());
            body.extend_from_slice(&t.filter_bits);
            match pick {
                METHOD_RAW_V => body.extend_from_slice(&t.raw),
                METHOD_RLE_V => body.extend_from_slice(&t.rle),
                _ => body.extend_from_slice(&t.lz),
            }
            bodies.push(body);
        }
    }

    // 5. container assembly
    let header_size = 34usize;
    let index_size = bodies.len() * 8;
    let mut body: Vec<u8> = Vec::with_capacity(
        header_size + 4 + index_size + bodies.iter().map(|b| b.len()).sum::<usize>() + 4,
    );
    body.extend_from_slice(&[0x50, 0x59, 0x52, 0x41, 0x0D, 0x0A, 0x1A, 0x0A]); // "PYRA\r\n\x1A\n"
    body.extend_from_slice(&3u32.to_be_bytes()); // version
    body.extend_from_slice(&(width as u32).to_be_bytes());
    body.extend_from_slice(&(height as u32).to_be_bytes());
    body.push(bit_depth as u8);
    body.push(color_space as u8);
    body.push(pred_type as u8);
    body.push(chosen_method as u8);
    body.extend_from_slice(&(tile_size as u32).to_be_bytes());
    body.extend_from_slice(&(bodies.len() as u32).to_be_bytes());
    body.push(transform as u8);
    body.push(filter_mode as u8);
    body.extend_from_slice(&(index_size as u32).to_be_bytes());
    let mut offset = (header_size + 4 + index_size) as u32;
    for b in bodies.iter() {
        body.extend_from_slice(&offset.to_be_bytes());
        body.extend_from_slice(&(b.len() as u32).to_be_bytes());
        offset += b.len() as u32;
    }
    for b in bodies.iter() {
        body.extend_from_slice(b);
    }
    let crc = crc32(&body);
    body.extend_from_slice(&crc.to_be_bytes());

    let len = body.len();
    let boxed = body.into_boxed_slice();
    let ptr = boxed.as_ptr() as *mut u8;
    std::mem::forget(boxed);
    *out_ptr = ptr;
    *out_len = len;
    0
}

/// Release a buffer returned by `pyra_encode_image` / `pyra_decode_image`.
#[no_mangle]
pub unsafe extern "C" fn pyra_free_buf(ptr: *mut u8, len: usize) {
    if !ptr.is_null() && len > 0 {
        drop(Vec::from_raw_parts(ptr, len, len));
    }
}

#[no_mangle]
pub unsafe extern "C" fn pyra_free_buf_i32(ptr: *mut i32, len: usize) {
    if !ptr.is_null() && len > 0 {
        drop(Vec::from_raw_parts(ptr, len, len));
    }
}

// ----------------------------------------------------------- whole decode

/// Decode a whole container into a flat pixel buffer.
///
/// `meta_out` receives 12 ints: version, width, height, bit_depth,
/// color_space, pred_type, method, tile_size, num_tiles, transform,
/// filter_mode.
#[no_mangle]
pub unsafe extern "C" fn pyra_decode_image(
    data: *const u8,
    len: usize,
    out_ptr: *mut *mut i32,
    out_len: *mut usize,
    meta_out: *mut i32,
) -> isize {
    if len < 36 {
        return E_IMG_HEADER;
    }
    let data = std::slice::from_raw_parts(data, len);

    // magic + crc
    if &data[0..8] != b"PYRA\r\n\x1A\n" {
        return E_IMG_HEADER;
    }
    let body = &data[..len - 4];
    let stored = u32::from_be_bytes([data[len - 4], data[len - 3], data[len - 2], data[len - 1]]);
    if crc32(body) != stored {
        return E_IMG_CRC;
    }

    let version = u32::from_be_bytes([data[8], data[9], data[10], data[11]]) as i32;
    let header_size = match version {
        1 => 32usize,
        2 => 34usize,
        3 => 34usize,
        _ => return E_IMG_HEADER,
    };
    if len < header_size {
        return E_IMG_HEADER;
    }
    let width = u32::from_be_bytes([data[12], data[13], data[14], data[15]]) as usize;
    let height = u32::from_be_bytes([data[16], data[17], data[18], data[19]]) as usize;
    let bit_depth = data[20] as u32;
    let color_space = data[21] as i32;
    let pred_type = data[22] as i32;
    let method = data[23] as i32;
    let tile_size = u32::from_be_bytes([data[24], data[25], data[26], data[27]]) as usize;
    let num_tiles = u32::from_be_bytes([data[28], data[29], data[30], data[31]]) as usize;
    let (transform, filter_mode) = if version >= 2 {
        (data[32] as i32, data[33] as i32)
    } else {
        (TRANSFORM_YCOCG_V, FILTER_GLOBAL_V)
    };

    let channels = if color_space == CS_RGBA_V {
        4
    } else if color_space == 1 {
        1
    } else {
        3
    };
    if width == 0 || height == 0 || channels == 0 || tile_size == 0 {
        return E_IMG_SHAPE;
    }

    // tile index
    let mut off = header_size;
    let index_size = u32::from_be_bytes([data[off], data[off + 1], data[off + 2], data[off + 3]]) as usize;
    off += 4;
    let index = &data[off..off + index_size];
    off += index_size;
    if index.len() < num_tiles * 8 {
        return E_IMG_HEADER;
    }
    let mut tiles: Vec<&[u8]> = Vec::with_capacity(num_tiles);
    for i in 0..num_tiles {
        let p = i * 8;
        let toff = u32::from_be_bytes([index[p], index[p + 1], index[p + 2], index[p + 3]]) as usize;
        let tlen = u32::from_be_bytes([index[p + 4], index[p + 5], index[p + 6], index[p + 7]]) as usize;
        if toff + tlen > data.len() - 4 {
            return E_IMG_HEADER;
        }
        tiles.push(&data[toff..toff + tlen]);
    }

    let modv = residual_mod(color_space, bit_depth, transform) as i64;
    let offset_v = canonical_offset(color_space, bit_depth, transform);
    let width_bytes = pack_width_for_version(modv as u64, version);

    // tile geometry
    let mut geoms: Vec<TileGeom> = Vec::with_capacity(num_tiles);
    let mut ty = 0usize;
    while ty < height {
        let th = std::cmp::min(tile_size, height - ty);
        let mut tx = 0usize;
        while tx < width {
            let tw = std::cmp::min(tile_size, width - tx);
            geoms.push(TileGeom { x: tx, y: ty, w: tw, h: th });
            tx += tile_size;
        }
        ty += tile_size;
    }
    if geoms.len() != num_tiles {
        return E_IMG_SHAPE;
    }

    // decode tiles (parallel)
    let decoded: Vec<Option<Vec<i32>>> = std::thread::scope(|scope| {
        let mut handles = Vec::with_capacity(num_tiles);
        for (i, geom) in geoms.iter().enumerate() {
            let blob = tiles[i];
            handles.push(scope.spawn(move || {
                decode_tile_pixels(
                    blob,
                    geom,
                    channels,
                    color_space,
                    offset_v,
                    modv,
                    pred_type,
                    method,
                    transform,
                    filter_mode,
                    width_bytes,
                )
            }));
        }
        let mut out = Vec::with_capacity(num_tiles);
        for h in handles {
            match h.join() {
                Ok(v) => out.push(v),
                Err(_) => out.push(None),
            }
        }
        out
    });

    // assemble the canvas
    let total = width * height * channels;
    let mut canvas: Vec<i32> = vec![0; total];
    let src_stride = width * channels;
    for (i, geom) in geoms.iter().enumerate() {
        let px = match &decoded[i] {
            Some(p) => p,
            None => return E_IMG_ALLOC,
        };
        let stride = geom.w * channels;
        for r in 0..geom.h {
            let dst = (geom.y + r) * src_stride + geom.x * channels;
            std::ptr::copy_nonoverlapping(
                px.as_ptr().add(r * stride),
                canvas.as_mut_ptr().add(dst),
                stride,
            );
        }
    }

    if !meta_out.is_null() {
        let meta = [
            version,
            width as i32,
            height as i32,
            bit_depth as i32,
            color_space,
            pred_type,
            method,
            tile_size as i32,
            num_tiles as i32,
            transform,
            filter_mode,
        ];
        std::ptr::copy_nonoverlapping(meta.as_ptr(), meta_out, meta.len());
    }

    let len = canvas.len();
    let boxed = canvas.into_boxed_slice();
    let ptr = boxed.as_ptr() as *mut i32;
    std::mem::forget(boxed);
    *out_ptr = ptr;
    *out_len = len;
    0
}

unsafe fn decode_tile_pixels(
    blob: &[u8],
    geom: &TileGeom,
    channels: usize,
    color_space: i32,
    offset_v: i64,
    modv: i64,
    pred_type: i32,
    method: i32,
    transform: i32,
    filter_mode: i32,
    width_bytes: usize,
) -> Option<Vec<i32>> {
    let stride = geom.w * channels;
    let count = stride * geom.h;
    let mut body = blob;

    let mut tile_method = method;
    if method == METHOD_MIX_V {
        if body.is_empty() {
            return None;
        }
        tile_method = body[0] as i32;
        body = &body[1..];
        if tile_method > METHOD_LZ_V {
            return None;
        }
    }
    let mut filter_bits: &[u8] = &[];
    if filter_mode == FILTER_ADAPTIVE_V {
        let nbytes = (geom.h * 3 + 7) / 8;
        if body.len() < nbytes {
            return None;
        }
        filter_bits = &body[..nbytes];
        body = &body[nbytes..];
    }

    let mut residuals: Vec<u32> = vec![0; count];
    let packed_len = count * width_bytes;
    match tile_method {
        METHOD_RAW_V => {
            let rc = pyra_range_decode(
                body.as_ptr(),
                body.len(),
                modv as usize,
                count,
                residuals.as_mut_ptr(),
            );
            if rc < 0 {
                return None;
            }
        }
        METHOD_RLE_V => {
            let mut detail: i32 = 0;
            let rc = pyra_decode_rle_method(
                body.as_ptr(),
                body.len(),
                TOKEN_SYMBOLS as usize,
                packed_len,
                width_bytes,
                modv as u64,
                count,
                residuals.as_mut_ptr(),
                &mut detail,
            );
            if rc < 0 {
                return None;
            }
        }
        _ => {
            let mut detail: i32 = 0;
            let mut rle_len: u32 = 0;
            let rc = pyra_decode_lz_method(
                body.as_ptr(),
                body.len(),
                TOKEN_SYMBOLS as usize,
                2 * packed_len + 64,
                packed_len,
                width_bytes,
                modv as u64,
                count,
                residuals.as_mut_ptr(),
                &mut rle_len,
                &mut detail,
            );
            if rc < 0 {
                return None;
            }
        }
    }

    let res_i32: Vec<i32> = residuals.iter().map(|&v| v as i32).collect();
    let mut out: Vec<i32> = vec![0; count];
    if filter_mode == FILTER_ADAPTIVE_V {
        let rc = pyra_pipe_reconstruct_adaptive_into(
            res_i32.as_ptr(),
            count,
            stride,
            color_space,
            offset_v,
            modv,
            channels,
            transform,
            filter_bits.as_ptr(),
            filter_bits.len(),
            out.as_mut_ptr(),
            stride,
        );
        if rc < 0 {
            return None;
        }
    } else {
        let rc = pyra_pipe_reconstruct_into(
            res_i32.as_ptr(),
            count,
            stride,
            color_space,
            offset_v,
            modv,
            pred_type,
            channels,
            out.as_mut_ptr(),
            stride,
            transform,
        );
        if rc < 0 {
            return None;
        }
    }
    Some(out)
}
