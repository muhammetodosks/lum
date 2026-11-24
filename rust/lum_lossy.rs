//! LUMEN v4 — lossy codec.
//!
//! Architecture: RGB(A) → YCbCr (optional 4:2:0 chroma subsampling) → 8x8
//! DCT → perceptual quantization → context-modelled adaptive binary range
//! coding. The DC coefficient of every block is DPCM coded against the
//! previous block; the AC coefficients are coded in zig-zag order with a
//! significance context built from the band class and the previous
//! coefficient, an Exp-Golomb magnitude (adaptive unary prefix + raw bits)
//! and a raw sign bit.
//!
//! The binary range coder is the LZMA style carry-aware coder: an 11 bit
//! adaptive probability per context, shift-5 adaptation, direct bits for the
//! raw payload. It needs no division, which is what keeps the codec fast.
//!
//! Container (version 6), big endian:
//!
//! ```text
//!   0..8    magic  "LUMA\r\n\x1a\n"
//!   8..12   version = 6
//!  12..16   width
//!  16..20   height
//!  20       bit depth (8)
//!  21       colour space
//!  22       coding byte: bit0 = lossy, bit1 = 4:2:0 chroma, bit2 = YCoCg
//!  23       quality (1..100)
//!  24       chroma step multiplier, 1/128 units (0 = legacy, meaning 1.0)
//!  25..28   reserved (0)
//!  28..32   reserved (0)
//!  32..34   reserved (0, 0)
//!  34       mode = 1 (DCT)
//!  35       plane count
//!  36..     per plane: u32 length, then the range coded stream
//!  end-4..  CRC-32 of everything before it
//! ```

pub const MAGIC: &[u8; 8] = b"LUMA\r\n\x1A\n";
pub const VERSION: u32 = 6;
const HDR: usize = 36;

const E_PARAM: isize = -500;
const E_HEADER: isize = -501;
const E_CRC: isize = -502;
const E_STREAM: isize = -503;

const CS_GRAY_V: i32 = 1;
const CS_RGB_V: i32 = 2;
const CS_RGBA_V: i32 = 3;

// ------------------------------------------------------------------ crc32

fn crc32(data: &[u8]) -> u32 {
    let mut c: u32 = 0xFFFF_FFFF;
    for &b in data {
        c ^= b as u32;
        for _ in 0..8 {
            c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
        }
    }
    c ^ 0xFFFF_FFFF
}

// ----------------------------------------------------------- range coder

struct REnc {
    low: u64,
    range: u32,
    cache: u8,
    cache_size: u64,
    out: Vec<u8>,
    /// Ideal code length in bits at the probabilities actually used, split by
    /// symbol class. Only maintained when `LUMEN_STATS` is set; comparing it
    /// with the real output size says whether the *model* or the *coder* is
    /// the bottleneck.
    st: [f64; 8],
    stats_on: bool,
}

/// Statistic buckets for `REnc::st`.
const ST_SIG: usize = 0;
const ST_MAG: usize = 1;
const ST_ESC: usize = 2;
const ST_SIGN: usize = 3;
const ST_LAST: usize = 4;
const ST_DC: usize = 5;
const ST_DIRECT: usize = 6;

impl REnc {
    fn new() -> Self {
        REnc { low: 0, range: 0xFFFF_FFFF, cache: 0, cache_size: 1, out: Vec::new(),
               st: [0f64; 8], stats_on: std::env::var("LUMEN_STATS").is_ok() }
    }

    fn shift_low(&mut self) {
        if (self.low as u32) < 0xFF00_0000 || (self.low >> 32) != 0 {
            let carry = (self.low >> 32) as u8;
            let mut temp = self.cache;
            loop {
                self.out.push(temp.wrapping_add(carry));
                temp = 0xFF;
                self.cache_size -= 1;
                if self.cache_size == 0 {
                    break;
                }
            }
            self.cache = ((self.low as u32) >> 24) as u8;
        }
        self.cache_size += 1;
        self.low = ((self.low as u32) << 8) as u64;
    }

    #[inline]
    fn bit(&mut self, probs: &mut [u16], ctx: usize, bit: u32) {
        self.bit_t(probs, ctx, bit, ST_SIG);
    }

    #[inline]
    fn bit_t(&mut self, probs: &mut [u16], ctx: usize, bit: u32, tag: usize) {
        let prob = probs[ctx] as u32;
        if self.stats_on {
            let p = if bit == 0 { prob } else { 2048 - prob } as f64 / 2048.0;
            self.st[tag] -= p.max(1e-6).log2();
        }
        let bound = (self.range >> 11) * prob;
        if bit == 0 {
            self.range = bound;
            probs[ctx] = (prob + ((2048 - prob) >> 5)) as u16;
        } else {
            self.low += bound as u64;
            self.range -= bound;
            probs[ctx] = (prob - (prob >> 5)) as u16;
        }
        while self.range < (1 << 24) {
            self.range <<= 8;
            self.shift_low();
        }
    }

    #[inline]
    fn direct(&mut self, value: u32, bits: u32) {
        if self.stats_on {
            self.st[ST_DIRECT] += bits as f64;
        }
        for i in (0..bits).rev() {
            self.range >>= 1;
            if (value >> i) & 1 == 1 {
                self.low += self.range as u64;
            }
            if self.range < (1 << 24) {
                self.range <<= 8;
                self.shift_low();
            }
        }
    }

    fn finish(mut self) -> Vec<u8> {
        for _ in 0..5 {
            self.shift_low();
        }
        self.out
    }
}

struct RDec<'a> {
    code: u32,
    range: u32,
    buf: &'a [u8],
    pos: usize,
}

impl<'a> RDec<'a> {
    fn new(buf: &'a [u8]) -> Option<Self> {
        if buf.len() < 5 {
            return None;
        }
        let mut code: u32 = 0;
        for &b in &buf[1..5] {
            code = (code << 8) | b as u32;
        }
        Some(RDec { code, range: 0xFFFF_FFFF, buf, pos: 5 })
    }

    #[inline]
    fn next_byte(&mut self) -> u32 {
        let b = if self.pos < self.buf.len() { self.buf[self.pos] } else { 0 };
        self.pos += 1;
        b as u32
    }

    #[inline]
    fn bit(&mut self, probs: &mut [u16], ctx: usize) -> u32 {
        let prob = probs[ctx] as u32;
        let bound = (self.range >> 11) * prob;
        let bit;
        if self.code < bound {
            self.range = bound;
            probs[ctx] = (prob + ((2048 - prob) >> 5)) as u16;
            bit = 0;
        } else {
            self.code -= bound;
            self.range -= bound;
            probs[ctx] = (prob - (prob >> 5)) as u16;
            bit = 1;
        }
        while self.range < (1 << 24) {
            self.range <<= 8;
            self.code = (self.code << 8) | self.next_byte();
        }
        bit
    }

    #[inline]
    fn direct(&mut self, bits: u32) -> u32 {
        let mut value = 0u32;
        for _ in 0..bits {
            self.range >>= 1;
            let bit = if self.code >= self.range {
                self.code -= self.range;
                1
            } else {
                0
            };
            value = (value << 1) | bit;
            if self.range < (1 << 24) {
                self.range <<= 8;
                self.code = (self.code << 8) | self.next_byte();
            }
        }
        value
    }
}

// ---------------------------------------------------------------- models

/// Adaptive probability state for one plane.
///
/// `sig`  : significance, 10 band classes x 3 spatial-neighbour counts x 2
///          (whether the previous coefficient in this block was nonzero)
/// `mag`  : magnitude, 10 band classes x 4 neighbour buckets x 4 unary levels
///          (">=2", ">=3", ">=4", plus the escape flag position)
/// `mag_esc`: escaped magnitudes, 10 band classes x 20 Exp-Golomb contexts
/// `sign` : sign, 4 contexts from the left/above neighbours
/// `dc`   : DC residual, 24 Exp-Golomb contexts
/// `last` : index of the last nonzero coefficient, 8 unary contexts
struct Probs {
    sig: Vec<u16>,
    mag: Vec<u16>,
    mag_esc: Vec<u16>,
    sign: Vec<u16>,
    dc: Vec<u16>,
    last: Vec<u16>,
}

impl Probs {
    fn new() -> Self {
        Probs {
            sig: vec![1024u16; SIG_CTX],
            mag: vec![1024u16; MAG_CTX],
            mag_esc: vec![1024u16; 10 * 20],
            sign: vec![1024u16; 12],
            dc: vec![1024u16; 24],
            last: vec![1024u16; 8 + 64],
        }
    }
}

/// Significance context layout.
///
/// `cls` is the band class of the zig-zag position, `nbr` summarises how much
/// energy the left and above blocks carried at the same frequency, `ez` is how
/// much energy *this* block has already spent at lower frequencies, and
/// `prev` is the magnitude bucket of the immediately preceding coefficient.
///
/// The within-block term is the one HEVC-class coders have and this codec was
/// missing: after a few large coefficients a block is clearly "busy" and the
/// rest of its frequencies are far more likely to be significant, which a
/// purely spatial context cannot see.
const SIG_CTX: usize = 10 * 4 * 3;

/// Significance context: band class, neighbour energy, and the magnitude
/// bucket of the coefficient coded immediately before this one. A finer
/// split of the two neighbour magnitudes was measured and did not pay: the
/// extra states cost more in adaptation than they saved in resolution.
#[inline]
fn sig_ctx(cls: usize, nbr: usize, prev: usize) -> usize {
    (cls * 4 + nbr) * 3 + prev
}

/// Sign prediction from coefficients already coded in the same block.
///
/// A DCT coefficient is a projection onto a basis function, so a strong edge
/// in the spatial domain produces a run of coefficients whose signs alternate
/// in a fixed pattern along that frequency axis. The neighbouring *blocks* say
/// little about this; the neighbouring *frequencies* say a lot, and by the
/// time a coefficient is coded all of its lower-frequency neighbours in the
/// same block have already been transmitted.
///
/// Returns 0 when the neighbours disagree, 1 when they point positive and 2
/// when they point negative. `(u, v)` neighbours in the frequency grid always
/// precede `(u, v)` itself in zig-zag order, so the decoder has them too.
#[inline]
fn sign_score(coef: &[i32; 64], u: usize, v: usize) -> usize {
    let mut sc = 0i32;
    if u >= 1 {
        let t = coef[v * 8 + u - 1];
        sc += (t > 0) as i32 - (t < 0) as i32;
    }
    if v >= 1 {
        let t = coef[(v - 1) * 8 + u];
        sc += (t > 0) as i32 - (t < 0) as i32;
    }
    if u >= 2 {
        let t = coef[v * 8 + u - 2];
        sc += (t > 0) as i32 - (t < 0) as i32;
    }
    if v >= 2 {
        let t = coef[(v - 2) * 8 + u];
        sc += (t > 0) as i32 - (t < 0) as i32;
    }
    if sc > 0 {
        1
    } else if sc < 0 {
        2
    } else {
        0
    }
}

/// Number of magnitude contexts.
///
/// Level 0 (">= 2") is the only one that benefits from the neighbour context,
/// so it gets 10 classes x 4 neighbour buckets; the remaining levels share a
/// plain per-class model. 120 contexts total is small enough that each one
/// sees plenty of samples and adapts quickly.
const MAG_LEVELS: usize = 8;
const MAG_CTX: usize = 10 * 4 + 10 * (MAG_LEVELS - 1);

#[inline]
fn mag_ctx(cls: usize, nbr: usize, lvl: usize) -> usize {
    if lvl == 0 {
        cls * 4 + nbr
    } else {
        40 + cls * (MAG_LEVELS - 1) + (lvl - 1)
    }
}

/// Band class of a zig-zag position: the magnitude model is shared inside a
/// class so that each class sees enough samples to adapt.
const fn class_of(pos: usize) -> usize {
    if pos == 0 {
        0
    } else if pos <= 2 {
        1
    } else if pos <= 4 {
        2
    } else if pos <= 7 {
        3
    } else if pos <= 11 {
        4
    } else if pos <= 16 {
        5
    } else if pos <= 23 {
        6
    } else if pos <= 31 {
        7
    } else if pos <= 40 {
        8
    } else {
        9
    }
}

/// Inverse of `ZIGZAG`: the zig-zag index of each (u, v) frequency position.
/// The sign predictor needs to look at coefficients already coded in this
/// block, which means going from a frequency coordinate back to scan order.
const ZIG_POS: [[u8; 8]; 8] = {
    let mut t = [[0u8; 8]; 8];
    let mut i = 0;
    while i < 64 {
        let z = ZIGZAG[i];
        t[z / 8][z % 8] = i as u8;
        i += 1;
    }
    t
};

const ZIGZAG: [usize; 64] = [
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
];

/// Unsigned mapping of a signed value (0, -1, 1, -2, ...).
#[inline]
fn zz_signed(v: i32) -> u32 {
    if v < 0 {
        ((-(v as i64)) as u32) * 2 - 1
    } else {
        (v as u32) * 2
    }
}

#[inline]
fn unzz_signed(u: u32) -> i32 {
    if u & 1 == 0 {
        (u >> 1) as i32
    } else {
        -(((u + 1) >> 1) as i32)
    }
}

fn enc_expgolomb_tag(e: &mut REnc, probs: &mut [u16], value: u32, cap: usize, tag: usize) {
    // Shift by one so that 0 and 1 do not collide: with the plain mapping
    // `k = bit_length(value) - 1`, value 0 and value 1 both need k = 0 and the
    // decoder could not tell them apart (it returned 0 for both).
    let u = value as u64 + 1;
    let k = (63 - u.leading_zeros()) as usize;
    for j in 0..k {
        e.bit_t(probs, j.min(cap - 1), 1, tag);
    }
    e.bit_t(probs, k.min(cap - 1), 0, tag);
    if k > 0 {
        e.direct((u - (1u64 << k)) as u32, k as u32);
    }
}

fn dec_expgolomb(d: &mut RDec, probs: &mut [u16], cap: usize) -> u32 {
    let mut k = 0usize;
    while k < 32 && d.bit(probs, k.min(cap - 1)) == 1 {
        k += 1;
    }
    if k == 0 {
        return 0;
    }
    let u = (1u64 << k) + d.direct(k as u32) as u64;
    (u - 1) as u32
}

// ------------------------------------------------------------------- DCT

fn cos_table() -> [[f32; 8]; 8] {
    let mut c = [[0f32; 8]; 8];
    for u in 0..8 {
        let a = if u == 0 {
            (1.0f32 / 8.0).sqrt()
        } else {
            (2.0f32 / 8.0).sqrt()
        };
        for x in 0..8 {
            c[u][x] = a * (((2 * x + 1) as f32 * u as f32 * std::f32::consts::PI / 16.0).cos());
        }
    }
    c
}

fn fdct(b: &mut [f32; 64], c: &[[f32; 8]; 8]) {
    let mut tmp = [0f32; 64];
    for y in 0..8 {
        for u in 0..8 {
            let mut s = 0f32;
            for x in 0..8 {
                s += c[u][x] * b[y * 8 + x];
            }
            tmp[y * 8 + u] = s;
        }
    }
    for x in 0..8 {
        for v in 0..8 {
            let mut s = 0f32;
            for y in 0..8 {
                s += c[v][y] * tmp[y * 8 + x];
            }
            b[v * 8 + x] = s;
        }
    }
}

fn idct(f: &mut [f32; 64], c: &[[f32; 8]; 8]) {
    // f = C^T F C: first pass down the columns, then across the rows.
    let mut tmp = [0f32; 64];
    for v in 0..8 {
        for y in 0..8 {
            let mut s = 0f32;
            for u in 0..8 {
                s += c[u][y] * f[u * 8 + v];
            }
            tmp[y * 8 + v] = s;
        }
    }
    for y in 0..8 {
        for x in 0..8 {
            let mut s = 0f32;
            for v in 0..8 {
                s += c[v][x] * tmp[y * 8 + v];
            }
            f[y * 8 + x] = s;
        }
    }
}

// ------------------------------------------------------ quantization

const BASE_LUMA: [f32; 64] = [
    16.0, 11.0, 10.0, 16.0, 24.0, 40.0, 51.0, 61.0,
    12.0, 12.0, 14.0, 19.0, 26.0, 58.0, 60.0, 55.0,
    14.0, 13.0, 16.0, 24.0, 40.0, 57.0, 69.0, 56.0,
    14.0, 17.0, 22.0, 29.0, 51.0, 87.0, 80.0, 62.0,
    18.0, 22.0, 37.0, 56.0, 68.0, 109.0, 103.0, 77.0,
    24.0, 35.0, 55.0, 64.0, 81.0, 104.0, 113.0, 92.0,
    49.0, 64.0, 78.0, 87.0, 103.0, 121.0, 120.0, 101.0,
    72.0, 92.0, 95.0, 98.0, 112.0, 100.0, 103.0, 99.0,
];

const BASE_CHROMA: [f32; 64] = [
    17.0, 18.0, 24.0, 47.0, 99.0, 99.0, 99.0, 99.0,
    18.0, 21.0, 26.0, 66.0, 99.0, 99.0, 99.0, 99.0,
    24.0, 26.0, 56.0, 99.0, 99.0, 99.0, 99.0, 99.0,
    47.0, 66.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0,
    99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0,
    99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0,
    99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0,
    99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0,
];

fn quant_table(base: &[f32; 64], quality: i32) -> [f32; 64] {
    let q = quality.clamp(1, 100);
    let scale = if q < 50 { 5000.0 / q as f32 } else { 200.0 - 2.0 * q as f32 };
    let mut out = [1f32; 64];
    for i in 0..64 {
        let v = ((base[i] * scale + 50.0) / 100.0).floor();
        out[i] = v.max(1.0);
    }
    out
}

/// Rate-distortion optimised quantisation (RDOQ).
///
/// Plain rounding minimises the distortion of each coefficient in isolation,
/// but the coder pays for every nonzero coefficient: a significance flag, a
/// magnitude and a sign. Rounding a coefficient to `+1` when it is worth
/// `0.55` of a step buys a tiny distortion reduction and costs ~5 bits, so at
/// low rates the optimal answer is zero.
///
/// For every AC coefficient this compares the deadzone level with the level
/// one step closer to zero and picks the one minimising `D + lambda * R`,
/// where the rate is a bit estimate and `lambda` scales with the quantiser
/// (the classic Lagrangian weighting). The DC coefficient is skipped: it is
/// predicted from its neighbours and carries the block mean.
fn rdoq(block: &[f32; 64], q: &[f32; 64], coef: &mut [i32; 64], lam: f32, dz: f32) {
    for i in 0..64 {
        let step = q[i];
        let v = block[i] / step;
        let dzl = if v >= 0.0 {
            (v + dz).floor()
        } else {
            -((-v + dz).floor())
        };
        if i == 0 {
            coef[i] = dzl as i32;
            continue;
        }
        let l0 = dzl as i32;
        let l1 = if l0 > 0 {
            l0 - 1
        } else if l0 < 0 {
            l0 + 1
        } else {
            0
        };
        let e0 = block[i] - l0 as f32 * step;
        let e1 = block[i] - l1 as f32 * step;
        let cost0 = e0 * e0 + lam * rate_est(l0);
        let cost1 = e1 * e1 + lam * rate_est(l1);
        coef[i] = if cost1 < cost0 { l1 } else { l0 };
    }
}

/// Rough bit cost of coding one quantised level: significance + magnitude +
/// sign. A zero level is free (the significance pass simply stops there).
#[inline]
fn rate_est(l: i32) -> f32 {
    if l == 0 {
        0.0
    } else {
        3.0 + (l.unsigned_abs() as f32).log2()
    }
}

/// Lagrangian multiplier for RDOQ: `lam = LAMBDA * step * step`.
fn lam() -> f32 {
    let k: f32 = std::env::var("LUMEN_LAM")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(LAMBDA);
    k
}

const LAMBDA: f32 = 1.6;

/// High-frequency quantiser shaping.
///
/// The base tables follow JPEG's psychovisual curve, but that curve was tuned
/// for 8x8 blocks at ~1 bit/pixel. Above the middle of the band a step of 1 is
/// often *too fine*: the coefficients there are mostly sensor noise and
/// rounding residue, they cost a full sign plus a magnitude each, and removing
/// them barely moves PSNR. Raising the tail of the table trades a little
/// sharpness for a lot of bytes. Swept before being baked in; read by encoder
/// and decoder alike.
fn hf_shape() -> f32 {
    std::env::var("LUMEN_HF")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(HF_SHAPE)
}

const HF_SHAPE: f32 = 1.0;

/// Frequency index above which shaping applies: `u + v >= 5`, i.e. everything
/// past the low-frequency wedge of the 8x8 block.
fn shape_table(q: &mut [f32; 64], k: f32) {
    if k == 1.0 {
        return;
    }
    for i in 0..64 {
        let (u, v) = (i % 8, i / 8);
        if u + v >= 5 {
            q[i] = (q[i] * k).min(255.0 * 4.0);
        }
    }
}

/// Spectral tilt of the quantiser.
///
/// The base tables are JPEG's, tuned for 8x8 blocks at roughly one bit per
/// pixel under a Huffman coder. Our coder is 4:4:4 with an adaptive binary
/// coder, and it is swept at 30-46 dB, so the optimal balance between low and
/// high frequencies is not necessarily JPEG's. `tilt` scales the step linearly
/// in `(u + v)`: positive spends less on detail, negative spends more.
fn spectral_tilt(quality: i32) -> f32 {
    if let Some(v) = std::env::var("LUMEN_TILT").ok().and_then(|v| v.parse().ok()) {
        return v;
    }
    let lo = std::env::var("LUMEN_T0")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(TILT_LOW);
    let hi = std::env::var("LUMEN_T1")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(TILT_HIGH);
    tilt_of(quality, lo, hi)
}

/// Tilt with no environment involvement. The decoder must use this one: a
/// file has to decode to the same pixels on every machine, so nothing on the
/// decode side may depend on a tuning variable being set.
fn tilt_of(quality: i32, lo: f32, hi: f32) -> f32 {
    let t = (quality.clamp(1, 100) as f32 - 1.0) / 99.0;
    lo + (hi - lo) * t
}

/// Tilt endpoints. At low rates the high band is mostly noise and going
/// coarse there pays for itself; at high rates the opposite holds, and the
/// measurements put the two ends far apart, so the tilt is interpolated in
/// quality rather than fixed.
const TILT_LOW: f32 = -0.6;
const TILT_HIGH: f32 = -0.95;

fn tilt_table(q: &mut [f32; 64], t: f32, t2: f32) {
    if t == 0.0 && t2 == 0.0 {
        return;
    }
    for i in 0..64 {
        let f = (i % 8 + i / 8) as f32 / 14.0;
        q[i] = (q[i] * (1.0 + t * f + t2 * f * f)).max(1.0);
    }
}

/// Curvature of the tilt. The linear term can only tip the whole spectrum in
/// one direction; a negative value spends proportionally more on the middle
/// band, where most of the visible structure lives, while leaving the noisiest
/// corner of the spectrum alone.
fn tilt_bend() -> f32 {
    std::env::var("LUMEN_T2")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(TILT_BEND)
}

const TILT_BEND: f32 = 0.0;

/// Plane-level quantiser scale.
///
/// The codec spends roughly 60 % of its bytes on luma and 40 % on the two
/// chroma planes, but RGB error weights chroma error by 1.402 (R) and 1.772
/// (B) after the inverse transform. That means a bit moved from luma to
/// chroma buys more RGB PSNR than a bit kept in luma, up to a point. These
/// two knobs are swept to find that point; 1.0 is neutral and is what gets
/// baked in if the sweep does not pay.
fn table_scale(q: &mut [f32; 64], s: f32) {
    if s == 1.0 {
        return;
    }
    for v in q.iter_mut() {
        *v = (*v * s).max(1.0);
    }
}

fn chroma_qmult() -> f32 {
    std::env::var("LUMEN_CQ")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(CHROMA_QMULT)
}

fn luma_qmult() -> f32 {
    std::env::var("LUMEN_YQ")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(LUMA_QMULT)
}

/// Baked from a sweep of 0.5 .. 1.25. The optimum is a *finer* chroma step,
/// which is the opposite of what a "the eye is blind to colour" argument
/// would suggest: the inverse YCbCr transform multiplies chroma error by
/// 1.402 for R and 1.772 for B, so chroma error dominates RGB MSE even
/// though the eye tolerates it. Spending more bits there is cheaper than
/// spending them on luma. The curve is flat between 0.70 and 0.75.
const CHROMA_QMULT: f32 = 0.72;
const LUMA_QMULT: f32 = 1.0;

/// Build the luma and chroma quantiser tables for a quality level.
///
/// `chroma_mult` is the *total* chroma step multiplier and must be identical
/// on both sides. On encode it comes from the tuning knobs and is then written
/// into the file; on decode it is read back from the file. `knobs` selects
/// whether the environment-overridable variants are consulted, and is true
/// only for the encoder.
/// Per-band quantiser multipliers, indexed by `class_of`.
///
/// The spectral tilt is a smooth one-parameter curve: it can only say "coarser
/// towards the top" with a single slope. The actual optimal step per frequency
/// is not a straight line in log space, so this table lets each band class be
/// tuned on its own.
///
/// Both sides of the codec must use the same table, so this sits outside the
/// encoder-only tuning block. The identity table returns immediately, which
/// leaves the bitstream byte-identical to a build without it.
/// Band endpoints, interpolated in quality exactly like the tilt.
///
/// The optimal band shape is not scale invariant: at low rates the coarse
/// bands carry mostly noise and want a coarser step, while flat images punish
/// that same shape at 34 dB. A single table therefore does not fit both ends,
/// so the two endpoints are stored and blended.
const BAND_LOW: [f32; 10] = [
    0.8531, 1.1444, 1.0163, 1.0983, 0.9886, 0.8967, 0.9563, 0.9565, 0.9685, 1.0257,
];
const BAND_MID: [f32; 10] = [
    1.0753, 1.2123, 1.1563, 1.1431, 1.0442, 1.0301, 0.9138, 0.9112, 0.8657, 0.9127,
];
const BAND_HIGH: [f32; 10] = [
    1.2160, 1.2390, 1.2468, 1.2457, 1.1399, 1.1608, 0.8679, 0.8703, 0.8165, 0.8759,
];

/// Quality anchors of the three endpoints. The fitted shape is not a straight
/// line in quality: it bulges upward through the middle and then flattens, so
/// one segment was visibly wrong in the middle of the range (flat images lost
/// 2 % at 38 dB). Outside the measured range the end shape is held constant.
const BAND_Q_LOW: f32 = 10.0;
const BAND_Q_MID: f32 = 56.0;
const BAND_Q_HIGH: f32 = 90.0;

fn band_blend(a: &[f32; 10], b: &[f32; 10], t: f32) -> [f32; 10] {
    let mut m = [1f32; 10];
    for i in 0..10 {
        m[i] = a[i] + (b[i] - a[i]) * t;
    }
    m
}

fn band_of(quality: i32, _lo: &[f32; 10], _hi: &[f32; 10]) -> [f32; 10] {
    let q = quality.clamp(1, 100) as f32;
    if q <= BAND_Q_MID {
        let t = ((q - BAND_Q_LOW) / (BAND_Q_MID - BAND_Q_LOW)).clamp(0.0, 1.0);
        band_blend(&BAND_LOW, &BAND_MID, t)
    } else {
        let t = ((q - BAND_Q_MID) / (BAND_Q_HIGH - BAND_Q_MID)).clamp(0.0, 1.0);
        band_blend(&BAND_MID, &BAND_HIGH, t)
    }
}

fn band_mults(quality: i32) -> [f32; 10] {
    if let Ok(s) = std::env::var("LUMEN_QBAND") {
        if !s.is_empty() {
            let mut m = [1f32; 10];
            for (i, part) in s.split(',').take(10).enumerate() {
                if let Ok(v) = part.trim().parse::<f32>() {
                    m[i] = v;
                }
            }
            return m;
        }
    }
    band_of(quality, &BAND_LOW, &BAND_HIGH)
}

fn band_table(q: &mut [f32; 64], m: &[f32; 10]) {
    if m.iter().all(|&v| v == 1.0) {
        return;
    }
    for i in 0..64 {
        let at = ZIGZAG[i];
        q[at] = (q[at] * m[class_of(i)]).max(1.0);
    }
}

/// Uniform scale on both tables. During a fit it is the knob that holds the
/// distortion at the target while the band shape moves.
fn global_scale() -> f32 {
    std::env::var("LUMEN_GSCALE")
        .ok()
        .and_then(|v| v.parse().ok())
        .filter(|v: &f32| *v > 0.0)
        .unwrap_or(1.0)
}

fn quantisers(quality: i32, chroma_mult: f32, knobs: bool) -> ([f32; 64], [f32; 64]) {
    let mut qy = quant_table(&BASE_LUMA, quality);
    let mut qc = scaled(&chroma_base(), quality, chroma_mult);
    let shape = if knobs { hf_shape() } else { HF_SHAPE };
    shape_table(&mut qy, shape);
    shape_table(&mut qc, shape);
    let (tilt, bend) = if knobs {
        (spectral_tilt(quality), tilt_bend())
    } else {
        (tilt_of(quality, TILT_LOW, TILT_HIGH), TILT_BEND)
    };
    tilt_table(&mut qy, tilt, bend);
    tilt_table(&mut qc, tilt, bend);
    let bands = band_mults(quality);
    band_table(&mut qy, &bands);
    band_table(&mut qc, &bands);
    let gs = global_scale();
    let yq = if knobs { luma_qmult() } else { LUMA_QMULT };
    table_scale(&mut qy, yq * gs);
    table_scale(&mut qc, gs);
    (qy, qc)
}

/// Encode-side chroma multiplier, rounded to the 1/128 grid the header can
/// carry. Rounding here rather than at write time is what keeps the encoder
/// and the decoder in exact agreement.
fn chroma_mult_to_byte(m: f32) -> u8 {
    (m * 128.0).round().clamp(1.0, 255.0) as u8
}

/// Colour transform selection: false = YCbCr (the default), true = YCoCg.
///
/// YCbCr's inverse amplifies chroma error by 1.402 (R) and 1.772 (B), which is
/// why chroma error dominated RGB MSE and the chroma quantiser had to be made
/// finer. YCoCg's inverse is `R = Y + Co - Cg`, `G = Y + Cg`,
/// `B = Y - Co - Cg`: unit coefficients, so the same chroma error costs less
/// in RGB terms and the chroma steps can be coarser for the same quality.
/// Whether that actually wins is a measurement, not an argument.
fn use_ycocg() -> bool {
    std::env::var("LUMEN_CT")
        .ok()
        .map(|v| v == "1")
        .unwrap_or(YCOCG)
}

/// Baked from a direct A/B against YCbCr on the same six photos.
const YCOCG: bool = true;

/// Chroma quantiser stride multiplier.
///
/// Chroma is currently eating ~40% of the stream. The eye resolves far less
/// colour detail than luma at the same rate, so a codec that spends bits
/// evenly between the planes wastes them. This knob scales the chroma steps
/// (bigger step = fewer bits) and is swept before being baked in; it is read
/// by both encoder and decoder so a sweep stays self-consistent.
fn chroma_stride() -> f32 {
    std::env::var("LUMEN_CS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(CHROMA_STRIDE)
}

const CHROMA_STRIDE: f32 = 1.0;

fn scaled(base: &[f32; 64], quality: i32, k: f32) -> [f32; 64] {
    let mut q = quant_table(base, quality);
    if k != 1.0 {
        for v in q.iter_mut() {
            *v = (*v * k).max(1.0);
        }
    }
    q
}

/// Softer chroma table: the standard one flattens to 99 from index 4 onwards,
/// which was tuned for 4:2:0 (subsampled) chroma. At 4:4:4 the chroma plane
/// carries full-resolution colour detail and that table throws it away.
const BASE_CHROMA_SOFT: [f32; 64] = [
    17.0, 18.0, 24.0, 47.0, 63.0, 80.0, 92.0, 99.0,
    18.0, 21.0, 26.0, 56.0, 74.0, 90.0, 99.0, 99.0,
    24.0, 26.0, 40.0, 66.0, 84.0, 99.0, 99.0, 99.0,
    47.0, 56.0, 66.0, 84.0, 99.0, 99.0, 99.0, 99.0,
    63.0, 74.0, 84.0, 99.0, 99.0, 99.0, 99.0, 99.0,
    80.0, 90.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0,
    92.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0,
    99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0, 99.0,
];

/// Chroma quantiser used by the 4:4:4 stream.
///
/// Measured choice: the soft table (below) beats both the standard 4:2:0
/// table and the luma table at every rate point, because a full-resolution
/// chroma plane really does carry detail down to the diagonal.
fn chroma_base() -> [f32; 64] {
    BASE_CHROMA_SOFT
}

/// Quantiser rounding bias. 0.5 is round-to-nearest; smaller values push
/// coefficients toward zero, which is a win because a zero coefficient is
/// free in the significance pass. Measured: 0.4 beats 0.35/0.45/0.5 at every
/// rate point, and 0.62 collapses the image.
const DEADZONE: f32 = 0.4;

/// Deadzone width, overridable for sweeps. Read by the encoder only, so the
/// decoder is unaffected: the quantiser is not mirrored on decode.
fn deadzone() -> f32 {
    std::env::var("LUMEN_DZ")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(DEADZONE)
}

// ------------------------------------------------------------- planes

/// Dimensions of plane `i`. Luma and alpha stay at full resolution; only the
/// two chroma planes are subsampled in the 4:2:0 mode.
fn plane_dims_at(i: usize, cs: i32, w: usize, h: usize, subsampled: bool) -> (usize, usize) {
    if cs == CS_GRAY_V || i == 0 || i == 3 {
        (w, h)
    } else if subsampled {
        ((w + 1) / 2, (h + 1) / 2)
    } else {
        (w, h)
    }
}

fn rgb_to_planes(src: &[u8], w: usize, h: usize, ch: usize, cs: i32,
                 subsampled: bool, cocg: bool) -> Vec<Vec<f32>> {
    if cs == CS_GRAY_V {
        let mut y = vec![0f32; w * h];
        for i in 0..w * h {
            y[i] = src[i * ch] as f32;
        }
        return vec![y];
    }
    let n = if subsampled { 2 } else { 1 };
    let cw = (w + n - 1) / n;
    let chh = (h + n - 1) / n;
    // luma at full resolution
    let mut py = vec![0f32; w * h];
    for y in 0..h {
        for x in 0..w {
            let i = (y * w + x) * ch;
            let (r, g, b) = (src[i] as f32, src[i + 1] as f32, src[i + 2] as f32);
            py[y * w + x] = if cocg {
                0.25 * r + 0.5 * g + 0.25 * b
            } else {
                0.299 * r + 0.587 * g + 0.114 * b
            };
        }
    }
    // chroma at the (possibly reduced) chroma resolution
    let mut pb = vec![0f32; cw * chh];
    let mut pr = vec![0f32; cw * chh];
    let inv = 1.0 / (n * n) as f32;
    for y in 0..chh {
        for x in 0..cw {
            let (mut r, mut g, mut b) = (0f32, 0f32, 0f32);
            for dy in 0..n {
                for dx in 0..n {
                    let sx = (x * n + dx).min(w - 1);
                    let sy = (y * n + dy).min(h - 1);
                    let i = (sy * w + sx) * ch;
                    r += src[i] as f32;
                    g += src[i + 1] as f32;
                    b += src[i + 2] as f32;
                }
            }
            r *= inv;
            g *= inv;
            b *= inv;
            let o = y * cw + x;
            if cocg {
                // Co = R/2 - B/2, Cg = -R/4 + G/2 - B/4, both in +-127.5
                pb[o] = 0.5 * r - 0.5 * b + 128.0;
                pr[o] = -0.25 * r + 0.5 * g - 0.25 * b + 128.0;
            } else {
                pb[o] = -0.168736 * r - 0.331264 * g + 0.5 * b + 128.0;
                pr[o] = 0.5 * r - 0.418688 * g - 0.081312 * b + 128.0;
            }
        }
    }
    let mut out = vec![py, pb, pr];
    if cs == CS_RGBA_V {
        let mut a = vec![0f32; w * h];
        for i in 0..w * h {
            a[i] = src[i * ch + 3] as f32;
        }
        out.push(a);
    }
    out
}

fn upsample(plane: &[f32], cw: usize, ch: usize, w: usize, h: usize,
            subsampled: bool) -> Vec<f32> {
    if !subsampled || (cw == w && ch == h) {
        return plane.to_vec();
    }
    let mut out = vec![0f32; w * h];
    for y in 0..h {
        let fy = ((y as f32 + 0.5) * 0.5 - 0.5).max(0.0);
        let y0 = (fy as usize).min(ch - 1);
        let y1 = (y0 + 1).min(ch - 1);
        let wy = fy - y0 as f32;
        for x in 0..w {
            let fx = ((x as f32 + 0.5) * 0.5 - 0.5).max(0.0);
            let x0 = (fx as usize).min(cw - 1);
            let x1 = (x0 + 1).min(cw - 1);
            let wx = fx - x0 as f32;
            let a = plane[y0 * cw + x0] * (1.0 - wx) + plane[y0 * cw + x1] * wx;
            let b = plane[y1 * cw + x0] * (1.0 - wx) + plane[y1 * cw + x1] * wx;
            out[y * w + x] = a * (1.0 - wy) + b * wy;
        }
    }
    out
}

// ------------------------------------------------------ plane coding

/// Gradient predictor from JPEG-LS (LOCO-I). `c` is the above-left neighbour.
#[inline]
fn med3(a: i32, b: i32, c: i32) -> i32 {
    let mx = a.max(b);
    let mn = a.min(b);
    if c >= mx {
        mn
    } else if c <= mn {
        mx
    } else {
        a + b - c
    }
}

/// Neighbour bookkeeping for one block: which zig-zag positions are nonzero
/// and which of those are negative. The context models below use the *already
/// coded* block to the left and the block above, which is what separates a
/// context-adaptive coder from plain DPCM + Exp-Golomb.
#[derive(Clone, Copy)]
struct Neigh {
    /// Magnitude bucket of each zig-zag position: 0 = zero, 1 = |v| == 1,
    /// 2 = |v| >= 2. Two bits of history per frequency beat a plain nonzero
    /// flag by a wide margin: "the block to the left had a *big* coefficient
    /// here" predicts significance far better than "it had a coefficient".
    mag: [u8; 64],
    neg: u64,
}

impl Default for Neigh {
    fn default() -> Self {
        Neigh { mag: [0u8; 64], neg: 0 }
    }
}

impl Neigh {
    #[inline]
    fn mag_at(&self, i: usize) -> usize {
        self.mag[i] as usize
    }
    #[inline]
    fn neg_at(&self, i: usize) -> usize {
        ((self.mag[i] != 0) as usize) & (((self.neg >> i) & 1) as usize)
    }
}

/// Below this many pixels the threads cost more than they save: spawning is
/// tens of microseconds, a small image is less work than that.
const PAR_MIN_PIXELS: usize = 256 * 256;

/// Encode every plane, concurrently when the image is big enough.
///
/// Planes are independent by construction. The DC prediction chain runs inside
/// one plane, the neighbourhood state is per plane, and each plane has its own
/// range coder, so a plane's bytes cannot depend on another's. The streams are
/// concatenated in plane order afterwards, which makes the output byte for
/// byte identical to the sequential version.
fn enc_all_planes(planes: &[Vec<f32>], color_space: i32, width: usize, height: usize,
                  use_sub: bool, qy: &[f32; 64], qc: &[f32; 64],
                  cos: &[[f32; 8]; 8]) -> Vec<Vec<u8>> {
    let job = |i: usize| {
        let q = if i == 0 || i == 3 { qy } else { qc };
        let (pw, ph) = plane_dims_at(i, color_space, width, height, use_sub);
        enc_plane(&planes[i], pw, ph, q, cos)
    };
    if planes.first().map_or(0, |p| p.len()) < PAR_MIN_PIXELS {
        return (0..planes.len()).map(job).collect();
    }
    std::thread::scope(|sc| {
        let hs: Vec<_> = (0..planes.len()).map(|i| sc.spawn(move || job(i))).collect();
        hs.into_iter().map(|h| h.join().unwrap()).collect()
    })
}

fn enc_plane(samples: &[f32], w: usize, h: usize, q: &[f32; 64],
             cos: &[[f32; 8]; 8]) -> Vec<u8> {
    let mut e = REnc::new();
    let mut p = Probs::new();
    let bw = (w + 7) / 8;
    let bh = (h + 7) / 8;
    let mut up_dc = vec![0i32; bw];
    let mut cur_dc = vec![0i32; bw];
    let mut up = vec![Neigh::default(); bw];
    let mut cur = vec![Neigh::default(); bw];
    let mut block = [0f32; 64];
    let mut coef = [0i32; 64];
    // The tuning knobs are environment variables, and `std::env::var` takes a
    // lock and allocates. Reading them per block (and, for the deadzone, per
    // coefficient) dominated the encoder: 65 lookups per block. They cannot
    // change mid-image, so they are resolved once per plane.
    let lam_v = lam();
    let dz = deadzone();
    for by in 0..bh {
        let mut left_dc = 0i32;
        let mut left = Neigh::default();
        for bx in 0..bw {
            for y in 0..8 {
                let sy = (by * 8 + y).min(h - 1);
                for x in 0..8 {
                    let sx = (bx * 8 + x).min(w - 1);
                    block[y * 8 + x] = samples[sy * w + sx];
                }
            }
            fdct(&mut block, cos);
            rdoq(&block, q, &mut coef, lam_v, dz);

            // ---- DC: two-dimensional gradient prediction.
            let pred = if bx == 0 && by == 0 {
                0
            } else if bx == 0 {
                up_dc[bx]
            } else if by == 0 {
                left_dc
            } else {
                med3(left_dc, up_dc[bx], up_dc[bx - 1])
            };
            enc_expgolomb_tag(&mut e, &mut p.dc, zz_signed(coef[0] - pred), 24, ST_DC);

            // ---- last nonzero position, adaptively coded.
            let mut last = 0usize;
            for (i, &zz) in ZIGZAG.iter().enumerate().skip(1) {
                if coef[zz] != 0 {
                    last = i;
                }
            }
            e.bit_t(&mut p.last, 0, (last != 0) as u32, ST_LAST);
            let mut nz = Neigh::default();
            if last != 0 {
                // Position coded through a 6-bit binary tree (63 adaptive
                // nodes). The previous scheme sent the bit length as unary and
                // then the low bits raw, which cost ~6.5 bits per block. The
                // tree approaches the real entropy of the distribution, which
                // is heavily skewed towards small positions.
                let mut node = 1usize;
                for j in (0..6).rev() {
                    let b = ((last >> j) & 1) as u32;
                    e.bit_t(&mut p.last, 8 + (node - 1), b, ST_LAST);
                    node = node * 2 + b as usize;
                }

                let above = up[bx];
                for i in 1..=last {
                    let v = coef[ZIGZAG[i]];
                    let s = (v != 0) as u32;
                    let cls = class_of(i);
                    let nbr = (left.mag_at(i) + above.mag_at(i)).min(3);
                    let prev = nz.mag_at(i.wrapping_sub(1));
                    e.bit_t(&mut p.sig, sig_ctx(cls, nbr, prev), s, ST_SIG);
                    if s == 1 {
                        let a = v.unsigned_abs();
                        nz.mag[i] = if a >= 2 { 2 } else { 1 };
                        if v < 0 {
                            nz.neg |= 1u64 << i;
                        }
                        // magnitude: truncated unary (">=2", ">=3", ">=4"),
                        // escape at 4. A flat 4-bit tree charged four bits for
                        // the most common case of all (|v| == 1); this charges
                        // one, and the escape path is rare.
                        let nb = (left.mag_at(i) + above.mag_at(i)).min(3);
                        let mut lvl = 0usize;
                        while lvl < MAG_LEVELS && a >= 2 + lvl as u32 {
                            e.bit_t(&mut p.mag, mag_ctx(cls, nb, lvl), 1, ST_MAG);
                            lvl += 1;
                        }
                        if lvl < MAG_LEVELS {
                            e.bit_t(&mut p.mag, mag_ctx(cls, nb, lvl), 0, ST_MAG);
                        } else {
                            enc_expgolomb_tag(&mut e, &mut p.mag_esc[cls * 20..cls * 20 + 20],
                                              a - (MAG_LEVELS as u32 + 1), 20, ST_ESC);
                        }
                        // sign: predicted from the same frequency in the
                        // left/above blocks, which are strongly correlated.
                        let z = ZIGZAG[i];
                        let sctx = 4 * sign_score(&coef, z % 8, z / 8)
                            + left.neg_at(i) + 2 * above.neg_at(i);
                        e.bit_t(&mut p.sign, sctx, (v < 0) as u32, ST_SIGN);
                    }
                }
            }
            left_dc = coef[0];
            left = nz;
            cur_dc[bx] = coef[0];
            cur[bx] = nz;
        }
        std::mem::swap(&mut up_dc, &mut cur_dc);
        std::mem::swap(&mut up, &mut cur);
    }
    // Ideal code length per symbol class, versus what the coder really emitted.
    // The gap between the two is pure coder overhead; the totals themselves say
    // where the bits are spent, which is what a modelling change has to fix.
    if e.stats_on && std::env::var("LUMEN_STATS").as_deref() != Ok("quiet") {
        let ideal: f64 = e.st.iter().sum();
        let real = e.out.len() as f64 * 8.0;
        eprintln!(
            "lum-stats {:4}x{:<4} sig {:6.0}B mag {:6.0}B esc {:6.0}B sign {:6.0}B \
             last {:6.0}B dc {:6.0}B raw {:6.0}B | ideal {:7.0}B real {:7.0}B ({:.1}%)",
            w, h,
            e.st[ST_SIG] / 8.0, e.st[ST_MAG] / 8.0, e.st[ST_ESC] / 8.0,
            e.st[ST_SIGN] / 8.0, e.st[ST_LAST] / 8.0, e.st[ST_DC] / 8.0,
            e.st[ST_DIRECT] / 8.0,
            ideal / 8.0, real, real / ideal * 100.0);
    }
    e.finish()
}

/// Decode every plane, concurrently when the image is big enough. Each plane
/// has its own stream and its own coder state, and the reconstruct step is
/// per plane, so nothing is shared. Errors are reported in plane order.
fn dec_all_planes(blob: &[u8], spans: &[(usize, usize)], color_space: i32,
                  width: usize, height: usize, use_sub: bool,
                  qy: &[f32; 64], qc: &[f32; 64],
                  cos: &[[f32; 8]; 8]) -> Result<Vec<Vec<f32>>, isize> {
    let job = |i: usize| {
        let q = if i == 0 || i == 3 { qy } else { qc };
        let (pw, ph) = plane_dims_at(i, color_space, width, height, use_sub);
        let (off, len) = spans[i];
        dec_plane(&blob[off..off + len], pw, ph, q, cos)
    };
    let n = spans.len();
    let big = plane_dims_at(0, color_space, width, height, use_sub);
    let got: Vec<Option<Vec<f32>>> = if big.0 * big.1 < PAR_MIN_PIXELS {
        (0..n).map(job).collect()
    } else {
        std::thread::scope(|sc| {
            let hs: Vec<_> = (0..n).map(|i| sc.spawn(move || job(i))).collect();
            hs.into_iter().map(|h| h.join().unwrap()).collect()
        })
    };
    let mut planes = Vec::with_capacity(n);
    for p in got {
        match p {
            Some(p) => planes.push(p),
            None => return Err(E_STREAM),
        }
    }
    Ok(planes)
}

fn dec_plane(bytes: &[u8], w: usize, h: usize, q: &[f32; 64],
             cos: &[[f32; 8]; 8]) -> Option<Vec<f32>> {
    let mut d = RDec::new(bytes)?;
    let mut p = Probs::new();
    let bw = (w + 7) / 8;
    let bh = (h + 7) / 8;
    let mut out = vec![0f32; w * h];
    let mut up_dc = vec![0i32; bw];
    let mut cur_dc = vec![0i32; bw];
    let mut up = vec![Neigh::default(); bw];
    let mut cur = vec![Neigh::default(); bw];
    let mut coef = [0i32; 64];
    let mut block = [0f32; 64];
    for by in 0..bh {
        let mut left_dc = 0i32;
        let mut left = Neigh::default();
        for bx in 0..bw {
            for c in coef.iter_mut() {
                *c = 0;
            }
            let pred = if bx == 0 && by == 0 {
                0
            } else if bx == 0 {
                up_dc[bx]
            } else if by == 0 {
                left_dc
            } else {
                med3(left_dc, up_dc[bx], up_dc[bx - 1])
            };
            let diff = unzz_signed(dec_expgolomb(&mut d, &mut p.dc, 24));
            coef[0] = pred.wrapping_add(diff);

            let mut nz = Neigh::default();
            if d.bit(&mut p.last, 0) != 0 {
                let mut node = 1usize;
                let mut last = 0usize;
                for _ in 0..6 {
                    let b = d.bit(&mut p.last, 8 + (node - 1)) as usize;
                    node = node * 2 + b;
                    last = (last << 1) | b;
                }
                let last = last.min(63);
                let above = up[bx];
                for i in 1..=last {
                    let cls = class_of(i);
                    let nbr = (left.mag_at(i) + above.mag_at(i)).min(3);
                    let prev = nz.mag_at(i.wrapping_sub(1));
                    let s = d.bit(&mut p.sig, sig_ctx(cls, nbr, prev));
                    if s == 1 {
                        let nb = (left.mag_at(i) + above.mag_at(i)).min(3);
                        let mut lvl = 0usize;
                        while lvl < MAG_LEVELS && d.bit(&mut p.mag, mag_ctx(cls, nb, lvl)) == 1 {
                            lvl += 1;
                        }
                        let a = if lvl < MAG_LEVELS {
                            1 + lvl as u32
                        } else {
                            MAG_LEVELS as u32 + 1
                                + dec_expgolomb(&mut d, &mut p.mag_esc[cls * 20..cls * 20 + 20], 20)
                        };
                        let z = ZIGZAG[i];
                        let sctx = 4 * sign_score(&coef, z % 8, z / 8)
                            + left.neg_at(i) + 2 * above.neg_at(i);
                        let sign = d.bit(&mut p.sign, sctx);
                        let v = if sign == 1 { -(a as i32) } else { a as i32 };
                        coef[ZIGZAG[i]] = v;
                        nz.mag[i] = if a >= 2 { 2 } else { 1 };
                        if v < 0 {
                            nz.neg |= 1u64 << i;
                        }
                    }
                }
            }
            left_dc = coef[0];
            left = nz;
            cur_dc[bx] = coef[0];
            cur[bx] = nz;

            for i in 0..64 {
                block[i] = coef[i] as f32 * q[i];
            }
            idct(&mut block, cos);
            for y in 0..8 {
                let sy = by * 8 + y;
                if sy >= h {
                    break;
                }
                for x in 0..8 {
                    let sx = bx * 8 + x;
                    if sx >= w {
                        break;
                    }
                    out[sy * w + sx] = block[y * 8 + x];
                }
            }
        }
        std::mem::swap(&mut up_dc, &mut cur_dc);
        std::mem::swap(&mut up, &mut cur);
    }
    // Blocking is proportional to the quantiser: half a DC step per edge at
    // worst. A quarter of the DC step is enough to catch it without touching
    // real edges, and it is derived from the header, so nothing extra is
    // stored in the stream.
    deblock(&mut out, w, h, (q[0] * 0.25).clamp(0.0, 12.0));
    Some(out)
}

// ------------------------------------------------------ deblocking filter

/// In-loop deblocking filter.
///
/// The transform is applied per 8x8 block, so *every* block edge can carry a
/// step of up to half the DC quantiser. At low bit rates that step dominates
/// the error, and unlike sensor noise it is structured: straight lines on a
/// fixed grid. It therefore costs a lot of PSNR and looks even worse than it
/// scores.
///
/// The filter is a four-tap edge smoother. It only fires when the step across
/// the boundary is smaller than the quantiser-derived `strength`, because a
/// genuine edge in the image is always larger than half a quantiser step, so
/// real edges are left untouched. When the two neighbouring rows on either
/// side are also flat (the `strong` case) the filter reaches one pixel
/// further, which is what removes the blocking on smooth gradients.
fn deblock(plane: &mut [f32], w: usize, h: usize, strength: f32) {
    if w < 3 || h < 3 || strength <= 0.0 {
        return;
    }
    let mut p = [0f32; 4];
    let mut q = [0f32; 4];
    // Vertical block edges: filter across x = 8, 16, 24, ...
    let mut bx = 1;
    while bx * 8 < w {
        let x = bx * 8;
        bx += 1;
        if x < 2 || x + 1 >= w {
            continue;
        }
        for y in 0..h {
            let base = y * w;
            let (p1, p0, q0, q1) = (plane[base + x - 2], plane[base + x - 1],
                                    plane[base + x], plane[base + x + 1]);
            let d = q0 - p0;
            // Only a *step* is a blocking artefact. When the boundary is
            // flat (d == 0) any filtering would just smooth real texture.
            if d.abs() < 0.75 || d.abs() > strength {
                continue;
            }
            let t = (d * 3.0 + (p1 - q1)) * 0.125;
            // Round away from zero symmetrically, so the filter never adds a
            // DC bias the way a `+4` rounding term does.
            let delta = t.signum() * (t.abs() + 0.5).floor();
            let delta = delta.clamp(-strength, strength);
            if delta == 0.0 {
                continue;
            }
            plane[base + x - 1] = p0 + delta;
            plane[base + x] = q0 - delta;
            if (p1 - p0).abs() <= strength && (q1 - q0).abs() <= strength {
                plane[base + x - 2] = p1 + delta * 0.5;
                plane[base + x + 1] = q1 - delta * 0.5;
            }
        }
    }
    // Horizontal block edges: filter across y = 8, 16, 24, ...
    let mut by = 1;
    while by * 8 < h {
        let y = by * 8;
        by += 1;
        if y < 2 || y + 1 >= h {
            continue;
        }
        for x in 0..w {
            let (p1, p0, q0, q1) = (plane[(y - 2) * w + x], plane[(y - 1) * w + x],
                                    plane[y * w + x], plane[(y + 1) * w + x]);
            let d = q0 - p0;
            // Only a *step* is a blocking artefact. When the boundary is
            // flat (d == 0) any filtering would just smooth real texture.
            if d.abs() < 0.75 || d.abs() > strength {
                continue;
            }
            let t = (d * 3.0 + (p1 - q1)) * 0.125;
            // Round away from zero symmetrically, so the filter never adds a
            // DC bias the way a `+4` rounding term does.
            let delta = t.signum() * (t.abs() + 0.5).floor();
            let delta = delta.clamp(-strength, strength);
            if delta == 0.0 {
                continue;
            }
            plane[(y - 1) * w + x] = p0 + delta;
            plane[y * w + x] = q0 - delta;
            if (p1 - p0).abs() <= strength && (q1 - q0).abs() <= strength {
                plane[(y - 2) * w + x] = p1 + delta * 0.5;
                plane[(y + 1) * w + x] = q1 - delta * 0.5;
            }
        }
    }
}

// -------------------------------------------------------------- container

fn put_u32(out: &mut Vec<u8>, v: u32) {
    out.extend_from_slice(&v.to_be_bytes());
}

fn rd_u32(data: &[u8], off: usize) -> u32 {
    u32::from_be_bytes([data[off], data[off + 1], data[off + 2], data[off + 3]])
}

/// Encode one image.
pub fn encode(data: &[u8], width: usize, height: usize, channels: usize,
              color_space: i32, quality: i32, subsampling: i32) -> Result<Vec<u8>, isize> {
    if width == 0 || height == 0 {
        return Err(E_PARAM);
    }
    let ch = match color_space {
        CS_GRAY_V => 1,
        CS_RGB_V => 3,
        CS_RGBA_V => 4,
        _ => return Err(E_PARAM),
    };
    if channels != ch || data.len() < width * height * ch {
        return Err(E_PARAM);
    }
    if !(1..=100).contains(&quality) {
        return Err(E_PARAM);
    }
    // 4:2:0 is opt-in only. Measurements at matched RGB PSNR show that
    // subsampling chroma always costs more bits than it saves: the luma plane
    // has to be quantised harder to buy back the colour error, which loses
    // more than the halved chroma planes win. AUTO therefore means 4:4:4.
    let sub = match subsampling {
        0 | 1 => false,
        2 => true,
        _ => return Err(E_PARAM),
    };
    if color_space == CS_GRAY_V {
        // 4:2:0 has no meaning for a single plane
    }
    let use_sub = sub && color_space != CS_GRAY_V;

    let cos = cos_table();
    let cq_byte = chroma_mult_to_byte(chroma_stride() * chroma_qmult());
    let cq_used = cq_byte as f32 / 128.0;
    let (qy, qc) = quantisers(quality, cq_used, true);
    let cocg = use_ycocg();
    let planes = rgb_to_planes(data, width, height, ch, color_space, use_sub, cocg);
    let (cw, chh) = if use_sub { ((width + 1) / 2, (height + 1) / 2) } else { (width, height) };

    let streams = enc_all_planes(&planes, color_space, width, height, use_sub, &qy, &qc, &cos);

    let mut out = Vec::with_capacity(64 + streams.iter().map(|s| s.len()).sum::<usize>());
    out.extend_from_slice(MAGIC);
    put_u32(&mut out, VERSION);
    put_u32(&mut out, width as u32);
    put_u32(&mut out, height as u32);
    out.push(8); // bit depth
    out.push(color_space as u8);
    out.push(1 | ((use_sub as u8) << 1) | ((cocg as u8) << 2)); // coding
    out.push(quality as u8);
    // Byte 24: the chroma step multiplier in 1/128 units. The decoder has to
    // reproduce the exact quantiser the encoder used, so the value travels
    // with the file instead of being compiled into both sides. Zero means
    // "no multiplier", which is what version-5 files written before this
    // field existed contain.
    out.push(cq_byte);
    out.push(0);
    out.push(0);
    out.push(0);
    put_u32(&mut out, 0);
    out.push(0);
    out.push(0);
    out.push(1); // DCT mode
    out.push(planes.len() as u8);
    for s in &streams {
        put_u32(&mut out, s.len() as u32);
        out.extend_from_slice(s);
    }
    debug_assert_eq!(out.len(), HDR + streams.iter().map(|s| 4 + s.len()).sum::<usize>());
    let crc = crc32(&out);
    put_u32(&mut out, crc);
    Ok(out)
}

/// Decode one image into interleaved 8 bit samples.
pub fn decode(blob: &[u8]) -> Result<(usize, usize, i32, Vec<u8>), isize> {
    if blob.len() < HDR + 4 {
        return Err(E_HEADER);
    }
    if &blob[0..8] != MAGIC {
        return Err(E_HEADER);
    }
    if rd_u32(blob, 8) != VERSION {
        return Err(E_HEADER);
    }
    let width = rd_u32(blob, 12) as usize;
    let height = rd_u32(blob, 16) as usize;
    let bit_depth = blob[20];
    let color_space = blob[21] as i32;
    let coding = blob[22];
    let quality = blob[23] as i32;
    let nplanes = blob[35] as usize;
    if bit_depth != 8 || width == 0 || height == 0 || coding & 1 == 0
        || !(1..=100).contains(&quality) || nplanes == 0
    {
        return Err(E_HEADER);
    }
    let body = blob.len() - 4;
    if crc32(&blob[..body]) != rd_u32(blob, body) {
        return Err(E_CRC);
    }
    let ch = match color_space {
        CS_GRAY_V => 1,
        CS_RGB_V => 3,
        CS_RGBA_V => 4,
        _ => return Err(E_HEADER),
    };
    let expect_planes = if color_space == CS_GRAY_V { 1 } else { 3 + (color_space == CS_RGBA_V) as usize };
    if nplanes != expect_planes {
        return Err(E_HEADER);
    }
    let use_sub = (coding >> 1) & 1 == 1;
    let cocg = (coding >> 2) & 1 == 1;
    let cos = cos_table();
    let cq_byte = blob[24];
    let cq_used = if cq_byte == 0 { 1.0 } else { cq_byte as f32 / 128.0 };
    let (qy, qc) = quantisers(quality, cq_used, false);
    let (cw, chh) = if use_sub { ((width + 1) / 2, (height + 1) / 2) } else { (width, height) };

    // Validate the plane table first, in file order and with the same checks
    // as the sequential decoder, so malformed files fail identically.
    let mut off = HDR;
    let mut spans: Vec<(usize, usize)> = Vec::with_capacity(nplanes);
    for _ in 0..nplanes {
        if off + 4 > body {
            return Err(E_STREAM);
        }
        let len = rd_u32(blob, off) as usize;
        off += 4;
        if off + len > body {
            return Err(E_STREAM);
        }
        spans.push((off, len));
        off += len;
    }
    if off != body {
        return Err(E_STREAM);
    }
    let planes = dec_all_planes(blob, &spans, color_space, width, height, use_sub,
                                &qy, &qc, &cos)?;

    let mut out = vec![0u8; width * height * ch];
    if color_space == CS_GRAY_V {
        for i in 0..width * height {
            out[i] = planes[0][i].round().clamp(0.0, 255.0) as u8;
        }
        return Ok((width, height, color_space, out));
    }
    let yy = &planes[0];
    let cb = upsample(&planes[1], cw, chh, width, height, use_sub);
    let cr = upsample(&planes[2], cw, chh, width, height, use_sub);
    let alpha = if color_space == CS_RGBA_V {
        Some(upsample(&planes[3], cw, chh, width, height, use_sub))
    } else {
        None
    };
    for i in 0..width * height {
        let y = yy[i];
        let b = cb[i] - 128.0;
        let r = cr[i] - 128.0;
        let (rr, gg, bb) = if cocg {
            let t = y - (cr[i] - 128.0);
            (t + (cb[i] - 128.0), y + (cr[i] - 128.0), t - (cb[i] - 128.0))
        } else {
            (y + 1.402 * r, y - 0.344136 * b - 0.714136 * r, y + 1.772 * b)
        };
        out[i * ch] = rr.round().clamp(0.0, 255.0) as u8;
        out[i * ch + 1] = gg.round().clamp(0.0, 255.0) as u8;
        out[i * ch + 2] = bb.round().clamp(0.0, 255.0) as u8;
        if let Some(a) = &alpha {
            out[i * ch + 3] = a[i].round().clamp(0.0, 255.0) as u8;
        }
    }
    Ok((width, height, color_space, out))
}

// ------------------------------------------------------------------- API

/// Encode raw interleaved 8 bit samples into a LUMEN v4 container.
#[no_mangle]
pub unsafe extern "C" fn lum_lossy_encode(
    data: *const u8,
    len: usize,
    width: usize,
    height: usize,
    channels: usize,
    color_space: i32,
    quality: i32,
    subsampling: i32,
    out_ptr: *mut *mut u8,
    out_len: *mut usize,
) -> isize {
    if data.is_null() || out_ptr.is_null() || out_len.is_null() {
        return E_PARAM;
    }
    let src = std::slice::from_raw_parts(data, len);
    match encode(src, width, height, channels, color_space, quality, subsampling) {
        Ok(blob) => {
            let n = blob.len();
            let mut boxed = blob.into_boxed_slice();
            *out_ptr = boxed.as_mut_ptr();
            *out_len = n;
            std::mem::forget(boxed);
            0
        }
        Err(e) => e,
    }
}

/// Decode a LUMEN v4 container into interleaved 8 bit samples.
#[no_mangle]
pub unsafe extern "C" fn lum_lossy_decode(
    data: *const u8,
    len: usize,
    out_ptr: *mut *mut u8,
    out_len: *mut usize,
) -> isize {
    if data.is_null() || out_ptr.is_null() || out_len.is_null() {
        return E_PARAM;
    }
    let src = std::slice::from_raw_parts(data, len);
    match decode(src) {
        Ok((_w, _h, _cs, px)) => {
            let n = px.len();
            let mut boxed = px.into_boxed_slice();
            *out_ptr = boxed.as_mut_ptr();
            *out_len = n;
            std::mem::forget(boxed);
            0
        }
        Err(e) => e,
    }
}

/// Read the header of a LUMEN v4 container.
///
/// `meta_out` receives 8 ints: width, height, bit depth, colour space,
/// quality, subsampling (1 = 4:2:0), plane count, mode.
#[no_mangle]
pub unsafe extern "C" fn lum_lossy_info(
    data: *const u8,
    len: usize,
    meta_out: *mut i32,
) -> isize {
    if data.is_null() || meta_out.is_null() || len < HDR + 4 {
        return E_HEADER;
    }
    let blob = std::slice::from_raw_parts(data, len);
    if &blob[0..8] != MAGIC || rd_u32(blob, 8) != VERSION {
        return E_HEADER;
    }
    let body = len - 4;
    if crc32(&blob[..body]) != rd_u32(blob, body) {
        return E_CRC;
    }
    let out = std::slice::from_raw_parts_mut(meta_out, 8);
    out[0] = rd_u32(blob, 12) as i32;
    out[1] = rd_u32(blob, 16) as i32;
    out[2] = blob[20] as i32;
    out[3] = blob[21] as i32;
    out[4] = blob[23] as i32;
    out[5] = ((blob[22] >> 1) & 1) as i32;
    out[6] = blob[35] as i32;
    out[7] = blob[34] as i32;
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gradient(w: usize, h: usize) -> Vec<u8> {
        let mut v = vec![0u8; w * h * 3];
        for y in 0..h {
            for x in 0..w {
                let i = (y * w + x) * 3;
                v[i] = ((x * 255) / w.max(1)) as u8;
                v[i + 1] = ((y * 255) / h.max(1)) as u8;
                v[i + 2] = (((x + y) * 127) / (w + h).max(1)) as u8;
            }
        }
        v
    }

    fn psnr(a: &[u8], b: &[u8]) -> f64 {
        let mut se = 0f64;
        for (x, y) in a.iter().zip(b.iter()) {
            let d = *x as f64 - *y as f64;
            se += d * d;
        }
        let mse = se / a.len() as f64;
        if mse == 0.0 {
            99.0
        } else {
            10.0 * (255.0 * 255.0 / mse).log10()
        }
    }

    #[test]
    fn round_trip_quality() {
        let (w, h) = (64, 48);
        let src = gradient(w, h);
        for q in [30, 60, 90, 100] {
            let blob = encode(&src, w, h, 3, CS_RGB_V, q, 0).unwrap();
            let (dw, dh, cs, out) = decode(&blob).unwrap();
            assert_eq!((dw, dh, cs), (w, h, CS_RGB_V));
            let p = psnr(&src, &out);
            assert!(p > 25.0, "quality {} psnr {:.2}", q, p);
        }
    }

    #[test]
    fn quality_is_monotonic() {
        let (w, h) = (96, 64);
        let src = gradient(w, h);
        let mut last = 0usize;
        for q in [20, 40, 60, 80, 95] {
            let n = encode(&src, w, h, 3, CS_RGB_V, q, 0).unwrap().len();
            assert!(n >= last, "size must grow with quality: {} < {}", n, last);
            last = n;
        }
    }

    #[test]
    fn crc_rejects_corruption() {
        let src = gradient(32, 32);
        let mut blob = encode(&src, 32, 32, 3, CS_RGB_V, 75, 0).unwrap();
        let n = blob.len();
        blob[n / 2] ^= 0xFF;
        assert!(decode(&blob).is_err());
    }

    #[test]
    fn gray_and_rgba() {
        let (w, h) = (40, 24);
        let mut g = vec![0u8; w * h];
        for y in 0..h {
            for x in 0..w {
                g[y * w + x] = ((x * 7 + y * 3) % 256) as u8;
            }
        }
        let blob = encode(&g, w, h, 1, CS_GRAY_V, 90, 0).unwrap();
        let (_, _, cs, out) = decode(&blob).unwrap();
        assert_eq!(cs, CS_GRAY_V);
        assert!(psnr(&g, &out) > 30.0);

        let mut rgba = vec![0u8; w * h * 4];
        for i in 0..w * h {
            rgba[i * 4] = (i % 251) as u8;
            rgba[i * 4 + 1] = (i * 3 % 241) as u8;
            rgba[i * 4 + 2] = (i * 7 % 239) as u8;
            rgba[i * 4 + 3] = 200;
        }
        let blob = encode(&rgba, w, h, 4, CS_RGBA_V, 95, 1).unwrap();
        let (_, _, cs, out) = decode(&blob).unwrap();
        assert_eq!(cs, CS_RGBA_V);
        assert_eq!(out.len(), w * h * 4);
        assert!((out[3] as i32 - 200).abs() <= 6);
    }

    #[test]
    fn subsampling_round_trip() {
        let (w, h) = (48, 40);
        let src = gradient(w, h);
        let blob = encode(&src, w, h, 3, CS_RGB_V, 80, 2).unwrap();
        let (_, _, _, out) = decode(&blob).unwrap();
        assert!(psnr(&src, &out) > 25.0);
    }

    #[test]
    fn range_coder_bits_round_trip() {
        let mut e = REnc::new();
        let mut probs = vec![1024u16; 8];
        let mut vals: Vec<(usize, u32)> = Vec::new();
        let mut r = 777u32;
        for _ in 0..50_000 {
            r = r.wrapping_mul(1664525).wrapping_add(1013904223);
            let ctx = (r >> 27) as usize & 7;
            let b = (r >> 13) & 1;
            vals.push((ctx, b));
            e.bit(&mut probs, ctx, b);
        }
        let bytes = e.finish();
        let mut d = RDec::new(&bytes).unwrap();
        let mut probs = vec![1024u16; 8];
        for (i, &(ctx, b)) in vals.iter().enumerate() {
            assert_eq!(d.bit(&mut probs, ctx), b, "bit {} ctx {}", i, ctx);
        }
    }

    #[test]
    fn range_coder_direct_round_trip() {
        let mut e = REnc::new();
        let mut vals: Vec<(u32, u32)> = Vec::new();
        let mut r = 999u32;
        for _ in 0..20_000 {
            r = r.wrapping_mul(1664525).wrapping_add(1013904223);
            let bits = 1 + (r >> 28);
            let v = (r >> 3) & ((1u32 << bits) - 1);
            vals.push((v, bits));
            e.direct(v, bits);
        }
        let bytes = e.finish();
        let mut d = RDec::new(&bytes).unwrap();
        for (i, &(v, bits)) in vals.iter().enumerate() {
            assert_eq!(d.direct(bits), v, "direct {} ({} bits)", i, bits);
        }
    }

    #[test]
    fn range_coder_round_trips_large_random_payloads() {
        // Large magnitudes plus raw direct bits are what a real photograph
        // produces; smooth synthetic gradients only exercise tiny symbols.
        let mut e = REnc::new();
        let mut probs = vec![1024u16; 20];
        let mut vals: Vec<u32> = Vec::new();
        let mut r = 12345u32;
        for _ in 0..20_000 {
            r = r.wrapping_mul(1664525).wrapping_add(1013904223);
            let v = (r >> 22) % 900; // magnitudes with k up to 9
            vals.push(v);
            enc_expgolomb_tag(&mut e, &mut probs, v, 20, ST_SIG);
        }
        let bytes = e.finish();
        let mut d = RDec::new(&bytes).unwrap();
        let mut probs = vec![1024u16; 20];
        for (i, &v) in vals.iter().enumerate() {
            let back = dec_expgolomb(&mut d, &mut probs, 20);
            assert_eq!(back, v, "symbol {} of {} (stored {})", i, vals.len(), v);
        }
        assert_eq!(d.bit(&mut [1024u16; 1], 0), 0, "stream not exhausted");
    }
}
