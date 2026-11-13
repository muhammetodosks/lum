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
#include <cstring>
#include <cctype>
#include <cstdio>
#include <algorithm>
#include <string>
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

// ------------------------------------------------- medical preview (PYRM)

// Window/level mapping of one slice of a medical volume into 8 bit.
//
// The sample formats are the ones of the PYRM container: U8, U16, U32, I16,
// I32 and F32 (little endian). Signed samples are shifted into their signed
// range before the window is applied, which is what a radiologist expects:
// the level is given in the units of the modality (Hounsfield units for CT,
// for instance).
//
// `window` is the width of the window, `level` its centre. A window of 0 is
// treated as 1 (a flat window shows the level itself).

// Window/level preview of one slice of a medical volume.
//
// The sample formats are the ones of the PYRM container: U8, U16, U32, I16,
// I32 and F32 (little endian). Signed samples are shifted into their signed
// range before the window is applied, which is what a radiologist expects:
// the level is given in the units of the modality (Hounsfield units for CT,
// for instance).
//
// `window` is the width of the window, `level` its centre. A window of 0 is
// treated as 1 (a flat window shows the level itself).
//
// `slope` and `intercept` are the calibration of the modality
// (value = slope * stored + intercept); the window is applied in the
// calibrated domain. `invert` selects the MONOCHROME1 convention, where the
// low end of the range is white.
//
// The output has one byte per *pixel* (the channels are averaged), exactly
// like the pure-Python reference in pyra/medical/preview.py.

int pyra_med_preview(const uint8_t* samples, size_t len, int width, int height,
                     int depth, int channels, int sample_format, int slice,
                     double window, double level, double slope,
                     double intercept, int invert, uint8_t* out) {
    if (!samples || !out || width <= 0 || height <= 0 || depth <= 0 ||
        channels <= 0 || slice < 0 || slice >= depth) {
        return -1;
    }
    if (sample_format < 0 || sample_format > 5) {
        return -1;
    }
    static const int kSampleBytes[6] = {1, 2, 4, 2, 4, 4};
    static const int kSigned[6] = {0, 0, 0, 1, 1, 0};
    const int sb = kSampleBytes[sample_format];
    const size_t slice_samples = (size_t)width * (size_t)height * (size_t)channels;
    const size_t slice_bytes = slice_samples * (size_t)sb;
    if (len < slice_bytes * (size_t)(slice + 1)) {
        return -1;
    }
    const uint8_t* src = samples + slice_bytes * (size_t)slice;

    double lo = level - window * 0.5;
    double span = window > 0.0 ? window : 1.0;
    if (slope == 0.0) slope = 1.0;

    const size_t pixels = (size_t)width * (size_t)height;
    for (size_t i = 0; i < pixels; i++) {
        double sum = 0.0;
        for (int c = 0; c < channels; c++) {
            const uint8_t* p = src + (i * (size_t)channels + (size_t)c) * (size_t)sb;
            double value;
            if (sample_format == 5) {
                float f;
                std::memcpy(&f, p, 4);
                value = (double)f;
            } else {
                uint64_t raw = 0;
                for (int b = 0; b < sb; b++) raw |= (uint64_t)p[b] << (8 * b);
                if (kSigned[sample_format]) {
                    // shift the raw bit pattern into the signed range
                    int64_t sval = (int64_t)(raw << (64 - 8 * sb)) >>
                                   (64 - 8 * sb);
                    value = (double)sval;
                } else {
                    value = (double)raw;
                }
            }
            sum += value;
        }
        double value = (sum / (double)channels) * slope + intercept;
        double scaled = (value - lo) * 255.0 / span;
        if (scaled < 0.0) scaled = 0.0;
        if (scaled > 255.0) scaled = 255.0;
        int v = (int)scaled;   // truncation, exactly like the Python reference
        if (invert) v = 255 - v;
        out[i] = (uint8_t)v;
    }
    return 0;
}

// ------------------------------------------------------------- PNM helpers

// Read a binary PGM/PPM file (P5/P6) into a raw sample buffer.
//
// The header is parsed exactly like the Python reference in pyra/api.py:
// comments start with '#' and run to the end of the line, tokens may be
// spread over several lines. 16 bit samples stay big endian.
//
// Returns the number of bytes written into `out`, or a negative error code:
// -1 bad arguments, -2 unreadable file, -3 malformed header, -4 truncated
// data, -5 unsupported variant.

int pyra_pnm_read(const char* path, uint8_t* out, size_t out_cap,
                  int* info) {
    if (!path || !out || !info) return -1;
    FILE* f = std::fopen(path, "rb");
    if (!f) return -2;

    auto read_token = [&](std::string& tok) -> bool {
        tok.clear();
        int c;
        for (;;) {
            c = std::fgetc(f);
            if (c == EOF) return false;
            if (c == '#') {
                while (c != EOF && c != '\n') c = std::fgetc(f);
                continue;
            }
            if (!std::isspace(c)) break;
        }
        if (c == EOF) return false;
        do {
            tok.push_back((char)c);
            c = std::fgetc(f);
        } while (c != EOF && !std::isspace(c));
        return true;
    };

    std::string magic, t1, t2, t3;
    if (!read_token(magic) || magic != "P5" && magic != "P6") {
        std::fclose(f);
        return magic == "P2" || magic == "P3" ? -5 : -3;
    }
    if (!read_token(t1) || !read_token(t2) || !read_token(t3)) {
        std::fclose(f);
        return -3;
    }
    const long width = std::strtol(t1.c_str(), nullptr, 10);
    const long height = std::strtol(t2.c_str(), nullptr, 10);
    const long maxval = std::strtol(t3.c_str(), nullptr, 10);
    if (width <= 0 || height <= 0 || maxval <= 0 || maxval > 65535) {
        std::fclose(f);
        return -3;
    }
    const int channels = magic == "P5" ? 1 : 3;
    const int bit_depth = maxval > 255 ? 16 : 8;
    const size_t need = (size_t)width * (size_t)height * (size_t)channels *
                        (size_t)(bit_depth == 16 ? 2 : 1);
    if (need > out_cap) {
        std::fclose(f);
        return -1;
    }
    const size_t got = std::fread(out, 1, need, f);
    std::fclose(f);
    if (got != need) return -4;

    info[0] = channels;
    info[1] = (int)width;
    info[2] = (int)height;
    info[3] = bit_depth;
    info[4] = (int)maxval;
    info[5] = (int)need;
    return (int)need;
}

// Write a binary PGM/PPM file from a raw sample buffer.
//
// The buffer holds the samples exactly as they are stored in the file: 8 bit
// samples are written as they are, 16 bit samples are big endian (the PNM
// order), which is what `pyra_pnm_read` returns. Returns 0 on success, -1 on
// bad arguments, -2 on I/O error.

int pyra_pnm_write(const char* path, const uint8_t* data, int width, int height,
                   int channels, int bit_depth) {
    if (!path || !data || width <= 0 || height <= 0 || channels <= 0) {
        return -1;
    }
    if (bit_depth != 8 && bit_depth != 16) return -1;
    FILE* f = std::fopen(path, "wb");
    if (!f) return -2;
    const char* magic = channels == 1 ? "P5" : "P6";
    const unsigned maxval = bit_depth == 16 ? 65535u : 255u;
    std::fprintf(f, "%s\n%d %d\n%u\n", magic, width, height, maxval);
    const size_t samples = (size_t)width * (size_t)height * (size_t)channels;
    if (bit_depth == 8) {
        std::fwrite(data, 1, samples, f);
    } else {
        std::fwrite(data, 1, samples * 2, f);
    }
    const bool ok = std::fclose(f) == 0;
    return ok ? 0 : -2;
}

}  // extern "C"

