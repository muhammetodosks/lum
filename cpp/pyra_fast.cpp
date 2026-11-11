// PYRA C++ fast paths (v2 codec).
//
// These functions implement the hottest inner loops of the v2 format:
//
//   * pyra_fast_adaptive_residuals  - colour transform + per-row predictor
//     selection (seven candidates per row, scored by the modular L1
//     distance) in one pass over the tile.
//   * pyra_fast_adaptive_reconstruct - the inverse: every row is rebuilt
//     with the predictor it was encoded with, descaled and inverse
//     transformed.
//
// The encoder planner is *not* here: it has to measure candidates with the
// real range coder, and the coder lives in the Rust core (rust/pyra_core.rs,
// pyra_plan). The pure-Python reference planner mirrors it exactly.
//
// Every function is bit-exact with the pure-Python reference in
// pyra/predictors.py and pyra/color.py; the caller falls back to the Python
// reference when this library is missing.

#include <cstdint>
#include <cstdlib>
#include <algorithm>
#include <vector>

extern "C" {

static const int32_t CS_GRAY_V = 1;
static const int32_t CS_RGB_V = 2;
static const int32_t CS_RGBA_V = 3;

static const int32_t TRANSFORM_YCOCG_V = 0;
static const int32_t TRANSFORM_NONE_V = 1;

static const int FILTER_GLOBAL_V = 0;
static const int FILTER_ADAPTIVE_V = 1;

static const int PRED_CANDIDATE_COUNT = 7;

// ------------------------------------------------------------- primitives

static inline int64_t py_mod(int64_t x, int64_t m) {
    int64_t r = x % m;
    if (r < 0) r += m;
    return r;
}

static inline int64_t paeth_pred(int64_t a, int64_t b, int64_t c) {
    int64_t p = a + b - c;
    int64_t pa = p > a ? p - a : a - p;
    int64_t pb = p > b ? p - b : b - p;
    int64_t pc = p > c ? p - c : c - p;
    if (pa <= pb && pa <= pc) return a;
    if (pb <= pc) return b;
    return c;
}

static inline int64_t median_pred(int64_t a, int64_t b, int64_t c) {
    int64_t hi = a > b ? a : b;
    int64_t lo = a < b ? a : b;
    if (c >= hi) return lo;
    if (c <= lo) return hi;
    return a + b - c;
}

static inline int64_t pred_value(int pt, int64_t left, int64_t up, int64_t ul) {
    switch (pt) {
        case 0: return 0;                              // PRED_NONE
        case 1: return left;                           // PRED_LEFT
        case 2: return up;                             // PRED_UP
        case 3: return (left + up) >> 1;               // PRED_AVERAGE
        case 4: return paeth_pred(left, up, ul);       // PRED_PAETH
        case 5: return left + up - ul;                 // PRED_GRADIENT
        case 6: return median_pred(left, up, ul);      // PRED_MEDIAN
        default: return 0;
    }
}

static inline bool is_rgb_space(int32_t cs) {
    return cs == CS_RGB_V || cs == CS_RGBA_V;
}

// Colour transform of one row. TRANSFORM_NONE (and non-RGB spaces) copy.
static void transform_row(const int32_t* src, int32_t* dst, size_t n,
                          size_t ch, int32_t cs, int32_t transform) {
    if (transform == TRANSFORM_YCOCG_V && is_rgb_space(cs)) {
        size_t i = 0;
        for (; i + ch <= n; i += ch) {
            int64_t r = src[i + 0];
            int64_t g = src[i + 1];
            int64_t b = src[i + 2];
            int64_t co = r - b;
            int64_t t = b + (co >> 1);
            int64_t cg = g - t;
            int64_t y = t + (cg >> 1);
            dst[i + 0] = (int32_t)y;
            dst[i + 1] = (int32_t)co;
            dst[i + 2] = (int32_t)cg;
            if (ch == 4) dst[i + 3] = src[i + 3];
        }
        for (; i < n; i++) dst[i] = src[i];
    } else {
        for (size_t i = 0; i < n; i++) dst[i] = src[i];
    }
}

// Inverse colour transform of one row (values already descaled).
static void transform_row_inverse(const int32_t* src, int32_t* dst, size_t n,
                                  size_t ch, int32_t cs, int32_t transform) {
    if (transform == TRANSFORM_YCOCG_V && is_rgb_space(cs)) {
        size_t i = 0;
        for (; i + ch <= n; i += ch) {
            int64_t y = src[i + 0];
            int64_t co = src[i + 1];
            int64_t cg = src[i + 2];
            int64_t t = y - (cg >> 1);
            int64_t g = cg + t;
            int64_t b = t - (co >> 1);
            int64_t r = co + b;
            dst[i + 0] = (int32_t)r;
            dst[i + 1] = (int32_t)g;
            dst[i + 2] = (int32_t)b;
            if (ch == 4) dst[i + 3] = src[i + 3];
        }
        for (; i < n; i++) dst[i] = src[i];
    } else {
        for (size_t i = 0; i < n; i++) dst[i] = src[i];
    }
}

// Modular L1 distance of a residual from zero.
static inline int64_t mod_l1(int64_t r, int64_t modv) {
    int64_t d = modv - r;
    return r <= d ? r : d;
}

// Row filter table: 3 bits per row, MSB first (fields may straddle bytes).
static size_t pack_filter_bits(const uint8_t* preds, size_t rows, uint8_t* out) {
    uint32_t acc = 0;
    int nbits = 0;
    size_t o = 0;
    for (size_t i = 0; i < rows; i++) {
        acc = (acc << 3) | (uint32_t)(preds[i] & 7);
        nbits += 3;
        if (nbits >= 8) {
            out[o++] = (uint8_t)((acc >> (nbits - 8)) & 0xFF);
            nbits -= 8;
            acc &= (nbits == 0) ? 0u : ((1u << nbits) - 1u);
        }
    }
    if (nbits) {
        out[o++] = (uint8_t)((acc << (8 - nbits)) & 0xFF);
    }
    return o;
}

// Inverse of pack_filter_bits: read every row's 3-bit predictor id.
static void unpack_filter_bits(const uint8_t* data, size_t len, size_t rows,
                               uint8_t* out) {
    uint32_t acc = 0;
    int nbits = 0;
    size_t pos = 0;
    for (size_t i = 0; i < rows; i++) {
        while (nbits < 3) {
            uint8_t b = 0;
            if (pos < len) b = data[pos++];
            else pos++;
            acc = (acc << 8) | b;
            nbits += 8;
        }
        out[i] = (uint8_t)((acc >> (nbits - 3)) & 7);
        nbits -= 3;
        acc &= (nbits == 0) ? 0u : ((1u << nbits) - 1u);
    }
}

// ------------------------------------------------------ adaptive residuals

// Residuals + per-row predictor table for one tile.
// Returns 0 on success, negative on error.
int pyra_fast_adaptive_residuals(const int32_t* pixels, int count, int width,
                                  int height, int channels, int64_t offset,
                                  int64_t modv, int32_t color_space,
                                  int32_t transform, int32_t* out_res,
                                  uint8_t* out_bits, int32_t* detail) {
    if (!pixels || !out_res || !out_bits || width <= 0 || height <= 0 ||
        channels <= 0 || modv <= 0) {
        if (detail) *detail = -1;
        return -1;
    }
    const size_t rowlen = (size_t)width * (size_t)channels;
    if ((size_t)count != rowlen * (size_t)height) {
        if (detail) *detail = -2;
        return -1;
    }

    // `cur` holds the row being processed, `prev` the row above it.
    std::vector<int32_t> cur(rowlen), prev(rowlen), tmp(rowlen);
    std::vector<uint8_t> preds((size_t)height, 0);

    for (int y = 0; y < height; y++) {
        const int32_t* src_row = pixels + (size_t)y * rowlen;
        transform_row(src_row, tmp.data(), rowlen, (size_t)channels,
                      color_space, transform);
        if (offset != 0) {
            for (size_t i = 0; i < rowlen; i++) {
                tmp[i] = (int32_t)((int64_t)tmp[i] + offset);
            }
        }
        cur.swap(tmp);  // cur = row y, tmp = scratch

        const bool has_prev = y > 0;
        int best_pt = 0;
        int64_t best_score = -1;
        for (int pt = 0; pt < PRED_CANDIDATE_COUNT; pt++) {
            int64_t score = 0;
            for (size_t i = 0; i < rowlen; i++) {
                size_t x = i / (size_t)channels;
                int64_t left = x > 0 ? (int64_t)cur[i - channels] : 0;
                int64_t up = has_prev ? (int64_t)prev[i] : 0;
                int64_t ul = (has_prev && x > 0) ? (int64_t)prev[i - channels] : 0;
                int64_t pred = pred_value(pt, left, up, ul);
                int64_t r = py_mod((int64_t)cur[i] - pred, modv);
                score += mod_l1(r, modv);
            }
            if (best_score < 0 || score < best_score) {
                best_score = score;
                best_pt = pt;
            }
        }

        int32_t* res_row = out_res + (size_t)y * rowlen;
        for (size_t i = 0; i < rowlen; i++) {
            size_t x = i / (size_t)channels;
            int64_t left = x > 0 ? (int64_t)cur[i - channels] : 0;
            int64_t up = has_prev ? (int64_t)prev[i] : 0;
            int64_t ul = (has_prev && x > 0) ? (int64_t)prev[i - channels] : 0;
            int64_t pred = pred_value(best_pt, left, up, ul);
            res_row[i] = (int32_t)py_mod((int64_t)cur[i] - pred, modv);
        }
        preds[(size_t)y] = (uint8_t)best_pt;

        prev.swap(cur);  // prev = row y, cur = scratch
    }

    pack_filter_bits(preds.data(), (size_t)height, out_bits);
    if (detail) *detail = 0;
    return 0;
}

// --------------------------------------------------- adaptive reconstruct

int pyra_fast_adaptive_reconstruct(const int32_t* res, int count, int width,
                                   int height, int channels, int64_t offset,
                                   int64_t modv, int32_t color_space,
                                   int32_t transform, const uint8_t* filter_bits,
                                   int32_t* out_px) {
    if (!res || !out_px || !filter_bits || width <= 0 || height <= 0 ||
        channels <= 0 || modv <= 0) {
        return -1;
    }
    const size_t rowlen = (size_t)width * (size_t)channels;
    if ((size_t)count != rowlen * (size_t)height) {
        return -1;
    }

    std::vector<int32_t> rowbuf(rowlen), prevrow(rowlen), scaled(rowlen);
    std::vector<uint8_t> preds((size_t)height, 0);
    unpack_filter_bits(filter_bits, (size_t)((height * 3 + 7) / 8),
                       (size_t)height, preds.data());

    for (int y = 0; y < height; y++) {
        const int32_t* res_row = res + (size_t)y * rowlen;
        int pt = (int)preds[(size_t)y];
        const bool has_prev = y > 0;
        for (size_t i = 0; i < rowlen; i++) {
            size_t x = i / (size_t)channels;
            int64_t left = x > 0 ? (int64_t)rowbuf[i - channels] : 0;
            int64_t up = has_prev ? (int64_t)prevrow[i] : 0;
            int64_t ul = (has_prev && x > 0) ? (int64_t)prevrow[i - channels] : 0;
            int64_t pred = pred_value(pt, left, up, ul);
            rowbuf[i] = (int32_t)py_mod((int64_t)res_row[i] + pred, modv);
        }
        for (size_t i = 0; i < rowlen; i++) {
            scaled[i] = (int32_t)((int64_t)rowbuf[i] - offset);
        }
        transform_row_inverse(scaled.data(), out_px + (size_t)y * rowlen, rowlen,
                              (size_t)channels, color_space, transform);
        prevrow.swap(rowbuf);  // prevrow = row y, rowbuf = scratch
    }
    return 0;
}

}  // extern "C"
