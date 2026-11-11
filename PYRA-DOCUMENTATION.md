# PYRA — Tam Teknik Dokümantasyon

> **PYRA** — saf Python (stdlib-only) geri döndürülebilir (lossless) görüntü codec'i.
> Opsiyonel **Rust** çekirdeği (cdylib) + **C++20** hızlı yolu ile 40–75× hızlandırılmış,
> bit-exact (birebir aynı bayt) native/pure eşdeğerliğiyle.

| | |
|---|---|
| Sürüm | `0.2.0` (`pyra.__version__`) |
| Container formatı | **v2** (34 B header), v1 ile geriye uyumlu okuma |
| Kaynak dili | Python 3 (yalnızca stdlib) + Rust (std-only, kütüphane yok) + C++20 |
| Kayıpsızlık | **%100** — decode edilen pikseller bayt bayt aynı (CRC32 korumalı) |
| Toplam LOC | 7.071 (Rust 1.925 · testler 1.279 · Python ~3.300 · C++ 293) |
| Testler | native **245/245 PASS** · saf Python fallback **178/178 PASS** |
| Repozisyon | `/home/mami/pyra` (git, local-only — push yok) |
| HEAD | `d40acb7 v2 format: auto colour transform + auto row filter + per-row adaptive predictors` |

---

## İçindekiler

1. [Ne yaptığı ve neden](#1-ne-yaptığı-ve-neden)
2. [Hızlı başlangıç](#2-hızlı-başlangıç)
3. [Depo anatomisi](#3-depo-anatomisi)
4. [Dosya formatı (container) — v1 ve v2](#4-dosya-formatı-container--v1-ve-v2)
5. [Codec boru hattı (encode / decode)](#5-codec-boru-hattı-encode--decode)
6. [Renk transformu (YCoCg ↔ none)](#6-renk-transformu-ycocg--none)
7. [Tahmin ediciler (predictors)](#7-tahmin-ediciler-predictors)
8. [Satır-adaptif filtre (v2)](#8-satır-adaptif-filtre-v2)
9. [Entropi kodlayıcı (range coder)](#9-entropi-kodlayıcı-range-coder)
10. [Sıkıştırma metotları (RAW / RLE / LZ / MIX)](#10-sıkıştırma-metotları-raw--rle--lz--mix)
11. [Otomatik planlama (v2)](#11-otomatik-planlama-v2)
12. [Native mimari: Rust çekirdek](#12-native-mimari-rust-çekirdek)
13. [Native mimari: C++ hızlı yol](#13-native-mimari-c-hızlı-yol)
14. [Zero-copy canvas pipeline](#14-zero-copy-canvas-pipeline)
15. [Dispatch ve saf-Python fallback](#15-dispatch-ve-saf-python-fallback)
16. [Test paketi ve eşdeğerlik garantileri](#16-test-paketi-ve-eşdeğerlik-garantileri)
17. [Performans: hız benchmarkları](#17-performans-hız-benchmarkları)
18. [Performans: sıkıştırma oranı](#18-performans-sıkıştırma-oranı)
19. [Optimizasyon tarihi (ne denendi, ne kazanıldı)](#19-optimizasyon-tarihi-ne-denendi-ne-kazanıldı)
20. [Güvenlik modeli](#20-güvenlik-modeli)
21. [Python API referansı](#21-python-api-referansı)
22. [CLI referansı](#22-cli-referansı)
23. [Derleme ve geliştirme iş akışı](#23-derleme-ve-geliştirme-iş-akışı)
24. [Sınırlar ve gelecek işler](#24-sınırlar-ve-gelecek-işler)
25. [Terim sözlüğü](#25-terim-sözlüğü)

---

## 1. Ne yaptığı ve neden

PYRA, **tamamen kayıpsız** (reversible) bir görüntü codec'idir. Amacı iki şeyi aynı anda yapmak:

1. **Saf Python ile tamamen çalışabilmek.** Tek bağımlılık yok — `zlib`, `struct`, `ctypes` dışında
   Python standart kütüphanesinin dışına çıkmaz. `rustc` ya da `g++` yoksa her şey yine çalışır.
2. **Aynı baytları çok daha hızlı üretmek.** Sıkıştırmanın sıcak yolları (entropi kodlayıcı, RLE,
   LZ77, piksel tahmini, renk transformu) Rust ve C++'ye taşındı; Python tarafı sadece
   dosya/başlık yönetimi ve son `array → list` dönüşümünde kaldı.

Tasarımın temel kuralı:

> **Native hızlandırma asla çıktıyı değiştiremez.**
> Native yol ile saf Python yol aynı girdide **birebir aynı container baytlarını** üretir.

Bu kural, 2.520 farklı parametre kombinasyonunda otomatik olarak test edilir (§16).

### Kısa karşılaştırma

| | PNG (PIL, L9) | PYRA v2 | Not |
|---|---|---|---|
| 5 gerçek fotoğraf toplamı (640 px) | 928.511 B (3.85×) | **923.328 B (3.87×)** | PYRA %0,6 daha küçük |
| Bulutlar (640 px) | 238.846 B | **210.553 B** | PYRA %11,8 daha küçük |
| Monument Valley | 142.754 B | 166.910 B | PNG %16,9 daha küçük |

PNG kayıpsızdır ve iyi bir referanstır; PYRA onun **hızını 40–75× katına** çıkarır ve
bazı görsellerde onu geçer, bazılarında geride kalır. PNG'nin sıkıştırma algoritması
(DEFLATE, sabit Huffman/LZ77) üzerine kurulu olduğu için çok farklı içeriklerde örüntü
yakalama kapasitesi değişkendir.

Kayıplı referanslar (karşılaştırma için): JPEG q90 ≈ 17–30×, WebP q90 ≈ 30–229×.

---

## 2. Hızlı başlangıç

### 2.1 Derleme (opsiyonel ama önerilir)

```bash
cd /home/mami/pyra
bash scripts/build_native.sh
# -> _libpyra_core.so   (Rust, 4.4 MB)
# -> _libpyra_fast.so   (C++20, 20 KB)   [g++ varsa]
```

`build_native.sh` Rust'u **her zaman** derler; C++ modülünü yalnızca `g++` bulunduğunda
derler. Derleme başarısız olursa betik hata vermez — codec saf Python yoluna düşer.

### 2.2 CLI

```bash
cd /home/mami                        # paket burada import edilebilir
python3 -m pyra encode input.ppm output.pyra
python3 -m pyra decode output.pyra decoded.ppm
python3 -m pyra inspect output.pyra
python3 -m pyra verify  output.pyra
python3 -m pyra benchmark input.ppm
```

Seçenekler:

```bash
python3 -m pyra encode in.ppm out.pyra \
    --method    auto|raw|rle|lz|mix \
    --predictor auto|none|left|up|average|paeth|gradient|median \
    --transform auto|ycocg|none \
    --filter    auto|global|adaptive \
    --tile 256 \
    --colorspace auto|gray|rgb|rgba
```

> `--predictor` için varsayılan **"auto"**'dur (v0.1'de "paeth" idi).
> `auto` modunda transform, filtre ve predictor **görüntüye göre otomatik seçilir**.

### 2.3 Python API

```python
from pyra import encode, decode, encode_file, decode_file

blob = encode(pixels, width=640, height=480, bit_depth=8,
              color_space=2,            # CS_RGB
              pred_type=-1,            # -1 = auto
              transform=-1,            # -1 = auto
              filter_mode=-1,          # -1 = auto
              tile_size=256,
              method=-1)               # -1 = auto (RAW/RLE/LZ arasından seç)
meta, out_pixels = decode(blob)
assert out_pixels == pixels          # kayıpsız — bit-exact
```

Dosya üzerinde çalışmak:

```python
meta = encode_file("in.ppm", "out.pyra", color_space=-1, pred_type=-1)
meta = decode_file("out.pyra", "back.ppm")
```

### 2.4 Testleri çalıştırma

```bash
cd /home/mami                          # paket kökünün ÜSTÜ — "No module named pyra" hatasına dikkat
timeout 900 python3 -m pyra.tests.test_lossless                        # native
PYRA_DISABLE_NATIVE=1 timeout 900 python3 -m pyra.tests.test_lossless  # saf Python
```

Beklenen çıktı: `245 passed, 0 failed` (native) ve `178 passed, 0 failed` (fallback).

> **Tuzak:** `python3 -m pyra.tests.test_lossless` komutu `/home/mami/pyra` içinden
> çalıştırılırsa `No module named 'pyra'` hatası verir — cwd `/home/mami` olmalı.

---

## 3. Depo anatomisi

Paket dizini **aynı zamanda repo köküdür** (`/home/mami/pyra`).

```
pyra/
├── __init__.py           34    public API dışa aktarımları
├── __main__.py            4    python -m pyra girişi
├── constants.py          57    MAGIC, VERSION, CS_*, PRED_*, METHOD_*, TRANSFORM_*, FILTER_*, hata sınıfları
├── security.py           23    boyut/chunk/dosya doğrulama (decompression bomb koruması)
├── tile.py               36    Tile, split_into_tiles, assemble_from_tiles
├── color.py              94    YCoCg ileri/ters dönüşüm + none transform
├── predictors.py        185    7 tahmin edici + satır-adaptif seçim + filtre bit paketleme
├── entropy.py           254    FenwickCDF, RangeEncoder, RangeDecoder (referans, saf Python)
├── entropycost.py        44    entropi maliyeti yardımcıları (proxy — planlayıcıda artık kullanılmıyor)
├── compress.py          294    pack_residuals, RLE, LZ77 encode/decode (native dispatch)
├── container.py         145    PyraContainer: v1/v2 header, tile index, CRC32
├── codec.py             737    PyraCodec: encode/decode, planlama, tile adayları, native/fused dispatch
├── api.py               168    encode/decode/encode_file/decode_file/decode_region/iter_tiles/encode_animation
├── cli.py               209    argparse CLI
├── native.py            939    Rust çekirdeğin ctypes yükleyicisi + 25 sarmalayıcı
├── fast.py              143    C++ çekirdeğin ctypes yükleyicisi (2 sarmalayıcı)
├── rust/pyra_core.rs   1925    std-only cdylib — 24 dışa aktarılmış fonksiyon
├── cpp/pyra_fast.cpp    293    C++20 çekirdek — 2 dışa aktarılmış fonksiyon
├── scripts/build_native.sh 19  Rust + C++ derleme betiği
├── tests/test_lossless.py 1279 245 (native) / 178 (fallback) kontrol
├── pngbench.py          189    PNG/JPEG/WebP karşılaştırma aracı
└── README.md                  kullanıcı dokümantasyonu
```

**Katmanlar:**

```
        ┌─────────────────────────────────────────────┐
        │  api.py / cli.py                             │  genel API, dosya I/O, PNM
        ├─────────────────────────────────────────────┤
        │  codec.py — PyraCodec                        │  planlama, tile adayları, method seçimi
        ├──────────────┬──────────────────────────────┤
        │              │                              │
        ▼              ▼                              ▼
  container.py    predictors.py                   color.py
  (v1/v2 hdr)   (7 predictor + adaptive)      (YCoCg / none)
                       └──────────────┬───────────────┘
                                      ▼
                                 compress.py
                      (pack / RLE / LZ77 token üretimi)
                                      ▼
                                 entropy.py
                      (Fenwick CDF + range coder)
                                      ▼
      ┌───────────────────────────────────────────────┐
      │  native.py (Rust)  ·  fast.py (C++)            │  ← ctypes, None-dönüşlü fallback
      └───────────────────────────────────────────────┘
```

---

## 4. Dosya formatı (container) — v1 ve v2

### 4.1 Genel yerleşim

```
┌──────────────────────────────────────────────────────────┐
│ Header (v2: 34 B, v1: 32 B)                              │
├──────────────────────────────────────────────────────────┤
│ index_size : 4 B (uint32 BE)                            │
├──────────────────────────────────────────────────────────┤
│ Tile index : num_tiles × 8 B  (offset:u32, length:u32) │
├──────────────────────────────────────────────────────────┤
│ Tile verileri (sıralı, bitstream)                        │
├──────────────────────────────────────────────────────────┤
│ CRC32 : 4 B (uint32 BE) — header + index + tiles üzerinden│
└──────────────────────────────────────────────────────────┘
```

### 4.2 v2 header alanları

Format dizesi: `">8sIIIBBBBIIBB"` — **34 bayt**, big-endian.

| Ofset | Boyut | Alan | Açıklama |
|---|---|---|---|
| 0 | 8 | `magic` | `PYRA\r\n\x1A\n` |
| 8 | 4 | `version` | `2` |
| 12 | 4 | `width` | piksel genişliği (u32) |
| 16 | 4 | `height` | piksel yüksekliği (u32) |
| 20 | 1 | `bit_depth` | 8 veya 16 |
| 21 | 1 | `color_space` | 1=GRAY, 2=RGB, 3=RGBA |
| 22 | 1 | `pred_type` | 0..6 (bkz. §7) — global modda kullanılır |
| 23 | 1 | `method` | 0=RAW, 1=RLE, 2=LZ, 3=MIX |
| 24 | 4 | `tile_size` | tile kenar uzunluğu (varsayılan 256) |
| 28 | 4 | `num_tiles` | tile sayısı |
| 32 | 1 | `transform` | 0=YCOCG, 1=NONE |
| 33 | 1 | `filter_mode` | 0=GLOBAL, 1=ADAPTIVE |

### 4.3 v1 header (geriye uyum)

Format dizesi: `">8sIIIBBBBII"` — **32 bayt**. v1'de `transform` ve `filter_mode`
byte'ları **yoktur**. Okurken:

- `transform` → `TRANSFORM_YCOCG` (0)
- `filter_mode` → `FILTER_GLOBAL` (0)

yani eski dosyalar, v1'in varsayılan davranışıyla (YCoCg + global PAETH) çözülür.

### 4.4 Tile gövdesi düzeni (v2)

```
Tile body = [ method byte ] [ row filter table ] [ entropy stream ]
              (yalnız MIX)      (yalnız ADAPTIVE)
```

| Bölüm | Koşul | Uzunluk |
|---|---|---|
| method byte | `method == METHOD_MIX` | 1 B (0/1/2) |
| row filter table | `filter_mode == FILTER_ADAPTIVE` | `ceil(tile_height * 3 / 8)` B |
| entropy stream | her zaman | değişken |

Filtre tablosu **MSB-first** akümülatörle paketlenir; 3 bit'lik alanlar byte sınırını
aşabilir, eksik byte'lar 0 olarak doldurulur (bu davranış `pack_filter_bits` ile
`unpack_filter_bits` arasında simetriktir).

### 4.5 Bütünlük ve güvenlik

- **CRC32** (`zlib.crc32`) header + index + tile verisinin tamamı üzerinden hesaplanır,
  dosyanın son 4 baytında saklanır. Uyuşmazlık → `CorruptedFileError`.
- Magic, version, `transform`, `filter_mode`, `method` alanları doğrulanır.
- `validate_dimensions` → genişlik/yükseklik ≤ 65536, toplam piksel ≤ 268M
  (decompression bomb koruması), chunk ≤ 512 MB.

---

## 5. Codec boru hattı (encode / decode)

### 5.1 Encode

```
piksel listesi (w×h×ch)
   │
   ├─ 1. TILE BÖLME ──────────────── split_into_tiles()   (varsayılan 256×256)
   │
   ├─ 2. RENK TRANSFORMU ─────────── auto: YCoCg | none          (§6, §11)
   │
   ├─ 3. KANONİKLEŞTİRME ─────────── + offset (mod/2), mod = alfabe boyutu (§6)
   │
   ├─ 4. TAHMİN ──────────────────── global: 1 predictor / adaptive: satır başına (§7, §8)
   │
   ├─ 5. PACK ────────────────────── residual → little-endian packed bytes (§10)
   │
   ├─ 6. METOT ───────────────────── RAW | RLE | LZ   (MIX: tile başına en küçük) (§10)
   │
   ├─ 7. ENTROPİ KODLAMA ─────────── adaptive Fenwick CDF range coder (§9)
   │
   └─ 8. CONTAINER ───────────────── header + index + tiles + CRC32 (§4)
```

### 5.2 Decode

Tüm adımlar tersine döner; tile'lar ayrı ayrı çözülür ve `assemble_from_tiles()`
ile kanvasa geri yerleştirilir. Decode sonunda `array('i') → list` dönüşümü yapılır
(bu, Python tarafındaki tek önemli maliyettir).

### 5.3 Sıralama özeti

- **Predictor uygulaması**: `left` = satırın kendi solu, `up`/`upleft` = **önceki satır**,
  `upright` = önceki satırın sağı. Kanonikleştirme sonrası değerler üzerinde çalışılır.
- **Reconstruction**: satır önceki satırdan yeniden kurulur, sonra offset düşülür,
  sonra ters renk transformu uygulanır.
- `py_mod(x, m) = ((x % m) + m) % m` — Python'un negatif modül semantiği, Rust'ta
  birebir korunur (`let r = x % m; if r < 0 { r + m }`).

---

## 6. Renk transformu (YCoCg ↔ none)

### 6.1 YCoCg (varsayılan seçenek, `TRANSFORM_YCOCG = 0`)

Klasik, geri döndürülebilir YCoCg dönüşümü:

```
Y  = ( R + 2*G + B)          >> 2
Co = ( R - B )               >> 1
Cg = ( B - G )               >> 1
```

Tersi:

```
G  = Y - Cg
B  = G + Co
R  = G + Co
```

Alpha kanalı **dokunulmaz**.

**Artısı:** kanallar arası dekorelasyon → komşu pikseller arası daha yüksek korelasyon.
**Eksisi:** residual alfabe genişler (`1 << (bit_depth+3)`).

### 6.2 none (`TRANSFORM_NONE = 1`)

Kanal dönüşümü **yok** — pikseller olduğu gibi tahmin edilir.

- Residual alfabe `1 << bit_depth` (RGB'de bile 8-bit → 256 sembol).
- Offset uygulanmaz (`_canonical_offset` = 0).
- GRAY her zaman bu moddadır.

### 6.3 Neden "none" çoğu zaman kazanıyor

Prototip ölçümleri (`/tmp/opencode/proto_ratio.py`, saf Python):

| Görsel | YCoCg + PAETH | none + en iyi predictor | Kazanç |
|---|---|---|---|
| Clouds | 388.188 B | **374.094 B** (median) | %3,6 |
| Monument | 266.339 B | **252.890 B** (paeth) | %5,1 |
| Numbat | 202.782 B | **175.786 B** (average) | %15,4 |

Sebep: dar alfabe, **entropi kodlayıcının** sembol dağılımını daha verimli örnekler
(küçük frekanslarda daha iyi aralık kodlaması). YCoCg'nin kazandırdığı dekorelasyon
bu avantajı her görselde karşılamıyor.

Bu yüzden v2'de **transform otomatik seçilir** (§11).

---

## 7. Tahmin ediciler (predictors)

`predictors.py` — 7 aday, `PRED_NONE=0 … PRED_MEDIAN=6`:

| ID | Ad | Formül (kanonik satır `cur`, önceki satır `prev`) |
|---|---|---|
| 0 | `none` | `0` |
| 1 | `left` | `cur[x-1]` |
| 2 | `up` | `prev[x]` |
| 3 | `average` | `(left + up) >> 1` |
| 4 | `paeth` | Paeth predictor (a=left, b=up, c=upleft) |
| 5 | `gradient` | `left + up - upleft` |
| 6 | `median` | `median(left, up, upleft)` |

Residual = `(gerçek − tahmin) mod mod`, `mod = 1 << bit_depth` (none transform)
veya `1 << (bit_depth+3)` (YCoCg, RGB/RGBA).

Python referansı: `predictors.py: apply_predictor_row(row, prev, pred_type, channels, mod)`.
Rust karşılığı: `pyra_apply_predictor_row` / `pyra_reconstruct_row` (7 tipin tamamı,
`((x%m)+m)%m` semantiğiyle birebir).

---

## 8. Satır-adaptif filtre (v2, `FILTER_ADAPTIVE = 1`)

Her satır, 7 adayın **modüler L1 skoruna** göre kendi predictor'ını seçer:

```
skor = Σ  min(residual, mod − residual)      # modüler "mutlak fark"
```

- Düşük skor = daha iyi tahmin (daha küçük residual → daha iyi entropi).
- **Tie-break**: eşitlikte **ilk aday** (liste sırası: none, left, up, average, paeth, gradient, median) kazanır —
  bu, Python ↔ Rust ↔ C++ eşdeğerliğini garantiler.
- Seçim tablosu tile gövdesinde **3 bit/satır** olarak saklanır.

Ölçülen kazanç: global seçime göre **%0,4–1,5** (Clouds 374.094 → 370.052 B);
transform otomatik seçimiyle birleştiğinde **%10**'a kadar (Numbat 202.782 → 182.811 B).

---

## 9. Entropi kodlayıcı (range coder)

`entropy.py` (referans) ↔ `rust/pyra_core.rs` (`Fenwick`, `encode_symbols`, `RangeDec`).

### 9.1 Adaptif CDF (Fenwick ağacı)

```
n_symbols   = alfabe boyutu
freq[s]      = başlangıçta 1
total        = n_symbols
cap          = max(1 << 15, 2 * n_symbols)
size         = n_symbols'in üstündeki en küçük 2^k
tree[size+1] = Fenwick ağacı
```

- `add(symbol)`: frekansı artırır, ağacı günceller; `total > cap` ise **halving**:
  `freq = [(f+1) >> 1]`, sonra ağaç yeniden kurulur.
- `cum(symbol)`: sembolün **altındaki** sembollerin toplamı (prefix sum).
- `find(target)`: ikili arama; **clamp dalı** `pos >= n` ise `(n-1, cum(n-1), freq[n-1])`
  döner — Python referansıyla birebir (ilk yazımda `cum(n)` idi, düzeltildi).

### 9.2 Range encoder

```
low = 0, high = 2^32 − 1 (inclusive)
span = high − low + 1
r    = max(1, span // total)
cum  = cdf.cum(symbol); f = cdf.freq[symbol]
new_low  = low + r * cum
new_high = max(new_low, low + r * (cum + f) − 1)
```

**Renormalizasyon (3 dal):**

| Koşul | İşlem |
|---|---|
| E1: `high < 2^31` | `0` biti + `pending` adet `1` biti yaz; `low <<= 1`, `high = (high<<1)|1` |
| E2: `low >= 2^31` | `1` biti + `pending` adet `0` biti yaz; `low = (low−2^31)<<1`, `high = ((high−2^31)<<1)|1` |
| E3: `low >= 2^30 && high < 3*2^30` | `pending += 1`; `low = (low−2^30)<<1`, `high = ((high−2^30)<<1)|1` |

`flush()`: `pending += 1`; sonra `low < 2^30` ise `0` + pending×`1`, aksi halde `1` + pending×`0`.
Bitler **MSB-first** byte'a paketlenir, son bayt sıfırla doldurulur.

### 9.3 Range decoder

`low/high` aynı; `value` ilk 32 bit, `pos` bit konumu.
`decode_symbol()` sırası **Python referansıyla birebir**:

```
total = cdf.total
r     = max(1, (high − low + 1) // total)
target = (value − low) // r        → 0'ya veya total−1'e kırpılır
sym, cum, f = cdf.find(target)
low  = low + r*cum
high = low + r*f − 1
renormalize (E1/E2/E3; value de doldurulur)
cdf.add(sym)
```

Stream sonrası okunan bitler **0** olarak doldurulur (Python ile aynı).

### 9.4 Doğrulama

`/tmp/opencode/chk_range.py` — 60 rastgele deneme (n_symbols ∈ {2,5,258,1000,65536},
n ∈ {0,1,7,100,5000,60000}): **encode byte-identical**, **decode birebir**, 0 hata.

---

## 10. Sıkıştırma metotları (RAW / RLE / LZ / MIX)

| Method | Akış | Ne zaman iyi |
|---|---|---|
| `RAW` (0) | residual sembolleri doğrudan entropi kodlayıcıya | gürültülü / rastgele veri |
| `RLE` (1) | residual → packed bytes → RLE → byte literal token'ları → entropi | düz bölgeler, uzun aynı değerler |
| `LZ` (2) | residual → packed bytes → RLE → LZ77 token'ları → entropi | tekrarlayan desenler, fotoğraf |
| `MIX` (3) | tile başına RAW/RLE/LZ'den **en küçüğü** (+1 byte method tag) | heterojen görüntüler |

### 10.1 Pack / unpack

Residual'ler `mod` alfabesine göre **little-endian**, `width = ceil(bit_length(mod)/8)` bayta
paketlenir. Unpack sırasında `value >= mod` ise `CorruptedFileError`.

Rust: `pyra_pack_residuals` / `pyra_unpack_residuals` (ikisi de `array('I')` buffer ile,
zero-copy).

### 10.2 RLE

Format: `count byte` + `value byte`; `count == 0xFF` kaçışı yoktur — bunun yerine
**kaçış değeri** kullanılır (0x00 kaçışı, `0x00 0xNN` = NN kez 0x00). Ayrıntılar:
`compress.py: rle_encode / rle_decode`, hata mesajları birebir korunmuştur
("truncated RLE stream", "RLE run overshoots target length", ...).

Rust: `pyra_rle_encode` / `pyra_rle_decode`; run doldurma `fill()` ile hızlandırıldı.

### 10.3 LZ77

Sabitler: `MINMATCH=4`, `MAXMATCH=255`, `MAXDIST=65535`, `TOKEN_MATCH=256`,
`TOKEN_EOF=257`, `TOKEN_SYMBOLS=258`.

- **Encoder**: greedy longest-match, 3 baytlık hash anahtarı, kova başına en fazla
  32 konum, FIFO eviction. Adaylar **newest-first** taranır; eşitlikte **daha yeni**
  kazanır (`L > best_len` kuralı). Python referansıyla **token listesi birebir aynı**
  (321.949 token doğrulandı).
- **Decoder**: streaming (sembol tembel çekilir) — Python'da kalır; Rust tarafında
  hem `pyra_lz_decode_tokens` (batch) hem de fused `pyra_decode_lz_method` vardır.
- **Doğrulama**: `/tmp/opencode/chk_lz.py` — 9 elle seçilmiş vaka + 300 rastgele fuzz
  (overlap, run, block dahil) → **tümü PASS**.

### 10.4 Fused method decoder'ları (Rust)

Tek bir range-decoder oturumunda token akışı doğrudan RLE/LZ genişletmesine ve
residual unpack'a akar:

- `pyra_decode_rle_method` — token limiti `2*packed_len + 8`; aşarsa
  "RLE token stream exceeds length limit".
- `pyra_decode_lz_method` — 4 baytlık big-endian RLE uzunluk başlığı + LZ token akışı.

Hata durumunda Python wrapper'ları `None` döner → çağıran **saf Python referans yoluna**
düşer ve referans hata mesajını birebir üretir.

---

## 11. Otomatik planlama (v2)

`PyraCodec._plan_encoding` — encode başında **tek seferlik** bir karar:

| Karar | Seçenekler | Varsayılan |
|---|---|---|
| `transform` | YCoCg, none | `auto` |
| `filter_mode` | global, adaptive | `auto` |
| `pred_type` | 7 aday | `auto` |

### 11.1 Maliyet ölçümü: gerçek coder, entropi proxy'si değil

İlk denemede **ideal code length** (`Σ count_i · ceil(log2(total/count_i))`) kullanıldı.
Bu proxy **sistematik olarak yanlış sıralıyordu**:

- Rainbow'da proxy `p1 (left)`'i seçiyordu; gerçekte `p6 (median)` **%6 daha küçük** dosya üretiyordu.
- Clouds'ta `p1` vs `p6`, Fuji'de `p3` vs `p1` farkları oluştu.

Nedeni: ideal code length, gerçek **adaptif** kodlayıcının frekans tabanlı davranışını
(halving politikası dahil) yansıtmaz ve YCoCg'ye doğru biçimde biased'tır.

**Çözüm:** maliyet, **gerçek range coder'ın ürettiği bayt sayısıyla** ölçülür —
8 örnek satır gerçekten kodlanır, `len(flush())` alınır.

### 11.2 Örnekleme: yayılmış satırlar

```python
idx[i] = min(h - 1, i * h // n)      # n = _PLAN_SAMPLE_ROWS = 8
```

İlk N satır temsilci değildir (ör. Rainbow'un üstü boş/karanlıktı). Satırlar görüntüye
**yayılınca** transform seçimi her zaman doğru çıktı.

Ayrıca her örneklenen satır için **"up" bağlamı gerçek satır `y-1`** olmalıdır;
aksi halde proxy sistematik olarak `p1`'i seçiyordu.

### 11.3 N=8 yeterli mi?

Evet — 5 gerçek fotoğrafta N=8 ile alınan planlar N=16 ile **birebir aynı** (aynı
transform/filter/pred ve aynı bayt boyutları). Saf Python planlayıcı 640 px'de ~2,5 s
sürerken N=8 ile ~1,2 s'ye indi; native planlayıcı ~70 ms.

### 11.4 Kararın tekrarlanabilirliği

Planlayıcı **native (Rust) ve saf Python** yollarında **birebir aynı kararı** vermek
zorundadır; aksi halde container baytları farklılaşır ve parite testleri kırılır.
Rust planlayıcı (`pyra_plan`) ve Python referansı (`_plan_cost` + `RangeEncoder`):
**108 denemede 0 hata** (`/tmp/opencode/chk_plan.py`).

### 11.5 Seçim tablosu (gerçek fotoğraflar, 640 px)

| Görsel | v1 (YCoCg+PAETH) | v2 auto | Seçilen plan |
|---|---|---|---|
| Clouds | 226.227 B | **210.553 B** | (none, adaptive) |
| Monument | 178.485 B | **166.910 B** | (none, global, paeth) |
| Fuji-san | 219.158 B | **204.653 B** | (none, adaptive) |
| Rainbow | 281.869 B | **244.797 B** | (none, adaptive) |
| Numbat | 109.295 B | **96.415 B** | (none, adaptive) |

→ Ortalama **%9 daha küçük dosya**.

---

## 12. Native mimari: Rust çekirdek

`rust/pyra_core.rs` — **std-only** (`rustc --edition 2021 -O --crate-type cdylib`),
dış kütüphane yok, 24 dışa aktarılmış `extern "C"` fonksiyon.

### 12.1 Fonksiyon listesi

| Fonksiyon | İşlev |
|---|---|
| `pyra_pack_residuals` | residual → packed bayt |
| `pyra_unpack_residuals` | packed bayt → residual |
| `pyra_rle_encode` / `pyra_rle_decode` | RLE |
| `pyra_lz_encode` | LZ77 token üretimi (hash tablosu ile) |
| `pyra_lz_decode_tokens` | batch LZ77 çözme |
| `pyra_transform_forward` / `pyra_transform_inverse` | YCoCg (ve none) |
| `pyra_apply_predictor_row` / `pyra_reconstruct_row` | 7 predictor satır çekirdeği |
| `pyra_pipe_forward` | transform → kanonikleştir → satır tahmini (tek dönüşüm) |
| `pyra_pipe_reconstruct` | satır rekonstrüksiyon → descale → ters transform |
| `pyra_range_encode` / `pyra_range_decode` | **entropi kodlayıcının kendisi** |
| `pyra_decode_rle_method` / `pyra_decode_lz_method` | fused per-method decoder |
| `pyra_extract_tile` | kanvastan tile kopyalama |
| `pyra_pipe_forward_adaptive_into` | adaptive satır seçimi + residual + filtre tablosu |
| `pyra_pipe_reconstruct_adaptive_into` | filtre tablosuyla canvas'a rekonstrüksiyon |
| `pyra_pipe_reconstruct_into` | canvas'a (row-strided) doğrudan yazma |
| `pyra_encode_raw_method` / `pyra_encode_rle_method` / `pyra_encode_lz_method` | method başına entropi kodlama |
| `pyra_plan` | **v2 planlayıcı** (transform/filter/pred kararı) |

### 12.2 Hata kodları

| Kod | Anlam |
|---|---|
| `-1 … -5` | RLE: truncation, escape truncation, overshoot, run length 0, length mismatch |
| `-20 … -28` | LZ: EOF yok, sembol sınırı, literal/match aşımı, bozuk eklenti, geçersiz mesafe |
| `-40 … -45` | model: sembol sayısı, encoder buffer, encoder sembol, LZ başlık sembolü |
| `-50 … -55` | fused decoder: sembol>255, token limiti, implausible uzunluk, unpack aralığı, LZ sembol/mesafe |
| `-100` | pack: residual aralık dışı |
| `-110 / -111` | unpack: truncation / aralık |
| `-200` | predictor: kanal sayısı hatası |
| `-300` | pipe: şekil (shape) uyuşmazlığı |

### 12.3 Yükleyici (`native.py`)

- `ctypes.CDLL(pkg/"_libpyra_core.so")`; `OSError` → `None` (sessiz fallback).
- Tüm argtypes/restype'ler `_load()` içinde kaydedilir.
- **`_view(buf)`**: `array('i')` → zero-copy `ctypes.c_int32*n.from_buffer(buf)`.
- **`_u32_view(buf)`**: `array('I')` → `c_uint32` view.
- Pointer kaydırma için `ctypes.cast(ctypes.addressof(view) + off*4, POINTER(c_int32))`
  kullanılır — `ctypes.byref(view, offset)` **POINTER argtype'ıyla çalışmaz**.

> **Tuzak:** `array('I', [])` → `buffer_info()[0] == 0` (null). Rust tarafında
> `std::slice::from_raw_parts(null, 0)` UB'dir → `count == 0` guard'ları zorunlu.

---

## 13. Native mimari: C++ hızlı yol

`cpp/pyra_fast.cpp` (C++20, `g++ -O3 -std=c++20 -shared -fPIC`) — 2 dışa aktarılmış fonksiyon:

| Fonksiyon | İşlev |
|---|---|
| `pyra_fast_adaptive_residuals` | transform + satır-bazlı 7 aday predictor L1 seçimi → residual + filtre tablosu |
| `pyra_fast_adaptive_reconstruct` | satır filtreleriyle rekonstrüksiyon + descale + ters transform |

Yükleyici: `fast.py` (`ctypes`, `fast.available()`).

**Kapsam:** C++ modülü **sadece saf Python yolunda** devreye girer (native modda Rust'ın
adaptive pipeline'ı kullanılır). Amaç, `PYRA_DISABLE_NATIVE=1` fallback'inin de hızlı
kalması.

**Planlayıcı C++'ta değil:** C++'ta range coder yok; tek planlayıcı Rust'ta (`pyra_plan`).

> **Tuzak (düzeltildi):** C++'ta maske ataması `acc = (1<<nbits)-1` değil
> `acc &= (1<<nbits)-1` olmalı — aksi halde filtre tablosu bozuk (geçersiz predictor id) çıkıyordu.

---

## 14. Zero-copy canvas pipeline

Python liste dönüşümleri ctypes maliyetinin ~%85'ini yiyordu. Çözüm: piksel kanvası
tek bir `array('i')` buffer'ında yaşar ve native her şeyi doğrudan o buffer'a yazar.

### 14.1 Encode

```python
canvas = array('i', pixels)                       # tek kopya
for each tile:
    native.extract_tile(canvas, stride, x, y, tw, th, channels, out_buf)
    native.pipe_forward_into(out_buf, ...)         # transform + tahmin → residual
    native.pack_residuals_buf(res, ...)            # packed bayt
    native.encode_lz_method(packed, ...)           # entropi kodlama
```

### 14.2 Decode

```python
canvas = array('i', bytes(4 * w * h * ch))        # sıfırla
for each tile:
    native.decode_lz_method_into(data, count, mod, res_buf)
    native.pipe_reconstruct_into(res, ..., dst_offset=(ty*w+tx)*ch, dst_stride=w*ch)
pixels = canvas.tolist()                          # tek dönüşüm
```

### 14.3 Etkisi (mikro-benchmark, 256×256×3 tile)

| Aşama | Önce | Sonra |
|---|---|---|
| `list(out)` (ctypes → Python list) | **31 ms** | 0 ms (zero-copy) |
| Rust `pyra_pipe_reconstruct` | 2,6 ms | 2,6 ms (değişmedi) |

Sonuç: decode hızı **25× → 65×** aralığına çıktı.

### 14.4 Dikkat: encoder regresyonu

İlk sürüm her tile için **3 aday** (RAW/RLE/LZ) hesaplıyordu → RAW encode 17,9 ms → 47,5 ms
regresyon. Düzeltme: `_encode_native` yalnızca MIX/-1 modunda 3 aday hesaplar,
aksi halde sadece istenen metodu (`wanted = (method,)`).

---

## 15. Dispatch ve saf-Python fallback

### 15.1 Sözleşme

Her native wrapper **hata/uygunluk durumunda `None` döner** (veya `False`).
Çağıran kod:

```python
result = _native.foo(...)
if result is None:
    result = pure_python_foo(...)      # referans yol
```

Sonuç: native olmadığında da **aynı exception sınıfları ve mesajları** üretilir.

### 15.2 Ortam bayrakları (call-time okunur)

| Değişken | Etki |
|---|---|
| `PYRA_DISABLE_NATIVE=1` | native **ve** C++ backend'i kapatır (saf Python) |
| `PYRA_NATIVE=0` | aynı etki |

Bayraklar **import time'da değil, call time'da** okunur — bu sayede test paketi her iki
modda da tam konteyner yeniden kodlayıp bayt eşitliğini doğrulayabilir.

> **Tuzak (ölçüm):** `PYRA_DISABLE_NATIVE=1` set edilmişken "native" çalıştırmak anında
> `None` döner → **sahte hız** üretir. Ölçümden önce env'i pop edip
> `native.available()`'ı assert etmek gerekir.

### 15.3 Kapsam matrisi

| Aşama | Native | C++ | Saf Python |
|---|---|---|---|
| pack / unpack residual | ✅ Rust | — | ✅ |
| RLE encode/decode | ✅ Rust | — | ✅ |
| LZ encode | ✅ Rust | — | ✅ |
| LZ decode | ✅ Rust (batch + fused) | — | ✅ (streaming) |
| Range coder | ✅ Rust | — | ✅ |
| Predictor satırları | ✅ Rust | — | ✅ |
| Renk transformu | ✅ Rust | — | ✅ |
| Adaptive satır filtresi | ✅ Rust | ✅ C++ | ✅ |
| Otomatik planlama | ✅ Rust | — | ✅ |

---

## 16. Test paketi ve eşdeğerlik garantileri

`tests/test_lossless.py` — **1.279 satır**.

### 16.1 Sonuçlar

| Mod | Sonuç |
|---|---|
| Native (`_libpyra_core.so` + `_libpyra_fast.so` yüklü) | **245 passed, 0 failed** (rc=0) |
| Saf Python (`PYRA_DISABLE_NATIVE=1`) | **178 passed, 0 failed** (rc=0) |

> Kontrol sayısı farkı beklenen bir durumdur: native-mod-only kontroller (Rust ↔ Python
> eşdeğerliği) fallback modunda atlanır. Kaynak-doğruluk: native 245, fallback 178.

### 16.2 Kapsanan test grupları

| Grup | İçerik |
|---|---|
| `test_compress_methods` | RLE roundtrip + 150 rastgele bozuk stream; LZ roundtrip + **9 bozuk-kaynak vakası**; pack/unpack 4 mod genişliği; forced-method tile roundtrip (6 içerik × 3 metot); global method seçimi; container doğrulama; height=300 roundtrip; **forced-LZ container bozulması → 80 rastgele bozma** (sızıntı = 0) |
| `test_native_backend` | predictor eşdeğerliği (7 pred × 3 kanal, `prev=None` dahil); YCoCg eşdeğerliği + roundtrip; RGBA container; **fusion parite** (`pipe_residuals`/`pipe_reconstruct`, her cs) |
| `test_range_coder` | native encoder vs Python encoder **byte-identical** (6 alfabe × 6 uzunluk); native decoder vs Python decoder **sembol-aynı**; fused method decoder paritesi (cs × bd × method); **container byte paritesi** (cs × method); bozuk stream dayanıklılığı (120 bit flip); LZ "implausible length" koruması |
| `test_buffer_pipeline` | 270 kombinasyon container byte + piksel parite; çoklu tile canvas ofseti; `extract_tile` dikdörtgen; `pipe_reconstruct_into` canvas yazımı; method encoder/decoder paritesi |
| `test_v2_format` | v2 roundtrip (3 cs × 2 bd × 4 içerik × 2 transform × 2 filter × 5 method); auto plan determinizmi + native==pure byte paritesi; transform'un stream'i değiştirmesi; **v1 legacy container decode**; geçersiz transform byte → `CorruptedFileError` |

### 16.3 Kapsamlı parite taraması

`/tmp/opencode/chk_v2.py` — **2.520 kombinasyon**:

```
3 colorspace × 2 bit_depth × 4 içerik × 7 plan modu × 5 method × 3 boyut
```

Her kombinasyonda: native encode baytları vs saf Python encode baytları, native decode
pikselleri vs saf Python decode pikselleri, roundtrip, meta karşılaştırması.

**Sonuç: `TESTS: 2520  FAILS: 0`** — native ve saf Python birebir aynı container
baytlarını üretiyor.

### 16.4 Sıkı hata eşlemeleri

| Durum | Hata |
|---|---|
| Pack range dışı | `ValueError("residual symbol out of range at index N")` |
| Unpack range dışı | `CorruptedFileError` (index bazlı) |
| CRC uyuşmazlığı | `CorruptedFileError("CRC32 mismatch: stored 0x..., calculated 0x...")` |
| Geçersiz magic | `CorruptedFileError("Invalid magic number: not a PYRA file")` |
| Desteklenmeyen version | `CorruptedFileError("unsupported PYRA version: N")` |
| Geçersiz transform/filter | `CorruptedFileError("invalid color transform in header: N")` |
| RLE token aşımı | `CorruptedFileError("RLE token stream exceeds length limit")` |
| LZ implausible uzunluk | `CorruptedFileError("implausible RLE stream length N")` |

---

## 17. Performans: hız benchmarkları

**Yöntem:** 256×256 tile'lar, `native` ve `PYRA_DISABLE_NATIVE=1` (saf Python) ayrı
ayrı ölçülür, best-of-3 alınır. Ölçümde `native.available()` assert edilir
(sahte-hız tuzağı).

### 17.1 v2 (auto plan) — 256²

| Yol | Encode | Decode |
|---|---|---|
| 256² GRAY + RAW | 32,0× | 60,6× |
| 256² GRAY + RLE | 42,2× | 72,9× |
| 256² GRAY + LZ | 58,3× | 70,6× |
| 256² RGB + RAW | 36,2× | 54,8× |
| 256² RGB + RLE | 38,8× | 68,2× |
| 256² RGB + LZ | 57,5× | 71,8× |
| 256² RGBA + RAW | 35,5× | 61,5× |
| 256² RGBA + RLE | 40,1× | 73,4× |
| 256² RGBA + LZ | 61,3× | 74,8× |
| 512² RGB + LZ | 61,7× | 71,5× |

**Gürültü benzeri girdi (en kötü durum):** encode 40–45×, decode 59–71×.

### 17.2 v1 karşılaştırması (önceki sürüm)

| Yol | Encode | Decode |
|---|---|---|
| 256² GRAY + LZ | 66,3× | 65,3× |
| 256² RGB + LZ | 61,9× | 67,5× |
| 256² RGB + RLE | 41,6× | 68,2× |
| 512² RGB + LZ | 62,8× | 68,1× |

v2, plannın ek maliyetine rağmen encode hızında **%10–20 kayıp** göstermektedir
(planlayıcı ~70 ms). Bu, ~%9 daha küçük dosyalarla dengelenmiştir.

### 17.3 Profil (256×256×3 foto tile)

| Aşama | Süre |
|---|---|
| `extract_tile` | 0,05 ms |
| `pipe_forward_into` | 4,1 ms |
| `pack_residuals_buf` | 1,2 ms |
| `rle_encode_bytes` | 1,8 ms |
| `encode_raw_method` | 19,6 ms |
| `encode_rle_method` | 33,5 ms |
| `encode_lz_method` | 70,8 ms (LZ encode ~52 ms + coder ~16 ms) |
| `decode_lz_method_into` | 39,6 ms |
| `pipe_reconstruct_into` | 2,4 ms |
| `array.tolist()` | 1,0 ms |

**Darboğaz artık entropi kodlayıcıdır** (~%99). Saf Python planlayıcı ise ~2.477 ms
(64→8 satır örneklemesiyle ~1,2 s'ye indi).

---

## 18. Performans: sıkıştırma oranı

**Yöntem:** 640 px (aspect korunmuş), 8-bit RGB, PIL 10.2.0. Decode sonrası piksel
eşitliği her dosyada assert ile doğrulanır.

| Görsel | Ham | PYRA v1 | **PYRA v2** | PNG (L9) | v2 vs PNG |
|---|---|---|---|---|---|
| Clouds | 691.200 B | 226.227 B (3,06×) | **210.553 B (3,28×)** | 238.846 B (2,89×) | **−11,8%** |
| Monument Valley | 687.360 B | 178.485 B (3,85×) | **166.910 B (4,12×)** | 142.754 B (4,81×) | +16,9% |
| Fuji-san | 689.280 B | 219.158 B (3,15×) | **204.653 B (3,37×)** | 215.432 B (3,20×) | −5,0% |
| Rainbow | 817.920 B | 281.869 B (2,90×) | **244.797 B (3,34×)** | 240.207 B (3,41×) | +1,9% |
| Numbat | 691.200 B | 109.295 B (6,32×) | **96.415 B (7,17×)** | 91.272 B (7,57×) | +5,6% |
| **TOPLAM** | 3.576.960 B | 1.015.034 B (3,52×) | **923.328 B (3,87×)** | 928.511 B (3,85×) | **−0,6%** |

**Kaynak görseller:** `/usr/share/backgrounds/` (Clouds_by_Tibor_Mokanszki.jpg,
Monument_valley_by_orbitelambda.jpg, Fuji_san_by_amaral.png,
Rainbow_lightbulb_by_Daniel_Micallef.png, Numbat_wallpaper_dimmed_3480x2160.png).

### 18.1 Sentetik içerik (512²)

| İçerik | Oran |
|---|---|
| düz renk | 2427× |
| gradyan | 648× |
| foto benzeri | 1,44× |
| gürültü | 0,88× (gürültüde **%12 büyüme** — beklenen, veri sıkıştırılamaz) |

### 18.2 Kayıpsız referanslar

| Format | Oran (kayıplı) |
|---|---|
| JPEG q90 | 17–30× |
| WebP q90 | 30–229× |

---

## 19. Optimizasyon tarihi (ne denendi, ne kazanıldı)

10 commit, `feff9db` → `d40acb7`.

| Commit | İçerik | Kazanç |
|---|---|---|
| `8480cbc` | Rust native çekirdek + ctypes dispatch + saf fallback | encode 2,6× / decode ~1,0× |
| `6e8b344` | Fused pipeline (transform+tahmin / rekonstrüksiyon+tersYCoCg) | encode ~2,7× |
| `3ac202e` | **Native range coder** + fused per-method decoder'lar | decode 15–27× |
| `530acf1` | **Zero-copy buffer pipeline** (canvas gidiş-dönüş) | decode 25× → 65× |
| `e34433d` | Branchless renormalizasyon + integer-hash LZ tablosu | encode_raw −%7, encode_lz −%10 |
| `12fe6f2` | 8 baytlık LZ match karşılaştırma + desen genişletme | encode_lz −%9 |
| `fee92e0` | Benchmark + profil notları (doküman) | — |
| `d40acb7` | **v2 format**: auto transform + auto filter + adaptive predictor | dosya **−%9** |

### 19.1 Ölçülüp **reddedilen** optimizasyonlar

Bunlar uygulandı, A/B ile ölçüldü ve **performans kaybı** yüzünden geri alındı
(doğruluk kaybı değil — hepsi bit-exact kalmıştı):

| Deneme | Sonuç | Sebep |
|---|---|---|
| Branchless Fenwick `find` (2×size zero-padded tree) | decode_rle **+%26** | Gerçek veride branch predictor iyi çalışıyor; padded tree cache baskısı |
| `fastdiv` (f64 bölme) | nötr | Riski karşılığında kazanç yok |
| `BitWriter` (bit biriktirme) | nötr | — |
| Inline `add` / sabit tur sayılı `cum` | nötr | — |
| Flat open-addressing LZ tablosu | encode_lz **+%12** | next/prev dizilerinde rastgele cache miss |
| E1 burst toplu işleme | encode_raw **+%17** | E1/E2 dönüşümlü çalışıyor → burstlar 1–2 adım |
| 64-bit pencere bit okuyucu | decode_rle **+%3** | Maliyet bit okumada değil, low/high/value bağımlılık zincirinde |

### 19.2 Profil bulguları (kalıcı bilgi)

- Range decoder (200K sembol, rdtsc): `find` ≈ 70 gerçek cyc/sym, `add` ≈ 22,
  `renorm` ≈ 87.
- Renormalizasyon: **5,6 iterasyon/sembol**, dağılım **E1 %41,7 · E2 %41,8 · E3 %16,5**.
- Model (cum + add) tek başına yalnızca 17–21 ns/sembol → **model darboğaz değil**;
  maliyet `find` + `renorm` içinde.
- LZ encoder: ortalama kova uzunluğu 1,64 → match araması ucuz; SipHash maliyeti
  (~20 ns/lookup) asıl darboğazdı → integer hash ile çözüldü.

### 19.3 Denenmemiş, riskli kalan başlık

**İki kademeli CDF modeli** (Fenwick yerine block-prefix + within-block prefix dizileri):
decoder `find` ≈ 70 → ~30 cyc, encoder `cum` O(1). Tahmini %15–20 kazanç. **Risk:**
büyük alfabelerde (RAW 16-bit → 524.288 sembol) blok güncellemeleri patlıyor;
iki kademeli tarama `find`'i O(1) yapmıyor. Exactness koşulu: frekans/cum/halving
politikası ve `find` clamp semantiği (`cum(n-1)`) birebir korunmalı.

---

## 20. Güvenlik modeli

### 20.1 Giriş doğrulama (`security.py`)

| Kontrol | Limit |
|---|---|
| `MAX_WIDTH` / `MAX_HEIGHT` | 65.536 |
| `MAX_PIXELS` | 16.384 × 16.384 = 268M piksel (decompression bomb koruması) |
| `MAX_CHUNK_SIZE` | 512 MB |
| `MAX_RAM_USAGE` | 1 GB |

### 20.2 Bozuk dosya dayanıklılığı

- CRC32 kontrolü **decode'dan önce** yapılır → rastgele bozulma erken yakalanır.
- Tile offset/length doğrulaması (dosya sınırları içinde mi).
- Entropi token akışı için **uzunluk limitleri**:
  - RLE: `2*packed_len + 8` token
  - LZ: `2*packed_len + 64` bayt
  - LZ başlığı `0xFFFFFFFF` ise "implausible RLE stream length" hatası.
- LZ match mesafesi çıktı uzunluğundan büyükse reddedilir.
- Tüm bu durumlar `CorruptedFileError` üretir — `IndexError`/`ValueError` sızmaz
  (test paketi bunu ayrıca doğrular: "başka exception YASAK").

### 20.3 Kayıp veri yok

Kayıpsız codec'te "hata toleransı" yoktur; herhangi bir bit bozulması CRC ile yakalanır.
Kayıpsız "gürültü toleransı" isteniyorsa PNG/JPEG/WebP kullanılmalıdır.

---

## 21. Python API referansı

### 21.1 Çekirdek

```python
def encode(pixels, width, height, bit_depth=8, color_space=CS_RGB,
           pred_type=PRED_PAETH, tile_size=256, method=-1,
           transform=-1, filter_mode=-1) -> bytes
def decode(file_bytes) -> (meta: dict, pixels: list[int])
```

`-1` değerleri **"otomatik seç"** anlamına gelir (transform, filter_mode, method).
`pred_type=-1` → en iyi global predictor.

### 21.2 Dosya seviyesi

```python
def encode_file(input_path, output_path, color_space=-1, pred_type=PRED_PAETH,
                tile_size=256, method=-1, transform=-1, filter_mode=-1) -> dict
def decode_file(input_path, output_path) -> dict
```

Giriş PNM (P4/P5/P6), çıktı PNM.

### 21.3 Bölge ve tile erişimi

```python
def decode_region(file_bytes, rx, ry, rw, rh) -> (meta, pixels)
def iter_tiles(file_bytes) -> Generator[(meta, pixels)]
```

`decode_region` **yalnızca o bölgenin tile'larını** çözer — tüm görseli çözmez.

### 21.4 Animasyon

```python
def encode_animation(frames, width, height, fps=30, **kwargs) -> bytes
```

### 21.5 Sabitler

```python
CS_GRAY=1, CS_RGB=2, CS_RGBA=3
PRED_NONE=0, PRED_LEFT=1, PRED_UP=2, PRED_AVERAGE=3,
PRED_PAETH=4, PRED_GRADIENT=5, PRED_MEDIAN=6
METHOD_RAW=0, METHOD_RLE=1, METHOD_LZ=2, METHOD_MIX=3
TRANSFORM_YCOCG=0, TRANSFORM_NONE=1
FILTER_GLOBAL=0, FILTER_ADAPTIVE=1
VERSION=2, VERSION_LEGACY=1
```

### 21.6 Hatalar

```python
PyraError              # taban
├── SecurityError       # limit aşımı, decompression bomb
└── CorruptedFileError  # CRC, magic, version, biçim hataları
```

---

## 22. CLI referansı

```
python3 -m pyra {encode,decode,inspect,verify,benchmark} ...
```

| Komut | Açıklama |
|---|---|
| `encode in.ppm out.pyra` | PNM → PYRA |
| `decode in.pyra out.ppm` | PYRA → PNM |
| `inspect in.pyra` | container metadata + tile sayısı |
| `verify in.pyra` | magic/CRC bütünlük kontrolü |
| `benchmark in.ppm` | encode/decode süreleri + PNG boyut karşılaştırması |

Ortak seçenekler: `--method`, `--predictor`, `--transform`, `--filter`, `--tile`,
`--colorspace`.

Gerçek `inspect` çıktısı:

```
=== PYRA Image Inspection: t.pyra ===
  version: 2
  dimensions: 320x240 @ 8-bit
  color_space: rgb
  predictor: median
  transform: none
  filter: global
  method: raw
  tile_size: 256
  num_tiles: 2
  file_size: 71730 bytes
```

`encode`/`decode` ilerleme çıktısı:

```
Encoding t.ppm -> t.pyra...
  size: 71730 bytes, method: raw, transform: none, filter: global, tiles: 2
Done.
```

---

## 23. Derleme ve geliştirme iş akışı

### 23.1 Derleme

```bash
cd /home/mami/pyra
bash scripts/build_native.sh
```

Betik: `rustc --edition 2021 -O --crate-type cdylib -o _libpyra_core.so rust/pyra_core.rs`
ve (varsa) `g++ -O3 -std=c++20 -shared -fPIC -o _libpyra_fast.so cpp/pyra_fast.cpp`.

`.so` dosyaları `.gitignore`'dadır — takip edilmez.

### 23.2 Test döngüsü

```bash
cd /home/mami
timeout 900 python3 -m pyra.tests.test_lossless                       # native
PYRA_DISABLE_NATIVE=1 timeout 900 python3 -m pyra.tests.test_lossless # fallback
```

### 23.3 Doğrulama araçları (`/tmp/opencode/`)

| Betik | Amaç |
|---|---|
| `chk_v2.py` | 2.520 kombinasyon native/pure byte + piksel paritesi |
| `chk_range.py` | 60 deneme range coder byte/sembol paritesi |
| `chk_lz.py` | LZ encode/decode fuzz paritesi (9 vaka + 300 rastgele) |
| `chk_plan.py` | 108 deneme planlayıcı karar paritesi |
| `chk_buf.py` | 270 kombinasyon buffer pipeline paritesi |
| `chk_cpp.py` | C++ adaptive residual/reconstruct paritesi |
| `bench_rc.py` | native vs saf Python end-to-end benchmark |
| `bench_v2.py` | v2 format benchmark |
| `ratio_v5.py` | gerçek fotoğraf oran karşılaştırması (PNG/JPEG/WebP) |
| `ab.py` | interleaved A/B ölçüm (iki `.so` karşılaştırma) |
| `prof_native.py` / `prof_dec.py` | aşama bazlı profil |

### 23.4 Ölçüm tuzakları (sık yaşanan)

1. **Sentetik standalone micro-benchmark gerçek pipeline'ı yansıtmaz.** Mutlaka `ab.py`
   ile gerçek profille interleaved A/B yap.
2. **LLVM, boolean select'i geriye dönük branch'e çevirebilir**; mask tabanlı aritmetik
   kullanmak tek başına yetmez — hedef mimari ölçülerek doğrulanmalı.
3. **Paylaşımlı/aşırı yavaşlayan makinelerde** mutlak ms değerleri 3× bozulabilir; sadece
   göreli (interleaved) karşılaştırma güvenilirdir.
4. `PYRA_DISABLE_NATIVE=1` set edilmişken "native" çalıştırmak sahte hız üretir.
5. Rust'ta `from_raw_parts(null, 0)` → UB; `count == 0` guard'ları şart.
6. `include!` ile Rust dosyası eklenirken dosyadaki `#![allow(...)]` inner attribute'i
   derlemeyi kırıyor → `sed` ile silinmeli.
7. Python referansı kısmi pikselde `IndexError` atar (RGB'de uzunluk 3'ün, RGBA'da 4'ün
   katı olmalı); Rust kalanını kopyalar — bu, belgelenmiş native-only farktır.

---

## 24. Sınırlar ve gelecek işler

### Mevcut sınırlar

| Sınır | Detay |
|---|---|
| Animasyon | `encode_animation` var ama container'da animation bloğu yok (basit kapsülleme) |
| Renk derinliği | 8 ve 16 bit. 1/2/4 bit gri tonlama yok |
| Yükseklik | v2 header'da `height` u32; pratik sınır 65536 (güvenlik limiti) |
| PNG yazma | `pngbench.py` karşılaştırma aracı; PNG **okuma** yok (kendi formatı) |
| Multi-frame | tek kare |
| Filtre seçimi | satır-bazlı; 2D (blok) filtreler yok |
| Sıkıştırma | tek geçişli (context modeli yok, ileri referans yok) |

### Gelecek işler (öncelik sırasıyla)

1. **İki kademeli CDF modeli** — decoder `find` maliyetini düşürme (§19.3).
2. **PNG/WebP benzeri öngörü modelleri** (byte-oriented, "filter" + "context modeling")
   → tipik PNG'den %5–15 daha iyi oran mümkün.
3. **Blok bazlı (2D) filtre adaptasyonu** — satır yerine 8×8 blok.
4. **Animasyon için frame-to-frame delta** — video benzeri verimlilik.
5. **Paralel tile işleme** — `multiprocessing` ile tile başına paralel encode.
6. **Yerleşik (embedded) decoder** — Rust cdylib'i tek başına bir C kütüphanesi olarak
   sunmak (PYRA → `pyra_codec.h`).
7. **CLI'ye `--tile` optimizasyonu** — farklı tile boyutları için oran/hız deneyimi.

---

## 25. Terim sözlüğü

| Terim | Anlam |
|---|---|
| **Residual** | Gerçek piksel değerinden tahmin edilen değerin çıkarılmasıyla kalan fark (modulo alfabe) |
| **Canonicalization** | Residual'ları sabit aralığa (mod) sığdırma; offset ekleme/çıkarma |
| **Alphabe (mod)** | Residual sembollerinin alabileceği değer sayısı; `1 << bit_depth` (none) veya `1 << (bit_depth+3)` (YCoCg) |
| **Entropy coder** | Sembol frekanslarına göre değişken bit uzunluğu atayan kodlayıcı (burada: range coder) |
| **CDF** | Cumulative Distribution — sembollerin kümülatif frekansı (Fenwick ağacında tutulur) |
| **Halving** | Uzun akışlarda frekans tablosunun `freq = (freq+1)>>1` ile küçültülmesi |
| **Renormalizasyon** | Range coder'ın aralığı (low/high) normalize edip bit çıkarması (E1/E2/E3) |
| **Pack** | Residual'leri sabit genişlikte bayta paketleme (little-endian) |
| **RLE** | Run-length encoding — tekrar eden baytları (sayı, değer) ile sıkıştırma |
| **LZ77** | Geri referanslı sıkıştırma (mesafe + uzunluk) |
| **Tile** | Görüntünün 256×256 (varsayılan) parçası; bağımsız sıkıştırılır |
| **Fused pipeline** | Birden fazla aşamayı tek native çağrıda birleştirip ara dönüşümleri (liste↔buffer) elimine etme |
| **Zero-copy** | Veriyi Python listesine çevirmeden doğrudan `array('i')` buffer'ında tutma |
| **Bit-exact** | İki yolun ürettiği baytların **birebir aynı** olması |
| **Native/pure parity** | Rust/C++ yolunun saf Python yoluyla aynı çıktıyı üretmesi |
| **Fallback** | Native yoksa/başarısızsa saf Python yoluna düşme (`None` sözleşmesi) |

---

## Ek A — Tek satır özet

```
PYRA = stdlib-only saf Python kayıpsız görüntü codec'i
     + opsiyonel Rust (24 fonksiyon) ve C++20 (2 fonksiyon) native çekirdek
     + v2 container (auto transform / auto filtre / satır-adaptif predictor)
     → %100 kayıpsız, 40–75× hızlı, native/pure bayt-aynı (2520 kombinasyon testli),
       v1 dosyaları geriye uyumlu okuyor.
```

Repo: `/home/mami/pyra` · HEAD `d40acb7` · 10 commit · local-only (push yok)
