// PYRA Cloud (PYRC) C++ helper layer.
//
// The PYRC container itself is produced by the Rust core
// (rust/pyra_cloud.rs). This library covers the things that are *not* the
// container but belong to the cloud/earth-observation workflow:
//
//   * pyra_cloud_raw_read / pyra_cloud_raw_write
//       A tiny self describing raw cube container ("PYRX"): a 32 byte
//       header plus the samples of the cube in scene major, band planar
//       order. It is the interchange format between the sensor pipeline and
//       the codec.
//
//   * pyra_cloud_band_stats / pyra_cloud_histogram
//       Per band statistics over the whole cube: minimum, maximum, mean,
//       standard deviation and a 256 bin histogram.
//
//   * pyra_cloud_render_rgb
//       A false colour composite: three bands are windowed and mapped to
//       the red, green and blue channel of an 8 bit image.
//
//   * pyra_cloud_interleave / pyra_cloud_deinterleave
//       Band planar <-> pixel interleaved conversion.
//
// Everything is standard library only (C++20), no external dependencies, and
// the code never allocates more than the caller asked for.

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

extern "C" {

// ---------------------------------------------------------------- formats

static const int CLOUD_SFMT_U8 = 0;
static const int CLOUD_SFMT_U16 = 1;
static const int CLOUD_SFMT_U32 = 2;
static const int CLOUD_SFMT_I16 = 3;
static const int CLOUD_SFMT_I32 = 4;
static const int CLOUD_SFMT_F32 = 5;

static const size_t CLOUD_RAW_HEADER_SIZE = 32;
static const char CLOUD_RAW_MAGIC[4] = {'P', 'Y', 'R', 'X'};

static inline int cloud_sample_bytes(int sample_format) {
    switch (sample_format) {
        case CLOUD_SFMT_U8: return 1;
        case CLOUD_SFMT_U16:
        case CLOUD_SFMT_I16: return 2;
        case CLOUD_SFMT_U32:
        case CLOUD_SFMT_I32:
        case CLOUD_SFMT_F32: return 4;
        default: return 0;
    }
}

// ----------------------------------------------------------- sample access

// Read one sample as a double. Signed formats are sign extended, floats are
// converted from IEEE-754 binary32. The samples are little endian.
static inline double cloud_read_sample(const uint8_t* p, int sample_format) {
    switch (sample_format) {
        case CLOUD_SFMT_U8:
            return (double)p[0];
        case CLOUD_SFMT_U16: {
            uint16_t v = (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
            return (double)v;
        }
        case CLOUD_SFMT_U32: {
            uint32_t v = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
                         ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
            return (double)v;
        }
        case CLOUD_SFMT_I16: {
            int16_t v = (int16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
            return (double)v;
        }
        case CLOUD_SFMT_I32: {
            uint32_t raw = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
                           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
            int32_t v = (int32_t)raw;
            return (double)v;
        }
        case CLOUD_SFMT_F32: {
            uint32_t raw = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
                           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
            float f;
            std::memcpy(&f, &raw, 4);
            return (double)f;
        }
        default:
            return 0.0;
    }
}

// Offset of a sample inside the cube buffer.
static inline size_t cloud_offset(int width, int height, int bands, int scenes,
                                  int sample_bytes, int scene, int band,
                                  int row, int col) {
    const size_t plane = (size_t)width * (size_t)height;
    const size_t band_size = plane * (size_t)sample_bytes;
    const size_t scene_size = band_size * (size_t)bands;
    return (size_t)scene * scene_size + (size_t)band * band_size +
           ((size_t)row * (size_t)width + (size_t)col) * (size_t)sample_bytes;
}

// --------------------------------------------------------------- raw cube

// Write a raw cube file ("PYRX"). Returns 0 on success, -1 on bad
// arguments, -2 on an I/O error.
int pyra_cloud_raw_write(const char* path, const uint8_t* data, int width,
                         int height, int bands, int scenes, int sample_format) {
    if (!path || !data || width <= 0 || height <= 0 || bands <= 0 || scenes <= 0) {
        return -1;
    }
    const int sb = cloud_sample_bytes(sample_format);
    if (sb == 0) return -1;
    FILE* f = std::fopen(path, "wb");
    if (!f) return -2;
    uint8_t header[CLOUD_RAW_HEADER_SIZE];
    std::memset(header, 0, sizeof(header));
    std::memcpy(header, CLOUD_RAW_MAGIC, 4);
    header[4] = 1;                       // version
    header[5] = (uint8_t)sample_format;
    const uint32_t u_width = (uint32_t)width;
    const uint32_t u_height = (uint32_t)height;
    const uint32_t u_bands = (uint32_t)bands;
    const uint32_t u_scenes = (uint32_t)scenes;
    const uint32_t u_sb = (uint32_t)sb;
    std::memcpy(header + 8, &u_width, 4);
    std::memcpy(header + 12, &u_height, 4);
    std::memcpy(header + 16, &u_bands, 4);
    std::memcpy(header + 20, &u_scenes, 4);
    std::memcpy(header + 24, &u_sb, 4);
    bool ok = std::fwrite(header, 1, sizeof(header), f) == sizeof(header);
    if (ok) {
        const size_t total = (size_t)width * (size_t)height * (size_t)bands *
                             (size_t)scenes * (size_t)sb;
        ok = std::fwrite(data, 1, total, f) == total;
    }
    ok = (std::fclose(f) == 0) && ok;
    return ok ? 0 : -2;
}

// Read a raw cube file ("PYRX") into `out`.
//
// `info` receives: width, height, bands, scenes, sample_format,
// sample_bytes, data offset.
//
// Returns the number of data bytes written into `out`, or a negative error
// code: -1 bad arguments, -2 unreadable file, -3 malformed header,
// -4 truncated data, -5 the buffer is too small.
int pyra_cloud_raw_read(const char* path, uint8_t* out, size_t out_cap,
                        int* info) {
    if (!path || !out || !info) return -1;
    FILE* f = std::fopen(path, "rb");
    if (!f) return -2;
    uint8_t header[CLOUD_RAW_HEADER_SIZE];
    if (std::fread(header, 1, sizeof(header), f) != sizeof(header)) {
        std::fclose(f);
        return -4;
    }
    if (std::memcmp(header, CLOUD_RAW_MAGIC, 4) != 0) {
        std::fclose(f);
        return -3;
    }
    if (header[4] != 1) {
        std::fclose(f);
        return -3;
    }
    const int sample_format = header[5];
    const int sb = cloud_sample_bytes(sample_format);
    if (sb == 0) {
        std::fclose(f);
        return -3;
    }
    uint32_t width = 0, height = 0, bands = 0, scenes = 0, file_sb = 0;
    std::memcpy(&width, header + 8, 4);
    std::memcpy(&height, header + 12, 4);
    std::memcpy(&bands, header + 16, 4);
    std::memcpy(&scenes, header + 20, 4);
    std::memcpy(&file_sb, header + 24, 4);
    if (width == 0 || height == 0 || bands == 0 || scenes == 0 ||
        (int)file_sb != sb) {
        std::fclose(f);
        return -3;
    }
    const size_t total = (size_t)width * (size_t)height * (size_t)bands *
                         (size_t)scenes * (size_t)sb;
    if (total > out_cap) {
        std::fclose(f);
        return -5;
    }
    const size_t got = std::fread(out, 1, total, f);
    std::fclose(f);
    if (got != total) return -4;
    info[0] = (int)width;
    info[1] = (int)height;
    info[2] = (int)bands;
    info[3] = (int)scenes;
    info[4] = sample_format;
    info[5] = sb;
    info[6] = (int)CLOUD_RAW_HEADER_SIZE;
    info[7] = (int)total;
    return (int)total;
}

// ----------------------------------------------------------------- stats

// Statistics of one band over the whole cube.
struct PyraCloudBandStats {
    double minimum;
    double maximum;
    double mean;
    double stddev;
    int64_t count;
    uint64_t histogram[256];
};

// Compute the statistics of one band across every scene of the cube.
//
// Returns 0 on success, -1 on bad arguments, -2 when the buffer is too
// short for the described geometry.
int pyra_cloud_band_stats(const uint8_t* cube, size_t len, int width,
                           int height, int bands, int scenes, int sample_format,
                           int band, PyraCloudBandStats* out) {
    if (!cube || !out || width <= 0 || height <= 0 || bands <= 0 || scenes <= 0) {
        return -1;
    }
    if (band < 0 || band >= bands) return -1;
    const int sb = cloud_sample_bytes(sample_format);
    if (sb == 0) return -1;
    const size_t need = (size_t)width * (size_t)height * (size_t)bands *
                        (size_t)scenes * (size_t)sb;
    if (len < need) return -2;

    double lo = 0.0, hi = 0.0, mean = 0.0, m2 = 0.0;
    int64_t count = 0;
    for (int s = 0; s < scenes; s++) {
        for (int y = 0; y < height; y++) {
            for (int x = 0; x < width; x++) {
                const size_t off = cloud_offset(width, height, bands, scenes, sb,
                                               s, band, y, x);
                const double v = cloud_read_sample(cube + off, sample_format);
                if (count == 0) {
                    lo = hi = v;
                    mean = v;
                } else {
                    if (v < lo) lo = v;
                    if (v > hi) hi = v;
                    const double delta = v - mean;
                    mean += delta / (double)(count + 1);
                    m2 += delta * (v - mean);
                }
                count++;
            }
        }
    }

    out->minimum = lo;
    out->maximum = hi;
    out->mean = mean;
    out->stddev = count > 1 ? std::sqrt(m2 / (double)(count - 1)) : 0.0;
    out->count = count;
    std::memset(out->histogram, 0, sizeof(out->histogram));

    if (sample_format == CLOUD_SFMT_F32) {
        // A float has no natural 256 bin histogram; the value is normalised
        // into the observed range instead.
        const double span = hi - lo;
        for (int s = 0; s < scenes; s++) {
            for (int y = 0; y < height; y++) {
                for (int x = 0; x < width; x++) {
                    const size_t off = cloud_offset(width, height, bands, scenes,
                                                   sb, s, band, y, x);
                    const double v =
                        cloud_read_sample(cube + off, sample_format);
                    int bin = 0;
                    if (span > 0.0) {
                        bin = (int)((v - lo) * 255.0 / span);
                    }
                    if (bin < 0) bin = 0;
                    if (bin > 255) bin = 255;
                    out->histogram[bin]++;
                }
            }
        }
    } else {
        // The histogram uses the most significant byte of the canonical
        // (unsigned) sample value, which is the classic satellite product
        // histogram.
        const int bits = 8 * sb;
        const bool is_signed = (sample_format == CLOUD_SFMT_I16 ||
                                 sample_format == CLOUD_SFMT_I32);
        const uint32_t mask = (bits >= 32) ? 0xFFFFFFFFu : ((1u << bits) - 1u);
        const uint32_t half = (bits >= 32) ? 0x80000000u : (1u << (bits - 1));
        for (int s = 0; s < scenes; s++) {
            for (int y = 0; y < height; y++) {
                for (int x = 0; x < width; x++) {
                    const size_t off = cloud_offset(width, height, bands, scenes,
                                                   sb, s, band, y, x);
                    uint32_t raw = 0;
                    for (int k = 0; k < sb; k++) {
                        raw |= (uint32_t)cube[off + k] << (8 * k);
                    }
                    raw &= mask;
                    if (is_signed) {
                        // signed -> canonical: add half the range
                        raw = (raw + half) & mask;
                    }
                    const int shift = bits > 8 ? bits - 8 : 0;
                    const int bin = (int)((raw >> shift) & 0xFF);
                    out->histogram[bin]++;
                }
            }
        }
    }
    return 0;
}

// ------------------------------------------------------------- rendering

// Map a sample value into 0..255 with a window/level pair. A window of 0
// means "no scaling": the value is clamped into the byte range.
static inline uint8_t cloud_window(double value, double window,
                                    double level) {
    double scaled;
    if (window > 0.0) {
        const double low = level - window * 0.5;
        scaled = (value - low) * 255.0 / window;
    } else {
        scaled = value;
    }
    if (scaled < 0.0) scaled = 0.0;
    if (scaled > 255.0) scaled = 255.0;
    return (uint8_t)scaled;
}

// Render a false colour composite of one scene.
//
// `bands_r`, `bands_g` and `bands_b` are band indices (-1 disables a
// channel). Every channel has its own window/level pair.
//
// `out` receives width*height*3 bytes (RGB).
//
// Returns 0 on success, -1 on bad arguments, -2 when the buffer is too
// short.
int pyra_cloud_render_rgb(const uint8_t* cube, size_t len, int width,
                           int height, int bands, int scenes, int sample_format,
                           int scene, int band_r, int band_g, int band_b,
                           double window_r, double level_r, double window_g,
                           double level_g, double window_b, double level_b,
                           uint8_t* out) {
    if (!cube || !out || width <= 0 || height <= 0 || bands <= 0 || scenes <= 0) {
        return -1;
    }
    if (scene < 0 || scene >= scenes) return -1;
    const int idx[3] = {band_r, band_g, band_b};
    for (int c = 0; c < 3; c++) {
        if (idx[c] < -1 || idx[c] >= bands) return -1;
    }
    const int sb = cloud_sample_bytes(sample_format);
    if (sb == 0) return -1;
    const size_t need = (size_t)width * (size_t)height * (size_t)bands *
                        (size_t)scenes * (size_t)sb;
    if (len < need) return -2;

    const double window[3] = {window_r, window_g, window_b};
    const double level[3] = {level_r, level_g, level_b};
    for (int y = 0; y < height; y++) {
        for (int x = 0; x < width; x++) {
            for (int c = 0; c < 3; c++) {
                uint8_t v = 0;
                if (idx[c] >= 0) {
                    const size_t off = cloud_offset(width, height, bands, scenes,
                                                   sb, scene, idx[c], y, x);
                    v = cloud_window(
                        cloud_read_sample(cube + off, sample_format), window[c],
                        level[c]);
                }
                out[((size_t)y * (size_t)width + (size_t)x) * 3 + c] = v;
            }
        }
    }
    return 0;
}

// ------------------------------------------------------ interleave / planar

// Convert a band planar cube into a pixel interleaved image of one scene.
//
// `out` receives width*height*bands*sample_bytes bytes.
//
// Returns 0 on success, -1 on bad arguments, -2 when the buffer is too
// short.
int pyra_cloud_interleave(const uint8_t* cube, size_t len, int width,
                           int height, int bands, int scenes, int sample_format,
                           int scene, uint8_t* out) {
    if (!cube || !out || width <= 0 || height <= 0 || bands <= 0 || scenes <= 0) {
        return -1;
    }
    if (scene < 0 || scene >= scenes) return -1;
    const int sb = cloud_sample_bytes(sample_format);
    if (sb == 0) return -1;
    const size_t need = (size_t)width * (size_t)height * (size_t)bands *
                        (size_t)scenes * (size_t)sb;
    if (len < need) return -2;
    for (int y = 0; y < height; y++) {
        for (int x = 0; x < width; x++) {
            uint8_t* dst = out + (((size_t)y * (size_t)width + (size_t)x) *
                                  (size_t)bands * (size_t)sb);
            for (int b = 0; b < bands; b++) {
                const size_t off = cloud_offset(width, height, bands, scenes, sb,
                                               scene, b, y, x);
                std::memcpy(dst + (size_t)b * (size_t)sb, cube + off, (size_t)sb);
            }
        }
    }
    return 0;
}

// Inverse of pyra_cloud_interleave: pixel interleaved -> band planar.
int pyra_cloud_deinterleave(const uint8_t* interleaved, size_t len, int width,
                             int height, int bands, int sample_format,
                             uint8_t* out) {
    if (!interleaved || !out || width <= 0 || height <= 0 || bands <= 0) {
        return -1;
    }
    const int sb = cloud_sample_bytes(sample_format);
    if (sb == 0) return -1;
    const size_t need = (size_t)width * (size_t)height * (size_t)bands *
                        (size_t)sb;
    if (len < need) return -2;
    for (int y = 0; y < height; y++) {
        for (int x = 0; x < width; x++) {
            const uint8_t* src = interleaved + (((size_t)y * (size_t)width +
                                                 (size_t)x) * (size_t)bands *
                                                (size_t)sb);
            for (int b = 0; b < bands; b++) {
                // band planar: one plane after the other, row major inside
                uint8_t* dst = out + ((((size_t)b * (size_t)height + (size_t)y) *
                                       (size_t)width + (size_t)x) * (size_t)sb);
                std::memcpy(dst, src + (size_t)b * (size_t)sb, (size_t)sb);
            }
        }
    }
    return 0;
}

}  // extern "C"

#ifdef PYRA_CLOUD_SELFTEST
// ---------------------------------------------------------------- self test
//
// Compiled with -DPYRA_CLOUD_SELFTEST this file becomes a standalone test
// binary: `g++ -DPYRA_CLOUD_SELFTEST -std=c++20 -O2 -o cloud_test
// cpp/pyra_cloud.cpp && ./cloud_test`

#include <cstdio>

namespace {

int failures = 0;

void check(const char* name, bool ok) {
    std::printf("  %s %s\n", ok ? "PASS" : "FAIL", name);
    if (!ok) failures++;
}

}  // namespace

int main() {
    std::printf("=== PYRA cloud C++ self test ===\n");
    const int w = 5, h = 3, bands = 3, scenes = 2;
    const int fmt = CLOUD_SFMT_U16;
    const int sb = 2;
    std::vector<uint8_t> cube((size_t)w * h * bands * scenes * sb, 0);
    for (int s = 0; s < scenes; s++) {
        for (int b = 0; b < bands; b++) {
            for (int y = 0; y < h; y++) {
                for (int x = 0; x < w; x++) {
                    const size_t off = cloud_offset(w, h, bands, scenes, sb, s, b,
                                                   y, x);
                    const uint16_t v = (uint16_t)(s * 1000 + b * 100 + y * 10 + x);
                    cube[off] = (uint8_t)(v & 0xFF);
                    cube[off + 1] = (uint8_t)(v >> 8);
                }
            }
        }
    }

    // statistics
    PyraCloudBandStats stats{};
    check("band stats", pyra_cloud_band_stats(cube.data(), cube.size(), w, h,
                                               bands, scenes, fmt, 1,
                                               &stats) == 0);
    // band 1 covers every scene: values are s*1000 + 100 + y*10 + x
    check("band minimum", stats.minimum == 100.0);
    check("band maximum", stats.maximum == 1124.0);
    check("band count", stats.count == (int64_t)w * h * scenes);
    check("band mean", stats.mean > 611.9 && stats.mean < 612.1);
    int64_t total = 0;
    for (int i = 0; i < 256; i++) total += (int64_t)stats.histogram[i];
    check("histogram total", total == stats.count);

    // render
    std::vector<uint8_t> rgb((size_t)w * h * 3, 0);
    check("render", pyra_cloud_render_rgb(cube.data(), cube.size(), w, h, bands,
                                           scenes, fmt, 0, 0, 1, 2, 65535.0, 0.0,
                                           65535.0, 0.0, 65535.0, 0.0,
                                           rgb.data()) == 0);
    check("render size", rgb.size() == (size_t)w * h * 3);

    // interleave round trip
    std::vector<uint8_t> inter((size_t)w * h * bands * sb, 0);
    check("interleave", pyra_cloud_interleave(cube.data(), cube.size(), w, h,
                                              bands, scenes, fmt, 0,
                                              inter.data()) == 0);
    std::vector<uint8_t> back((size_t)w * h * bands * sb, 0);
    check("deinterleave", pyra_cloud_deinterleave(inter.data(), inter.size(), w,
                                                  h, bands, fmt,
                                                  back.data()) == 0);
    bool same = true;
    for (int y = 0; y < h && same; y++) {
        for (int x = 0; x < w && same; x++) {
            for (int b = 0; b < bands && same; b++) {
                const size_t src = cloud_offset(w, h, bands, scenes, sb, 0, b, y,
                                                x);
                // deinterleave writes band planar: plane b, row y, column x
                const size_t dst = (((size_t)b * h + (size_t)y) * w +
                                    (size_t)x) * sb;
                if (std::memcmp(cube.data() + src, back.data() + dst, sb) != 0) {
                    same = false;
                }
            }
        }
    }
    check("interleave round trip", same);

    // raw file round trip
    const char* path = "/tmp/pyra_cloud_selftest.pyrx";
    check("raw write", pyra_cloud_raw_write(path, cube.data(), w, h, bands,
                                             scenes, fmt) == 0);
    std::vector<uint8_t> loaded(cube.size() + 64, 0);
    int info[8] = {0};
    const int n = pyra_cloud_raw_read(path, loaded.data(), loaded.size(), info);
    check("raw read", n == (int)cube.size());
    check("raw geometry", info[0] == w && info[1] == h && info[2] == bands &&
                               info[3] == scenes && info[4] == fmt);
    check("raw payload", std::memcmp(cube.data(), loaded.data(), cube.size()) == 0);
    std::remove(path);

    std::printf("%s\n", failures == 0 ? "all cloud C++ checks passed"
                                      : "FAILURES PRESENT");
    return failures == 0 ? 0 : 1;
}
#endif
