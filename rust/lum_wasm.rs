// LUMEN WebAssembly entry points.
//
// The browser cannot ask the OS for a decoder, so `web/lum.js` loads this
// module and calls the two functions below. They are thin wrappers around the
// same `lum_image` / `lum_lossy` code the native `.so` exposes, so a `.lum`
// file decoded in a page is bit-identical to one decoded by the CLI.
//
// Memory contract (unchanged from the C ABI):
//   * JS allocates the input with `lum_alloc`, writes the bytes, then calls
//     `lum_wasm_decode` / `lum_wasm_encode`.
//   * Every returned buffer was produced by `Vec::into_boxed_slice` +
//     `mem::forget`, so JS releases it with `lum_free_buf(buf, len)`.
//   * `info` receives five i32: status, width, height, channels, kind where
//     status 0 means success and a negative value is the codec error code,
//     kind 0 is lossless and 1 is lossy.
//
// Build: scripts/build_wasm.sh -> web/lum.wasm

use std::slice;
use std::sync::atomic::{AtomicI32, Ordering};

const MAGIC: &[u8; 8] = b"LUMA\r\n\x1A\n";

const KIND_LOSSLESS: i32 = 0;
const KIND_LOSSY: i32 = 1;

const E_WASM_MAGIC: i32 = -100;
const E_WASM_VERSION: i32 = -101;
const E_WASM_LEN: i32 = -102;
const E_WASM_PARAM: i32 = -103;
const E_WASM_COLORSPACE: i32 = -104;

static LAST_ERROR: AtomicI32 = AtomicI32::new(0);

fn channels_for(color_space: i32) -> usize {
    match color_space {
        1 => 1,
        3 => 4,
        _ => 3,
    }
}

fn color_space_for(channels: usize) -> i32 {
    match channels {
        1 => 1,
        3 => 2,
        4 => 3,
        _ => -1,
    }
}

/// Allocate `len` zeroed bytes inside the wasm heap and hand the pointer to JS.
#[no_mangle]
pub extern "C" fn lum_alloc(len: usize) -> *mut u8 {
    if len == 0 {
        return std::ptr::null_mut();
    }
    let mut buf = vec![0u8; len];
    let ptr = buf.as_mut_ptr();
    std::mem::forget(buf);
    ptr
}

/// Release a buffer obtained from `lum_alloc`.
#[no_mangle]
pub unsafe extern "C" fn lum_alloc_free(ptr: *mut u8, len: usize) {
    if !ptr.is_null() && len > 0 {
        drop(Vec::from_raw_parts(ptr, len, len));
    }
}

/// Decode a `.lum` container into interleaved 8 bit samples.
///
/// Accepts both the lossless container (versions 1-3) and the lossy one
/// (version 6) and reports which one it found through `info[4]`.
#[no_mangle]
pub unsafe extern "C" fn lum_wasm_decode(
    data: *const u8,
    len: usize,
    info: *mut i32,
) -> *mut u8 {
    if info.is_null() {
        return std::ptr::null_mut();
    }
    let info = slice::from_raw_parts_mut(info, 5);
    info[0] = 0;
    info[1] = 0;
    info[2] = 0;
    info[3] = 0;
    info[4] = KIND_LOSSLESS;

    if data.is_null() || len < 16 {
        LAST_ERROR.store(E_WASM_LEN, Ordering::Relaxed);
        info[0] = E_WASM_LEN;
        return std::ptr::null_mut();
    }
    let blob = slice::from_raw_parts(data, len);
    if &blob[0..8] != MAGIC {
        LAST_ERROR.store(E_WASM_MAGIC, Ordering::Relaxed);
        info[0] = E_WASM_MAGIC;
        return std::ptr::null_mut();
    }
    let version = u32::from_be_bytes([blob[8], blob[9], blob[10], blob[11]]) as i32;

    if version == 6 {
        let mut meta = [0i32; 8];
        let rc = crate::lum_lossy::lum_lossy_info(data, len, meta.as_mut_ptr());
        if rc != 0 {
            LAST_ERROR.store(rc as i32, Ordering::Relaxed);
            info[0] = rc as i32;
            return std::ptr::null_mut();
        }
        let width = meta[0];
        let height = meta[1];
        let channels = channels_for(meta[3]);
        let mut out: *mut u8 = std::ptr::null_mut();
        let mut out_len: usize = 0;
        let rc = crate::lum_lossy::lum_lossy_decode(data, len, &mut out, &mut out_len);
        if rc != 0 || out.is_null() {
            LAST_ERROR.store(rc as i32, Ordering::Relaxed);
            info[0] = if rc != 0 { rc as i32 } else { E_WASM_PARAM };
            return std::ptr::null_mut();
        }
        let expect = width as usize * height as usize * channels;
        if out_len != expect {
            crate::lum_image::lum_free_buf(out, out_len);
            LAST_ERROR.store(E_WASM_PARAM, Ordering::Relaxed);
            info[0] = E_WASM_PARAM;
            return std::ptr::null_mut();
        }
        info[0] = 0;
        info[1] = width;
        info[2] = height;
        info[3] = channels as i32;
        info[4] = KIND_LOSSY;
        return out;
    }

    if !(1..=3).contains(&version) {
        LAST_ERROR.store(E_WASM_VERSION, Ordering::Relaxed);
        info[0] = E_WASM_VERSION;
        return std::ptr::null_mut();
    }

    let mut meta = [0i32; 11];
    let mut out: *mut i32 = std::ptr::null_mut();
    let mut out_len: usize = 0;
    let rc = crate::lum_image::lum_decode_image(
        data,
        len,
        &mut out,
        &mut out_len,
        meta.as_mut_ptr(),
    );
    if rc != 0 || out.is_null() {
        LAST_ERROR.store(rc as i32, Ordering::Relaxed);
        info[0] = if rc != 0 { rc as i32 } else { E_WASM_PARAM };
        return std::ptr::null_mut();
    }

    let width = meta[1];
    let height = meta[2];
    let bit_depth = meta[3].clamp(8, 16) as u32;
    let channels = channels_for(meta[4]);
    let count = width as usize * height as usize * channels;

    let src = slice::from_raw_parts(out, out_len);
    let mut bytes: Vec<u8> = Vec::with_capacity(count);
    let shift = bit_depth - 8;
    for i in 0..count.min(src.len()) {
        let v = src[i];
        let v = if v < 0 { 0 } else if v > 255 << shift { 255 } else { v >> shift };
        bytes.push(v as u8);
    }
    crate::lum_image::lum_free_buf_i32(out, out_len);

    let boxed = bytes.into_boxed_slice();
    let ptr = boxed.as_ptr() as *mut u8;
    let n = boxed.len();
    std::mem::forget(boxed);

    info[0] = 0;
    info[1] = width;
    info[2] = height;
    info[3] = channels as i32;
    info[4] = KIND_LOSSLESS;
    LAST_ERROR.store(0, Ordering::Relaxed);
    let _ = n;
    ptr
}

/// Encode interleaved 8 bit samples into a `.lum` container.
///
/// `quality <= 0` selects the lossless codec; `1..=100` selects the lossy one
/// at 4:4:4 and that quality. `*out_len` receives the byte count.
#[no_mangle]
pub unsafe extern "C" fn lum_wasm_encode(
    data: *const u8,
    len: usize,
    width: usize,
    height: usize,
    channels: usize,
    quality: i32,
    out_len: *mut usize,
) -> *mut u8 {
    if out_len.is_null() {
        return std::ptr::null_mut();
    }
    *out_len = 0;
    let color_space = color_space_for(channels);
    if color_space < 0 {
        LAST_ERROR.store(E_WASM_COLORSPACE, Ordering::Relaxed);
        return std::ptr::null_mut();
    }
    if data.is_null() || width == 0 || height == 0 {
        LAST_ERROR.store(E_WASM_PARAM, Ordering::Relaxed);
        return std::ptr::null_mut();
    }
    let need = width * height * channels;
    if len < need {
        LAST_ERROR.store(E_WASM_LEN, Ordering::Relaxed);
        return std::ptr::null_mut();
    }

    let mut out: *mut u8 = std::ptr::null_mut();
    let mut n: usize = 0;
    let rc = if quality > 0 {
        crate::lum_lossy::lum_lossy_encode(
            data,
            need,
            width,
            height,
            channels,
            color_space,
            quality.clamp(1, 100),
            1, // 4:4:4, the setting the published benchmarks use
            &mut out,
            &mut n,
        )
    } else {
        crate::lum_image::lum_encode_image_bytes(
            data,
            need,
            width,
            height,
            channels,
            8,
            color_space,
            -1,   // predictor: auto
            -1,   // method: pick the cheapest of raw/rle/lz per tile
            256,  // tile size, the library default
            -1,   // transform: auto
            -1,   // filter: auto
            height,
            &mut out,
            &mut n,
        )
    };
    if rc != 0 || out.is_null() || n == 0 {
        if !out.is_null() && n > 0 {
            crate::lum_image::lum_free_buf(out, n);
        }
        LAST_ERROR.store(if rc != 0 { rc as i32 } else { E_WASM_PARAM }, Ordering::Relaxed);
        return std::ptr::null_mut();
    }
    *out_len = n;
    LAST_ERROR.store(0, Ordering::Relaxed);
    out
}

/// Last error code produced by a wasm entry point (0 after a success).
#[no_mangle]
pub extern "C" fn lum_wasm_last_error() -> i32 {
    LAST_ERROR.load(Ordering::Relaxed)
}

/// Byte length of a container's header metadata, or a negative error code.
///
/// `meta_out` receives 6 i32: width, height, channels, kind, quality (0 for
/// lossless), version. Used by the JavaScript wrapper to size its output
/// before allocating.
#[no_mangle]
pub unsafe extern "C" fn lum_wasm_info(
    data: *const u8,
    len: usize,
    meta_out: *mut i32,
) -> isize {
    if data.is_null() || meta_out.is_null() || len < 16 {
        return E_WASM_LEN as isize;
    }
    let blob = slice::from_raw_parts(data, len);
    if &blob[0..8] != MAGIC {
        return E_WASM_MAGIC as isize;
    }
    let version = u32::from_be_bytes([blob[8], blob[9], blob[10], blob[11]]) as i32;
    let meta = slice::from_raw_parts_mut(meta_out, 6);
    if version == 6 {
        let mut m = [0i32; 8];
        let rc = crate::lum_lossy::lum_lossy_info(data, len, m.as_mut_ptr());
        if rc != 0 {
            return rc;
        }
        meta[0] = m[0];
        meta[1] = m[1];
        meta[2] = channels_for(m[3]) as i32;
        meta[3] = KIND_LOSSY;
        meta[4] = m[4];
        meta[5] = version;
        return 0;
    }
    if !(1..=3).contains(&version) {
        return E_WASM_VERSION as isize;
    }
    meta[0] = u32::from_be_bytes([blob[12], blob[13], blob[14], blob[15]]) as i32;
    meta[1] = u32::from_be_bytes([blob[16], blob[17], blob[18], blob[19]]) as i32;
    meta[2] = channels_for(blob[21] as i32) as i32;
    meta[3] = KIND_LOSSLESS;
    meta[4] = 0;
    meta[5] = version;
    0
}
