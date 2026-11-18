# PYRA & PYRM — Tam Teknik Dokümantasyon

> **PYRA** — saf Python (stdlib-only) kayıpsız görüntü codec'i, opsiyonel Rust + C++20 çekirdeği ile.
> **PYRM** — tıp ve sağlık için tasarlanmış klinik container: 3B hacim, geniş örnek alanları,
> DICOM uyumlu metadata ve katı bütünlük garantileri.

| | |
|---|---|
| Sürüm | `0.3.0` (`pyra.__version__`) |
| PYRA container | **v3** (34 B header, daraltılmış residual paketleme) |
| PYRM container | **v2** (144 B header, SHA-256 content hash) |
| Kaynak dili | Python 3 (yalnızca stdlib) + Rust (std-only) + C++20 |
| Kayıpsızlık | **%100** — decode edilen pikseller bayt bayt aynı |
| Toplam LOC | 14 540 (Rust 5 030 · testler 2 179 · Python 6 838 · C++ 493) |
| Testler | görsel: native **246** / saf Python **179** · tıbbi: native **67** / saf Python **65** |
| Parite | **864** tıbbi + **540** görsel native/pure kombinasyon → bayt-aynı |
| Repo | `/home/mami/pyra` → https://github.com/muhammetodosks/pyra (PRIVATE) |
| HEAD | `61a6bca` "Medical: a container that contradicts its own metadata is not usable" |

---

## İçindekiler

1. [Tasarım ilkeleri](#1-tasarım-ilkeleri)
2. [Depo anatomisi](#2-depo-anatomisi)
3. [PYRA container formatı (v3)](#3-pyra-container-formatı-v3)
4. [PYRA codec boru hattı](#4-pyra-codec-boru-hattı)
5. [Renk transformu](#5-renk-transformu)
6. [Tahmin ediciler](#6-tahmin-ediciler)
7. [Satır-adaptif filtre](#7-satır-adaptif-filtre)
8. [Entropi kodlayıcı](#8-entropi-kodlayıcı)
9. [Sıkıştırma metotları](#9-sıkıştırma-metotları)
10. [Otomatik planlama](#10-otomatik-planlama)
11. [Rust çekirdek](#11-rust-çekirdek)
12. [C++ çekirdek](#12-c-çekirdek)
13. [Dispatch ve saf-Python fallback](#13-dispatch-ve-saf-python-fallback)
14. [PYRM — tıbbi container](#14-pyrm--tıbbi-container)
15. [PYRM kod birimleri ve escape coding](#15-pyrm-kod-birimleri-ve-escape-coding)
16. [DICOM uyumluluğu](#16-dicom-uyumluluğu)
17. [Test paketi](#17-test-paketi)
18. [Performans](#18-performans)
19. [Sıkıştırma oranı](#19-sıkıştırma-oranı)
20. [Güvenlik modeli](#20-güvenlik-modeli)
21. [Python API](#21-python-api)
22. [CLI](#22-cli)
23. [Derleme ve iş akışı](#23-derleme-ve-iş-akışı)
24. [Ölçülüp reddedilen fikirler](#24-ölçülüp-reddedilen-fikirler)
25. [Sınırlar ve gelecek işler](#25-sınırlar-ve-gelecek-işler)
26. [Terim sözlüğü](#26-terim-sözlüğü)
27. [Kayıplı codec — PYRA v6](#27-kayıplı-codec--pyra-v6)

---

## 1. Tasarım ilkeleri

**PYRA** tamamen kayıpsız (reversible) bir görüntü codec'idir. İki şeyi aynı anda yapar:

1. **Saf Python ile tamamen çalışabilir.** `zlib`, `struct`, `ctypes` dışında standart kütüphanenin
   dışına çıkmaz. `rustc` ya da `g++` yoksa her şey yine çalışır.
2. **Aynı baytları çok daha hızlı üretmek.** Sıkıştırmanın sıcak yolları Rust ve C++'ye taşındı.

Tasarımın temel kuralı:

> **Native hızlandırma asla çıktıyı değiştirmez.**
> Native yol ile saf Python yol aynı girdide **birebir aynı container baytlarını** üretir.

**PYRM** aynı çekirdeği tıbbi veri için genişletir: 3B hacim, 8/16/32 bit işaretsiz/işaretli ve
float örnekler, DICOM metadata katmanı ve **decode sırasında doğrulanmayan veri asla
döndürülmez** ilkesi.

| İlke | Uygulama |
|---|---|
| Kayıpsız by construction | Encoder karşıt boru hattını da çalıştırır; `decoded == original` garanti |
| Gerçekten çalışan özellikler | Stub yok, TODO yok, sahte giriş yok (`grep TODO\|FIXME\|NotImplementedError` → 0) |
| Sınırlı bellek | Her limit adlandırılmış, uygulanmış, belgelenmiş |
| Deterministik | Aynı girdi → birebir aynı baytlar; eşitlikte düşük yöntem/aday kazanır |
| Güvenli decoder | Her dış girdi düşman kabul edilir |
| Klinik tutarlılık | Metadata geometriyle çelişiyorsa container "uyumlu" ilan edilmez |

---

## 2. Depo anatomisi

Repo kökü aynı zamanda paket köküdür (`pyra`).

```
pyra/
├── __init__.py            34    public API
├── __main__.py             4    python -m pyra girişi
├── constants.py           58    MAGIC, VERSION, CS_*, PRED_*, METHOD_*, TRANSFORM_*, FILTER_*, hata sınıfları
├── security.py            23    boyut/chunk/dosya doğrulama (decompression bomb koruması)
├── tile.py                36    Tile, split_into_tiles, assemble_from_tiles
├── color.py               94    YCoCg ileri/ters + none transform
├── predictors.py         185    7 tahmin edici + satır-adaptif seçim + filtre bit paketleme
├── entropy.py            253    FenwickCDF, RangeEncoder, RangeDecoder (saf Python referansı)
├── entropycost.py         44    entropi maliyeti yardımcıları
├── compress.py           311    pack_residuals (sürüm-duyarlı), RLE, LZ77
├── container.py          147    PyraContainer: v1/v2/v3 header, tile index, CRC32
├── codec.py              799    PyraCodec: planlama, tile adayları, native/fused dispatch
├── api.py                186    encode/decode/encode_file/decode_file/decode_region/iter_tiles
├── cli.py                566    argparse CLI (görsel + `med` grubu)
├── native.py            1086    Rust çekirdeğin ctypes yükleyicisi
├── fast.py               228    C++ çekirdeğin ctypes yükleyicisi
├── pngbench.py           189    stdlib-only PNG okuma/yazma (benchmark referansı)
│
├── medical/
│   ├── __init__.py        55    PYRM public API
│   ├── constants.py      132    sample formatları, predictorlar, tile flag'ları, hatalar
│   ├── container.py      160    144 B header, index, metadata, CRC32
│   ├── predictors.py     248    9 predictor (7 klasik + slice + MED), filtre tablosu, byte plane
│   ├── lz.py             151    LZ77 (tahsis yapmayan hash tablosu)
│   ├── codec.py          690    referans codec + native dispatch + verify()
│   ├── dicom.py          449    DICOM keyword katmanı, build/validate, UID üretimi
│   ├── dicomfile.py      433    gerçek DICOM Part 10 okuma/yazma
│   ├── preview.py        125    window/level önizleme
│   └── native.py         152    tıbbi Rust çekirdeğin ctypes yükleyicisi
│
├── rust/
│   ├── pyra_core.rs     2233    std-only çekirdek: Fenwick, RangeEnc/RangeDec, LzMatcher, plan
│   ├── pyra_image.rs    1104    tüm-görsel paralel codec, CRC32, escape tabanlı paketleme
│   └── pyra_med.rs      1693    PYRM native çekirdeği: SHA-256, paralel tile'lar
│
├── cpp/pyra_fast.cpp     493    C++20: adaptif filtreler, tıbbi önizleme, PNM I/O
├── scripts/build_native.sh       Rust + C++ derleme betiği
├── tests/
│   ├── test_lossless.py 1315    246 (native) / 179 (fallback) kontrol
│   └── test_medical.py   864     67 (native) / 65 (fallback) kontrol
├── docs/
│   ├── FORMAT-PYRM.md   433     PYRM format spesifikasyonu
│   └── (PYRA container formatı: bu doküman §3 ve README)
└── README.md             524    kullanıcı dokümantasyonu
```

**Katman diyagramı (PYRA):**

```
        ┌─────────────────────────────────────────────┐
        │  api.py / cli.py                             │
        ├─────────────────────────────────────────────┤
        │  codec.py — PyraCodec (planlama, adaylar)    │
        ├──────────────┬──────────────────────────────┤
        ▼              ▼                              ▼
  container.py    predictors.py                   color.py
  (v1/v2/v3 hdr)  (7 predictor + adaptive)      (YCoCg / none)
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

**Katman diyagramı (PYRM):**

```
        ┌─────────────────────────────────────────────┐
        │  pyra.medical: encode / decode / verify      │
        │  pyra.medical.dicom: build / validate        │
        │  pyra.medical.dicomfile: Part 10 I/O         │
        ├─────────────────────────────────────────────┤
        │  medical/codec.py (referans + dispatch)      │
        ├──────────────┬──────────────────────────────┤
        ▼              ▼                              ▼
  medical/       medical/                       medical/
  container.py   predictors.py                  dicom.py
  (144 B hdr)   (9 predictor, byte plane)      (DICOM kuralları)
                       └──────────────┬───────────────┘
                                      ▼
                              medical/lz.py
                        ┌─────────────┴─────────────┐
                        ▼                           ▼
                 medical/native.py            (saf Python
                 (Rust pyra_med.rs)            referans yolu)
```

---

## 3. PYRA container formatı (v3)

```
┌──────────────────────────────────────────────────────────┐
│ Header (34 B, ">8sIIIBBBBIIBB", big-endian)             │
├──────────────────────────────────────────────────────────┤
│ index_size : 4 B (uint32 BE)                             │
│ Tile index : num_tiles × 8 B (offset:u32, length:u32)   │
├──────────────────────────────────────────────────────────┤
│ Tile verileri (sıralı, bitstream)                         │
├──────────────────────────────────────────────────────────┤
│ CRC32 : 4 B (uint32 BE) — header + index + tiles üzerinden│
└──────────────────────────────────────────────────────────┘
```

### 3.1 Header alanları (v3)

| Ofset | Boyut | Alan | Açıklama |
|---|---|---|---|
| 0 | 8 | `magic` | `PYRA\r\n\x1A\n` |
| 8 | 4 | `version` | `3` |
| 12 | 4 | `width` | piksel genişliği (u32) |
| 16 | 4 | `height` | piksel yüksekliği (u32) |
| 20 | 1 | `bit_depth` | 8 veya 16 |
| 21 | 1 | `color_space` | 1=GRAY, 2=RGB, 3=RGBA |
| 22 | 1 | `pred_type` | 0..6 |
| 23 | 1 | `method` | 0=RAW, 1=RLE, 2=LZ, 3=MIX |
| 24 | 4 | `tile_size` | tile kenar uzunluğu (varsayılan 256) |
| 28 | 4 | `num_tiles` | tile sayısı |
| 32 | 1 | `transform` | 0=YCOCG, 1=NONE |
| 33 | 1 | `filter_mode` | 0=GLOBAL, 1=ADAPTIVE |

### 3.2 Geriye uyum

- **v1** — 32 B header (`">8sIIIBBBBII"`), transform/filter baytı yok → okunurken
  `transform = YCOCG`, `filter_mode = GLOBAL`.
- **v2** — v3 ile aynı 34 B header, ancak **eski (geniş) residual paketlemesi** ile.
  Decoder paket genişliğini sürüm baytından seçer, dolayısıyla v2 dosyalar da
  bit-exact çözülür.
- **v3** — residual her sembol için tam ihtiyaç kadar baytta saklanır:
  `((mod-1).bit_length() + 7) // 8`. Bu, `mod = 256` (8-bit gri veya "none"
  transform) durumunda sembol başına **2 bayt yerine 1 bayt** demektir; RLE ve
  LZ girdisi yarıya iner, dosya %8-10 küçülür.

---

## 4. PYRA codec boru hattı

### 4.1 Encode

```
piksel listesi (w×h×ch)
   │
   ├─ 1. TILE BÖLME ──────────────── split_into_tiles()   (varsayılan 256×256)
   ├─ 2. RENK TRANSFORMU ─────────── auto: YCoCg | none
   ├─ 3. KANONİKLEŞTİRME ─────────── + offset (mod/2)
   ├─ 4. TAHMİN ──────────────────── global veya satır başına adaptif
   ├─ 5. PACK ────────────────────── residual → little-endian paketlenmiş bayt
   ├─ 6. METOT ───────────────────── RAW | RLE | LZ  (MIX: tile başına en küçük)
   ├─ 7. ENTROPİ KODLAMA ─────────── adaptif Fenwick CDF range coder
   └─ 8. CONTAINER ───────────────── header + index + tiles + CRC32
```

### 4.2 Decode

Tüm adımlar tersine döner; tile'lar ayrı ayrı çözülür ve `assemble_from_tiles()`
ile kanvasa geri yerleştirilir.

### 4.3 Tile gövdesi düzeni

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
aşabilir, eksik byte'lar 0 olarak doldurulur (paketleme açma arasında simetriktir).

### 4.4 Modüler aritmetik

`py_mod(x, m) = ((x % m) + m) % m` — Python'un negatif modül semantiği, Rust'ta
`let r = x % m; if r < 0 { r + m }` ile birebir korunur.

---

## 5. Renk transformu

### 5.1 YCoCg (varsayılan seçenek)

```
Y  = ( R + 2*G + B)          >> 2
Co = ( R - B )               >> 1
Cg = ( B - G )               >> 1
```
Tersi: `G = Y - Cg`, `B = G + Co`, `R = G + Co`. Alpha kanalı dokunulmaz.

**Artısı:** kanallar arası dekorelasyon. **Eksisi:** residual alfabe genişler
(`1 << (bit_depth+3)`).

### 5.2 none (`TRANSFORM_NONE = 1`)

Kanal dönüşümü yok. Residual alfabe `1 << bit_depth`; offset uygulanmaz; GRAY her
zaman bu moddadır.

### 5.3 Neden "none" çoğu zaman kazanıyor

Dar alfabe, entropi kodlayıcının sembol dağılımını daha verimli örnekler. Prototip
ölçümleri (640 px): Clouds %3,6, Monument %5,1, Numbat %15,4 daha küçük. Bu yüzden
v2'den itibaren **transform otomatik seçilir**.

---

## 6. Tahmin ediciler

`predictors.py` — 7 aday, `PRED_NONE=0 … PRED_MEDIAN=6`:

| ID | Ad | Formül (kanonik satır `cur`, önceki satır `prev`) |
|---|---|---|
| 0 | `none` | `0` |
| 1 | `left` | `cur[x-1]` |
| 2 | `up` | `prev[x]` |
| 3 | `average` | `(left + up) >> 1` |
| 4 | `paeth` | Paeth predictor (a=left, b=up, c=upleft) |
| 5 | `gradient` | `left + up - upleft` |
| 6 | `median` | `median(left, up, upleft)` (JPEG-LS / LOCO-I) |

Residual = `(gerçek − tahmin) mod mod`, `mod = 1 << bit_depth` (none transform)
veya `1 << (bit_depth+3)` (YCoCg, RGB/RGBA).

---

## 7. Satır-adaptif filtre

Her satır, adayların **modüler L1 skoruna** göre kendi predictor'ını seçer:

```
skor = Σ  min(residual, mod − residual)      # modüler "mutlak fark"
```

Düşük skor = daha iyi tahmin. **Tie-break:** eşitlikte **ilk aday** kazanır —
bu Python ↔ Rust ↔ C++ eşdeğerliğini garanti eder. Seçim tablosu tile gövdesinde
**3 bit/satır** olarak saklanır.

Ölçülen kazanç: global seçime göre %0,4–1,5; transform otomatik seçimiyle
birleştiğinde %10'a kadar.

---

## 8. Entropi kodlayıcı

`entropy.py` (referans) ↔ `rust/pyra_core.rs` (`Fenwick`, `RangeEnc`, `RangeDec`).

### 8.1 Adaptif CDF (Fenwick ağacı)

```
n_symbols   = alfabe boyutu
freq[s]      = başlangıçta 1
total        = n_symbols
cap          = max(1 << 15, 2 * n_symbols)
```

- `add(symbol)`: frekansı artırır, ağacı günceller; `total > cap` ise **halving**:
  `freq = (f+1) >> 1`, sonra ağaç yeniden kurulur.
- `cum(symbol)`: sembolün **altındaki** sembollerin toplamı.
- `find(target)`: ikili arama; clamp dalı `pos >= n` ise `(n-1, cum(n-1), freq[n-1])`.

### 8.2 Sembol önbelleği (hız)

Bir sembolü güncellemek **kendi altındaki kümülatifi değiştirmez**. Bu yüzden model,
son güncellenen sembolün `(cum, freq)` çiftini saklar; tekrar eden sembol (gerçek
residual'lar için yaygın) ağaç yürüyüşünü atlar.

Bu saf bir uygulama değişikliğidir: **çıktı baytları değişmez**.

### 8.3 Range encoder / decoder

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

`flush()`: `pending += 1`; sonra `low < 2^30` ise `0` + pending×`1`, aksi halde
`1` + pending×`0`. Bitler **MSB-first** byte'a paketlenir, son bayt sıfırla doldurulur.

Rust tarafında bitler doğrudan byte'a paketlenir (`out` + `acc` + `nbits`), ikinci
geçiş yapılmaz — çıktı birebir aynı, hız daha yüksek.

---

## 9. Sıkıştırma metotları

| Method | Akış | Ne zaman iyi |
|---|---|---|
| `RAW` (0) | residual sembolleri doğrudan entropi kodlayıcıya | gürültülü / rastgele veri |
| `RLE` (1) | residual → packed → RLE → byte literal token'ları → entropi | düz bölgeler |
| `LZ` (2) | residual → packed → RLE → LZ77 token'ları → entropi | tekrarlayan desenler |
| `MIX` (3) | tile başına RAW/RLE/LZ'den **en küçüğü** (+1 byte method tag) | heterojen görüntüler |

### 9.1 LZ77 parametreleri

`MINMATCH=4`, `MAXMATCH=255`, `MAXDIST=65535`, `TOKEN_MATCH=256`, `TOKEN_EOF=257`,
`TOKEN_SYMBOLS=258`, bucket derinliği 32.

Greedy longest-match, 3 baytlık hash anahtarı, adaylar **newest-first** taranır;
eşitlikte **daha yeni** kazanır (`L > best_len` kuralı). Token listesi Python
referansıyla birebir aynıdır.

### 9.2 Erken iptal (bit bütçesi)

Üç adayın hepsini baştan kodlamak yerine RAW adayı bir **byte bütçesi** olarak
kullanılır: RLE/LZ adayı bütçeyi aşar aşmaz terk edilir. Güvenlik gerekçesi: nihai
boyut yalnızca **büyüyebilir**, dolayısıyla terk edilen aday asla kazanamaz. Global
metot kararında alt sınırlar karşılaştırılır; belirsizlikte eksik tile'lar bir kez
daha kodlanır. **Çıktı baytları değişmez.**

---

## 10. Otomatik planlama

`PyraCodec._plan_encoding` — encode başında **tek seferlik** bir karar:
`transform` (YCoCg/none), `filter_mode` (global/adaptive), `pred_type` (7 aday).

### 10.1 Maliyet ölçümü: gerçek coder

İlk denemede ideal code length kullanıldı; bu proxy **sistematik olarak yanlış**
sıralıyordu (Rainbow'da `p1` seçilirken gerçekte `p6` %6 daha küçük dosya
üretiyordu). Nedeni: ideal code length adaptif kodlayıcının halving politikasını
yansıtmaz ve YCoCg'ye biased'tır. **Çözüm:** maliyet gerçek range coder'ın ürettiği
bayt sayısıyla ölçülür — 8 örnek satır gerçekten kodlanır.

### 10.2 Örnekleme: yayılmış satırlar

```python
idx[i] = min(h - 1, i * h // n)      # n = _PLAN_SAMPLE_ROWS = 8
```

İlk N satır temsilci değildir. Ayrıca her örneklenen satır için "up" bağlamı
gerçek satır `y-1` olmalıdır.

N=8 yeterli: 5 gerçek fotoğrafta N=8 ile alınan planlar N=16 ile birebir aynı.

### 10.3 Seçim tablosu (gerçek fotoğraflar, 640 px)

| Görsel | v1 | v3 auto | Seçilen plan |
|---|---|---|---|
| Clouds | 226 227 B | **210 553 B** | (none, adaptive) |
| Monument | 178 485 B | **153 140 B** | (none, global, paeth) |
| Fuji-san | 219 158 B | **204 653 B** | (none, adaptive) |
| Rainbow | 281 869 B | **244 797 B** | (none, adaptive) |
| Numbat | 109 295 B | **96 415 B** | (none, adaptive) |

---

## 11. Rust çekirdek

`rust/pyra_core.rs` (2 233 satır) + `rust/pyra_image.rs` (1 104 satır) —
**std-only** (`rustc --edition 2021 -O --crate-type cdylib`), dış kütüphane yok.

### 11.1 Dışa aktarılan işlevler (özet)

| Fonksiyon | İşlev |
|---|---|
| `pyra_pack_residuals` / `pyra_unpack_residuals` | residual ↔ paketlenmiş bayt |
| `pyra_rle_encode` / `pyra_rle_decode` | RLE |
| `pyra_lz_encode` / `pyra_lz_decode_tokens` | LZ77 |
| `pyra_transform_forward` / `pyra_transform_inverse` | YCoCg |
| `pyra_apply_predictor_row` / `pyra_reconstruct_row` | 7 predictor satır çekirdeği |
| `pyra_pipe_forward` / `pyra_pipe_reconstruct` | fused transform+tahmin |
| `pyra_pipe_forward_adaptive_into` / `pyra_pipe_reconstruct_adaptive_into` | adaptif satır filtresi |
| `pyra_range_encode` / `pyra_range_decode` | entropi kodlayıcının kendisi |
| `pyra_decode_rle_method` / `pyra_decode_lz_method` | fused per-method decoder |
| `pyra_plan` | otomatik planlayıcı (paralel aday ölçümü) |
| `pyra_encode_image` | **tüm görseli tek çağrıda kodla** (paralel tile'lar) |
| `pyra_encode_image_bytes` | ham örnek baytlarından kodlama (liste dönüşümü yok) |
| `pyra_decode_image` | **tüm container'ı tek çağrıda çöz** (paralel tile'lar) |
| `pyra_free_buf` / `pyra_free_buf_i32` | Rust'te ayrılan buffer'ı serbest bırak |

### 11.2 Tüm-görsel paralel codec

`pyra_encode_image` planı çalıştırır, tile geometrisini kurar ve **her tile'ı ayrı
thread'de** kodlar (`std::thread::scope`). Container (header + index + CRC32) Rust
tarafında kurulur. Tile'lar birbirinden bağımsız olduğu için çıktı sırası sabittir;
thread sırası sonucu etkilemez.

`pyra_decode_image` container'ı parse eder, tile'ları paralel çözer ve kanvasa
yerleştirir.

### 11.3 LZ eşleyicisi (tahsis yapmayan)

Eski hâli `HashMap<u32, VecDeque<u32>>` idi → her yeni anahtar için heap allocation
(milyonlarca) + devasa tablo. Yeni hâli:

```
LZ_HASH_BITS = 16                 # 65536 kova
heads: Vec<i32>                  # kova başına son konum
prev:  Vec<i32>                  # pozisyon → önceki aynı hash'li pozisyon
```

Hash: Fibonacci `key * 2654435761 >> (32 - LZ_HASH_BITS)`. Zincir derinliği 64 → 16.
Token akışı Python referansıyla **birebir aynı** kalır.

### 11.4 Dallanmasız modüler aritmetik

Modül her zaman 2'nin kuvveti olduğu için:

```rust
fn mod_norm(r: i64, m: i64) -> i64 {
    ((r as u64) & ((m as u64) - 1)) as i64
}
```

Veriye bağlı dal kalmaz → her kaçırılan dal ~15 sikl tasarrufu.

### 11.5 SHA-256 (tıbbi çekirdek)

`rust/pyra_med.rs` içinde saf Rust FIPS 180-4 uygulaması — dış kütüphane yok.

---

## 12. C++ çekirdek

`cpp/pyra_fast.cpp` (493 satır, C++20) — 4 dışa aktarılmış işlev:

| Fonksiyon | İşlev |
|---|---|
| `pyra_fast_adaptive_residuals` | transform + satır-bazlı 7 aday predictor L1 seçimi |
| `pyra_fast_adaptive_reconstruct` | satır filtreleriyle rekonstrüksiyon + descale + ters transform |
| `pyra_med_preview` | pencere/level ile 8-bit tıbbi önizleme (kanal ortalaması + MONOCHROME1 tersine) |
| `pyra_pnm_read` / `pyra_pnm_write` | binary PGM/PPM I/O (8/16 bit, big-endian) |

C++ modülü saf Python yolunda da devreye girer; amaç fallback'in de hızlı kalması.

---

## 13. Dispatch ve saf-Python fallback

### 13.1 Sözleşme

Her native wrapper **hata/uygunluk durumunda `None` döner** (veya `False`). Çağıran
kod saf Python referans yoluna düşer ve **aynı exception sınıflarını ve mesajlarını**
üretir.

### 13.2 Ortam bayrakları (call-time okunur)

| Değişken | Etki |
|---|---|
| `PYRA_DISABLE_NATIVE=1` | native **ve** C++ backend'i kapatır (saf Python) |
| `PYRA_NATIVE=0` | aynı etki |

Bayraklar **import time'da değil, call time'da** okunur → test paketi her iki modda
da tam konteyner yeniden kodlayıp bayt eşitliğini doğrulayabilir.

### 13.3 Kapsam matrisi

| Aşama | Rust | C++ | Saf Python |
|---|---|---|---|
| pack / unpack residual | ✅ | — | ✅ |
| RLE encode/decode | ✅ | — | ✅ |
| LZ encode | ✅ | — | ✅ |
| LZ decode | ✅ | — | ✅ |
| Range coder | ✅ | — | ✅ |
| Predictor satırları | ✅ | ✅ | ✅ |
| Renk transformu | ✅ | — | ✅ |
| Adaptif satır filtresi | ✅ | ✅ | ✅ |
| Otomatik planlama | ✅ | — | ✅ |
| Tüm-görsel codec | ✅ | — | ✅ |
| PYRM codec + SHA-256 | ✅ | — | ✅ |
| Tıbbi önizleme | — | ✅ | ✅ |
| PNM I/O | — | ✅ | ✅ |

---

## 14. PYRM — tıbbi container

`PYRM` klinik veri için tasarlanmış kardeş container'dır: aynı kayıpsız predictor +
entropi kodlayıcı çekirdeği, ama hacimler, işaretli örnekler ve denetlenebilirlik için.

Tam spesifikasyon: [`docs/FORMAT-PYRM.md`](docs/FORMAT-PYRM.md).

### 14.1 Dosya düzeni

```
+--------------------+  offset 0
|  header (144 B)    |
+--------------------+  offset 144
|  metadata block    |  UTF-8 JSON, CRC-32 korumalı
+--------------------+
|  tile index        |  16 bayt/tile
+--------------------+
|  tile bodies       |  sıkıştırılmış örnekler
+--------------------+
```

### 14.2 Header (144 B, little-endian)

| Ofset | Boyut | Alan | Anlam |
|---:|---:|---|---|
| 0 | 8 | magic | `PYRM\r\n\x1A\n` |
| 8 | 4 | version | 2 |
| 12 | 4 | flags | rezerve, 0 |
| 16 | 4 | width | satır başına örnek |
| 20 | 4 | height | dilim başına satır |
| 24 | 4 | depth | dilim sayısı (2B için 1) |
| 28 | 4 | channels | piksel başına örnek |
| 32 | 4 | sample_format | 0=u8 1=u16 2=u32 3=i16 4=i32 5=f32 |
| 36 | 4 | bits | örnek başına bit (8/16/32) |
| 40 | 4 | tile_size | tile kenarı (örnek) |
| 44 | 4 | num_tiles | toplam tile (tüm dilimler) |
| 48 | 4 | index_offset | tile index'in byte ofseti |
| 52 | 4 | index_size | index boyutu |
| 56 | 4 | meta_offset | metadata bloğu ofseti |
| 60 | 4 | meta_size | metadata boyutu |
| 64 | 4 | meta_crc | metadata CRC-32 |
| 68 | 4 | payload_crc | tüm tile gövdelerinin CRC-32 |
| 72 | 4 | header_crc | bayt 0..72'nin CRC-32 |
| 76 | 4 | predictor | sabit predictor id (satır tablosu yoksa) |
| 80 | 32 | sha256 | ham örnek baytlarının SHA-256 |
| 112 | 32 | reserved | 0 |

### 14.3 Tile index

Tile başına 16 bayt: `offset (u32)`, `length (u32)`, `method (u8)`, `flags (u8)`,
`filter_bytes (u16)`, `crc32 (u32)`. Tile'lar dilim dilim, dilim içinde raster sırasıyla.

### 14.4 Tile gövdesi

```
[ row filter table ]   yalnız 0x02 (ADAPTIVE) flag'i varsa
[ entropy stream ]
```

Filtre tablosu satır başına 4 bit predictor id (MSB-first, `ceil(rows/2)` bayt).

### 14.5 Predictorlar (9 aday)

| id | ad | tahmin |
|---:|---|---|
| 0 | none | 0 |
| 1 | left | soldaki örnek |
| 2 | up | üstteki örnek |
| 3 | average | `(left + up) >> 1` |
| 4 | paeth | PNG Paeth |
| 5 | gradient | `left + up - upper_left` |
| 6 | median | JPEG-LS / LOCO-I median |
| 7 | slice | **önceki dilimdeki aynı örnek** (3B) |
| 8 | **MED** | LOCO-I MED: modüler halka üzerinde en yakın komşu |

Tahmin bağlamı **tile-local**'dir: bir tile komşusundan örnek ödünç almaz, bu yüzden
her tile bağımsız çözülür (ve encoder/decoder tile'ları paralel çalıştırabilir).
Tek istisna `slice` predictor'ıdır.

Auto seçimde adaylar modüler dairesel mesafe ile (`min(r, modulus - r)`) skorlanır;
eşitlikte **ilk aday** kazanır → tüm uygulamalar aynı kararı verir.

### 14.6 Bütünlük garantileri

Decoder sırayla doğrular:

1. magic ve sürüm,
2. header CRC-32 (bayt 0..72),
3. tile index boyutu ↔ tile sayısı,
4. metadata CRC-32,
5. **her tile gövdesinin** CRC-32,
6. tüm payload'ın CRC-32,
7. çözülen örneklerin **SHA-256**'ı header'daki hash ile.

Herhangi bir uyuşmazlık `CorruptedFileError` fırlatır. **Decoder asla doğrulanmamış
veri döndürmez.**

### 14.7 Metadata

UTF-8 JSON (sıralı anahtarlar, kompakt). CRC-32 korumalı. Klinik alanlar DICOM
anahtar adlarıyla birebir aynıdır (`PatientID`, `Modality`, `StudyInstanceUID`,
`Rows`, `Columns`, `NumberOfFrames`, `BitsAllocated`, `BitsStored`, `HighBit`,
`PixelRepresentation`, `PhotometricInterpretation`, `RescaleSlope`,
`RescaleIntercept`, `WindowCenter`, `WindowWidth`, `PixelSpacing`,
`SliceThickness`, `TransferSyntaxUID`, `SOPClassUID` …).

---

## 15. PYRM kod birimleri ve escape coding

### 15.1 Kod birimleri

| sample format | kod değeri | değer baytı | residual modülü |
|---|---|---|---|
| u8 | örnek başına 1 | 1 | 256 |
| u16 | örnek başına 1 | 2 | 65536 |
| u32 | örnek başına 1 | 4 | 2^32 |
| i16 | örnek başına 1 | 2 | 65536 (işaretli → +32768) |
| i32 | örnek başına 1 | 4 | 2^32 (işaretli → +2^31) |
| f32 | örnek başına 4 | 1 | 256 (dört byte plane) |

İşaretli örnekler `modulus/2` eklenerek işaretsiz kod alanına taşınır. Float
örnekler **dört byte plane'ına ayrılır** (önce düşük plane) ve asla tahmin edilmez.

### 15.2 Escape coding (sıkıştırmanın asıl kaynağı)

16/32-bit formatlarda residual'ların büyük çoğunluğu küçüktür (ölçüm: sentetik CT
verisinde **%99,9** residual `|c| < 64`). Bu nedenle:

- Küçük residual'lar **zig-zag** ile 0..127 aralığına eşlenir (`c < 0 ? -2c-1 : 2c`).
- `|c| > 63` olanlar **kaçış sembolü** (128) + 7 bit'lik chunk'lar hâlinde kodlanır.
- Alfabet **129** (0..127 doğrudan + 128 kaçış) — sabit ve küçük.
- Kaçış oranı **%25'ten büyükse** escape şeması kullanılmaz; eski byte-plane
  yolu tercih edilir (karar ölçümle verilir, sabit eşik değil).

Sonuç: 16-bit CT hacminde dosya **%22 küçüldü** (2 000 713 → 1 556 684 B).

---

## 16. DICOM uyumluluğu

### 16.1 Katmanlar

| Modül | Sorumluluk |
|---|---|
| `medical/dicom.py` | DICOM anahtar sözlüğü, metadata üretimi (`build_dicom_metadata`), doğrulama (`validate_dicom_metadata`), kalibrasyon okuma, UID üretimi (`generate_uid`) |
| `medical/dicomfile.py` | Gerçek **DICOM Part 10** dosyası okuma/yazma (Explicit VR Little Endian, sıkıştırmasız pixel data) |

### 16.2 Metadata sadakat sözleşmesi

`PYRM → DICOM → PYRM` zincirinde **nitelik seti birebir aynı kalır**: ne kaybolur
ne de uydurulur. Doğrulama testi:

```
only in original: {}   only in readback: {}   changed: {}
```

Bunun için:

- `build_dicom_metadata` DICOM yazıcının ürettiği **10 standard niteliği de** üretir
  (`HighBit = BitsStored - 1`, `SOPClassUID` modyaliteye göre, `InstanceNumber`,
  `SeriesNumber`, `StudyID`, `StudyTime`, `PatientSex`, `PatientBirthDate`,
  `AccessionNumber`, `ReferringPhysicianName`).
- DICOM dosyası **artan tag sırasıyla** yazılır → **yinelenen eleman yasağına**
  uyulur (DICOM'da tekrar eden eleman geçersizdir).
- Transfer syntax `1.2.840.10008.1.2.1` (Explicit VR LE) sabittir; başka bir
  syntax okunmaya çalışılırsa **reddedilir** (yanlış okuma riski yok).
- Float örnekler reddedilir (DICOM karşılığı yoktur).

### 16.3 Klinik tutarlılık sözleşmesi

`is_dicom_compatible()` **çelişkileri de** reddeder:

- Rows/Columns/NumberOfFrames geometriyle uyuşmuyorsa,
- BitsAllocated/BitsStored/PixelRepresentation sample formatla çelişiyorsa,
- PhotometricInterpretation kanal sayısına uymuyorsa,
- `HighBit != BitsStored - 1` ise,
- `RescaleSlope == 0` ise,
- render edilemeyen bir pencere tanımlıysa.

`verify()` bu problemleri `dicom_problems` listesi olarak döner.

### 16.4 UID üretimi

`generate_uid(seed="")` → `2.25.` arcı + UUID'nin decimal kodlaması (≤ 64 karakter,
DICOM limiti). Seed verilirse deterministik, verilmezse benzersiz.

**Hasta kimliği asla otomatik doldurulmaz** — `PatientID` ve `Modality` kullanıcı
tarafından verilir; `--require-dicom` eksikse dosya **yazılmadan** exit 1 verir.

---

## 17. Test paketi

### 17.1 Görsel (`tests/test_lossless.py`, 1 315 satır)

| Mod | Sonuç |
|---|---|
| Native | **246 passed, 0 failed** |
| Saf Python (`PYRA_DISABLE_NATIVE=1`) | **179 passed, 0 failed** |

Kapsanan gruplar: roundtrip, determinizm, container, güvenlik, ROI, dosya/CLI I/O,
sıkıştırma sağlığı, animasyon, RLE/LZ bozulma fuzz'ı, zorlu metotlar, PNG roundtrip,
CLI bayrakları, native/pure parite, range coder paritesi, buffer pipeline paritesi,
v3 format + v1/v2 geriye uyum.

### 17.2 Tıbbi (`tests/test_medical.py`, 864 satır)

| Mod | Sonuç |
|---|---|
| Native | **67 passed, 0 failed** |
| Saf Python | **65 passed, 0 failed** |

Kapsanan gruplar: roundtrip matrisi (6 format × 5 geometri × 3 içerik × 3 metot ×
3 predictor), metadata round trip, bozuk metadata reddi, header/tile/payload CRC ve
**SHA-256** reddi, **400 bit-flip fuzz** (asla yanlış veri dönmez, reddedilme ≥ 300),
truncation reddi, native/pure parite, determinizm, kenar durumları, `info()`,
sıkıştırma, DICOM metadata build/validate, `verify`, MED predictor, önizleme
anlamları (MONOCHROME1 inversiyonu, rescale etkisi), DICOM Part 10 round trip,
CLI klinik container, CLI dump, klinik tutarlılık, yinelenen DICOM elemanı.

### 17.3 Kapsamlı parite taraması

- **864** tıbbi kombinasyon (6 format × 4 geometri × 3 içerik × 3 metot × 4 predictor)
- **540** görsel kombinasyon (3 cs × 2 bd × 3 içerik × 3 (transform, filter) × 5 metot × 2 boyut)

Her birinde: native encode baytları ↔ saf Python encode baytları, native decode
pikselleri ↔ saf Python decode pikselleri, roundtrip ve metadata karşılaştırması.
**Sonuç: FAILS: 0** — iki yol birebir aynı dosyayı üretir.

---

## 18. Performans

### 18.1 Görsel (256×256, native ↔ saf Python)

| Yol | Encode | Decode |
|---|---|---|
| GRAY + RAW | 42.3x | 59.6x |
| GRAY + RLE | 45.2x | 60.7x |
| GRAY + LZ | 38.5x | 61.6x |
| RGB + RAW | 46.9x | 54.2x |
| RGB + RLE | 48.2x | 62.3x |
| RGB + LZ | 41.5x | 61.6x |
| RGBA + RAW | 49.7x | 58.8x |
| RGBA + RLE | 50.3x | 59.9x |
| RGBA + LZ | 43.1x | 60.8x |

Gerçek fotoğraf (640×360 RGB, Clouds): encode **127 ms** (100x), decode **31 ms** (102x).

### 18.2 Tıbbi hacimler (sentetik CT, u16)

| Hacim | encode | decode | oran |
|---|---|---|---|
| 128×128×16 | 35.5 ms (169x) | 22.6 ms (93x) | 2.67x |
| 256×256×32 | 274.8 ms (215x) | 159.1 ms (113x) | 2.69x |
| 512×512×64 | 2 388.5 ms | 1 414.9 ms | 2.70x |

Toplam: 38 273 024 B → 14 201 844 B (**2.69x**).

### 18.3 v3 med → güncel karşılaştırma (256×256×32)

| | v3 med | şimdi | kazanç |
|---|---|---|---|
| encode | 2 051 ms | **275 ms** | **7.5x** |
| decode | 491 ms | **159 ms** | **3.1x** |
| dosya | 2 000 713 B | **1 556 684 B** | %22 küçük |

---

## 19. Sıkıştırma oranı

### 19.1 Gerçek fotoğraflar (640 px, 8-bit RGB)

| Görsel | ham | PYRA v1 | **PYRA v3** | PNG (zlib) | v3 vs PNG |
|---|---|---|---|---|---|
| Clouds | 691 200 | 226 227 (3.06x) | **210 553 (3.28x)** | 238 846 (2.89x) | −11.8 % |
| Monument Valley | 687 360 | 178 485 (3.85x) | **153 140 (4.49x)** | 142 754 (4.81x) | +7.3 % |
| Fuji-san | 689 280 | 219 158 (3.15x) | **204 653 (3.37x)** | 215 432 (3.20x) | −5.0 % |
| Rainbow | 817 920 | 281 869 (2.90x) | **244 797 (3.34x)** | 240 207 (3.41x) | +1.9 % |
| Numbat | 691 200 | 109 295 (6.32x) | **96 415 (7.17x)** | 91 272 (7.57x) | +5.6 % |
| **toplam** | 3 576 960 | 1 015 034 (3.52x) | **909 558 (3.93x)** | 928 511 (3.85x) | **−2.0 %** |

### 19.2 Tıbbi (sentetik CT hacimleri)

38.3 MB → 14.2 MB (**2.69x**). Kayıpsız: decode edilen örnekler birebir aynı.

---

## 20. Güvenlik modeli

### 20.1 Giriş doğrulama

| Kontrol | Limit |
|---|---|
| `MAX_WIDTH` / `MAX_HEIGHT` | 65 536 |
| `MAX_PIXELS` | 16 384 × 16 384 = 268M piksel |
| `MAX_CHUNK_SIZE` | 512 MB |

### 20.2 Bozuk dosya dayanıklılığı

- CRC32 **decode'dan önce** kontrol edilir.
- Tile offset/length dosya sınırları içinde mi kontrol edilir.
- LZ token akışı uzunluk limiti: `count * value_bytes * 2 + 64`.
- LZ match mesafesi çıktı uzunluğundan büyükse reddedilir.
- Tıbbi formatta: header CRC, metadata CRC, tile CRC, payload CRC ve SHA-256 —
  hepsi doğrulanır.
- Tüm bu durumlar `CorruptedFileError` üretir; `IndexError`/`ValueError` sızmaz.

### 20.3 Kayıp veri yok

Kayıpsız codec'te "hata toleransı" yoktur; her bit bozulması CRC/SHA ile yakalanır.

---

## 21. Python API

### 21.1 Görsel

```python
from pyra import encode, decode, encode_file, decode_file

blob = encode(pixels, width=640, height=480, bit_depth=8,
              color_space=2,        # CS_RGB
              pred_type=-1,         # -1 = auto
              transform=-1,         # -1 = auto
              filter_mode=-1,       # -1 = auto
              tile_size=256,
              method=-1)            # -1 = auto
meta, out_pixels = decode(blob)
assert out_pixels == pixels      # kayıpsız — bit-exact
```

Dosya seviyesi, bölge erişimi, tile iterasyonu ve animasyon da mevcut
(`encode_file`, `decode_file`, `decode_region`, `iter_tiles`, `encode_animation`).

### 21.2 Tıbbi

```python
from pyra.medical import (encode, decode, verify, info, preview_slice,
                          build_dicom_metadata, validate_dicom_metadata,
                          generate_uid, SFMT_U16)

meta = build_dicom_metadata(512, 512, 64, 1, SFMT_U16,
                            patient_id="ANON-1", modality="CT",
                            study_uid=generate_uid(),
                            rescale_slope=1.0, rescale_intercept=-1024.0,
                            window_center=40.0, window_width=400.0)
blob = encode(samples, 512, 512, 64, 1, SFMT_U16, metadata=meta)

report = verify(blob)              # {'ok': True, 'dicom_compatible': True, ...}
metadata, samples_back = decode(blob)
assert samples_back == samples

png = preview_slice(samples_back, 512, 512, 64, 1, SFMT_U16,
                     slice=30, window=400.0, level=40.0)
```

### 21.3 Hata sınıfları

```
PyraError                     # görsel formatlarının taban hatası
├── SecurityError             # limit aşımı, decompression bomb
└── CorruptedFileError        # CRC, magic, sürüm, biçim hataları

MedicalError                  # tıbbi formatların taban hatası
├── CorruptedFileError        # bozuk/truncated container
├── UnsupportedFormatError    # bilinmeyen sample formatı / metot / predictor
└── MedicalMetadataError      # klinik metadata doğrulaması başarısız
```

---

## 22. CLI

```
python3 -m pyra {encode,decode,inspect,verify,benchmark}
python3 -m pyra med {encode,decode,info,dump,verify,preview,to-dicom,from-dicom}
```

### 22.1 Görsel

```bash
python3 -m pyra encode in.ppm out.pyra \
    --method auto|raw|rle|lz|mix \
    --predictor auto|none|left|up|average|paeth|gradient|median \
    --transform auto|ycocg|none \
    --filter auto|global|adaptive \
    --tile 256 --colorspace auto|gray|rgb|rgba
python3 -m pyra inspect out.pyra
python3 -m pyra verify  out.pyra
python3 -m pyra decode out.pyra back.ppm
python3 -m pyra benchmark in.ppm
```

### 22.2 Tıbbi

```bash
# klinik container üret (geometri + DICOM metadata otomatik türetilir)
python3 -m pyra med encode ct.raw ct.pyrm \
    --width 512 --height 512 --depth 64 --format u16 \
    --dicom --patient-id ANON-1 --modality CT \
    --rescale-intercept -1024 --window-center 40 --window-width 400 \
    --require-dicom

python3 -m pyra med info    ct.pyrm          # geometri + klinik özet
python3 -m pyra med dump   ct.pyrm          # tam JSON (geometri + integrite + metadata)
python3 -m pyra med dump   ct.pyrm --metadata-only
python3 -m pyra med verify ct.pyrm          # bütünlük + klinik tutarlılık
python3 -m pyra med preview ct.pyrm s30.pgm --slice 30 --window 400 --level 40
python3 -m pyra med decode ct.pyrm out.raw
python3 -m pyra med to-dicom   ct.pyrm study.dcm
python3 -m pyra med from-dicom study.dcm back.pyrm
```

---

## 23. Derleme ve iş akışı

```bash
cd /home/mami/pyra
bash scripts/build_native.sh
# -> _libpyra_core.so   (Rust: pyra_core.rs + pyra_image.rs + pyra_med.rs)
# -> _libpyra_fast.so   (C++20: pyra_fast.cpp)
```

Derleme başarısız olursa betik hata vermez; codec saf Python yoluna düşer.

### 23.1 Test döngüsü

```bash
cd /home/mami                                  # paket kökünün ÜSTÜ
timeout 1200 python3 -m pyra.tests.test_lossless                       # 246
PYRA_DISABLE_NATIVE=1 timeout 1200 python3 -m pyra.tests.test_lossless # 179
timeout 1200 python3 -m pyra.tests.test_medical                        # 67
PYRA_DISABLE_NATIVE=1 timeout 1200 python3 -m pyra.tests.test_medical  # 65
```

### 23.2 Ölçüm tuzakları

1. **Sentetik standalone micro-benchmark gerçek pipeline'ı yansıtmaz.** Gerçek
   profille interleaved A/B yap.
2. `PYRA_DISABLE_NATIVE=1` set edilmişken "native" çalıştırmak **sahte hız** üretir.
3. Paylaşımlı/aşırı yavaşlayan makinelerde mutlak ms değerleri 3× bozulabilir.
4. Rust'ta `from_raw_parts(null, 0)` → UB; `count == 0` guard'ları şart.
5. `array.array("i", im.tobytes())` → bytes 4'er bayt yorumlanır ✗ →
   `array.array("i", list(im.tobytes()))`.
6. Rust'ta `u64::bit_length` yok → `64 - x.leading_zeros()`.
7. Crate root'un `py_mod` fonksiyonu i32 döndürür → tıbbi modül kendi
   `mod_norm`'unu kullanır.
8. Sabit boyutlu Rust dizileri aday sayısıyla eşleşmeli (`scores: [i64; 9]`).
9. Signed örnek üretiminde `to_bytes(..., signed=True)` + maskelenmiş değer →
   OverflowError; doğrusu unsigned raw bit pattern.
10. DICOM US/UL elemanları **binary** olmalı; `SamplesPerPixel` = channels.
11. `cli.py`'de modül ve fonksiyon adı çakışırsa AttributeError → modülü `_mod`
    suffix'iyle import et.

---

## 24. Ölçülüp reddedilen fikirler

Bunlar uygulandı, ölçüldü ve **performans veya sıkıştırma kaybı** yüzünden geri
alındı. Kayıt altında tutulur ki tekrar denenmesin:

| Deneme | Sonuç | Sebep |
|---|---|---|
| İki kademeli CDF modeli | decode 170 → **187 ms** | büyük alfabelerde blok güncellemeleri patlıyor |
| Branchless Fenwick | decode 170 → **178 ms** | gerçek veride branch predictor iyi çalışıyor |
| LOCO-I gradient/bias correction | dosya **−%0.6 … −%57** | residual'lar gürültü → satır ortalaması rastgele, sadece varyans ekler |
| ZIGZAG_LIMIT 63 → 127 | dosya **+%0.05** | 63 zaten optimal |
| ZIGZAG_LIMIT 63 → 31 | dosya **+%4.5** | kaçış oranı artıyor |
| Fastdiv (f64 bölme) | nötr | riski karşılığında kazanç yok |
| BitWriter (bit biriktirme) | nötr | — |
| Flat open-addressing LZ tablosu | encode_lz **+%12** | next/prev dizilerinde rastgele cache miss |
| E1 burst toplu işleme | encode_raw **+%17** | E1/E2 dönüşümlü çalışıyor → burstlar 1–2 adım |
| 64-bit pencere bit okuyucu | decode_rle **+%3** | maliyet bit okumada değil, low/high/value zincirinde |

---

## 25. Sınırlar ve gelecek işler

### Mevcut sınırlar

| Sınır | Detay |
|---|---|
| Renk derinliği (PYRA) | 8 ve 16 bit; 1/2/4 bit gri tonlama yok |
| Sıkıştırma (PYRA) | tek geçişli, context modeli yok, ileri referans yok |
| Blok filtreler | satır-bazlı; 2D (blok) filtreler yok |
| Animasyon (PYRA) | `encode_animation` var ama container'da animation bloğu yok |
| DICOM | sıkıştırmasız (Explicit VR LE) yazılır/okunur; JPEG/JPEG-LS/RLE transfer syntax'ları desteklenmez |
| DICOM | float örnekler reddedilir (DICOM karşılığı yok) |
| DICOM | yalnızca Explicit VR Little Endian |
| Sıkıştırma (PYRM) | JPEG-LS tarzı context modeling yok |
| Python API | `decode()` piksel listesini döndürür (büyük hacimlerde bellek yoğun) |

### Gelecek işler (öncelik sırasıyla)

1. **JPEG-LS context modeling** (PYRM) — bağlama göre ayrı modeller, tıbbi veride
   %5-15 kazanç beklenir; format değişikliği gerektiği için Python referansıyla
   birebir eşitlik şart.
2. **Range coder byte-refill** — renormalizasyonu byte bazlı yapmak; bayt formatı
   değişir, iki kademeli model denemeleri bu makinede yavaş çıktığı için tek
   başarılı aday.
3. **DICOM transfer syntax desteği** — Deflated Explicit VR LE ve RLE Lossless.
4. **Blok bazlı (2D) filtre adaptasyonu** — satır yerine 8×8 blok.
5. **Paralel tile işleme (PYRA)** — `multiprocessing` ile tile başına paralel encode.
6. **Yerleşik (embedded) decoder** — Rust cdylib'i tek başına C kütüphanesi olarak.
7. **Çoklu dilimli DICOM doğrulaması** — `SpacingBetweenSlices` /
   `ImagePositionPatient` tutarlılık kontrolü.

---

## 26. Terim sözlüğü

| Terim | Anlam |
|---|---|
| **Residual** | Gerçek değerden tahmin edilen değerin çıkarılmasıyla kalan fark (modulo alfabe) |
| **Kanonikleştirme** | Residual'ları sabit aralığa (mod) sığdırma; işaretli örnekler için offset ekleme |
| **Alfabe (mod)** | Residual sembollerinin alabileceği değer sayısı; `1 << bit` |
| **Entropy coder** | Sembol frekanslarına göre değişken bit uzunluğu atayan kodlayıcı |
| **CDF** | Kümülatif frekans (Fenwick ağacında tutulur) |
| **Halving** | Uzun akışlarda frekans tablosunun `(f+1)>>1` ile küçültülmesi |
| **Renormalizasyon** | Range coder'ın aralığı normalize edip bit çıkarması (E1/E2/E3) |
| **Pack** | Residual'leri sabit genişlikte bayta paketleme (little-endian) |
| **Code unit** | Bir örneğin kodlayıcıdaki temel değeri (int: 1, float: 4 byte plane) |
| **Escape coding** | Küçük residual'ları doğrudan, büyükleri kaçış chunk'larıyla kodlama |
| **Zig-zag** | İşaretli residual'ı işaretsiz alana eşleyen dönüşüm |
| **Byte plane** | Çok baytlı değerlerin bayt düzlemlerine ayrılması |
| **Tile** | Görüntünün 256×256 (varsayılan) parçası; bağımsız sıkıştırılır |
| **Adaptive row filter** | Her satırın kendi predictor'ını seçmesi (3 bit/satır) |
| **Auto planning** | Transform/filter/predictor'ın gerçek coder ile ölçülüp seçilmesi |
| **Bit budget / erken iptal** | Bir aday bütçeyi aşınca terk edilmesi (güvenli: boyut yalnızca büyür) |
| **Fused pipeline** | Birden fazla aşamayı tek native çağrıda birleştirmek |
| **Zero-copy** | Veriyi Python listesine çevirmeden `array('i')` buffer'ında tutmak |
| **Bit-exact** | İki yolun ürettiği baytların birebir aynı olması |
| **Native/pure parity** | Rust/C++ yolunun saf Python yoluyla aynı çıktıyı üretmesi |
| **Fallback** | Native yoksa/başarısızsa saf Python yoluna düşme (`None` sözleşmesi) |
| **Metadata fidelity** | PYRM ↔ DICOM ↔ PYRM zincirinde nitelik setinin değişmemesi |
| **Clinical consistency** | Metadata'nın container geometrisiyle çelişmemesi |
| **MONOCHROME1** | Yüksek yoğunluk = siyah olan gri tonlama (görüntüyü tersine çevirir) |
| **Rescale** | `value * slope + intercept` (CT için HU'ya çevirme) |
| **Window/Level** | Görüntüleme penceresi: `level ± window/2` aralığı 8-bit'e eşlenir |

---

## 27. Kayıplı codec — PYRA v6

v3 container'ı kayıpsızdır ve PNG ile yarışır. Fotoğraf için gereken şey
başkadır: kalite/boyut düğmesi, renk dönüşümü, blok dönüşümü ve bağlama
duyarlı bir entropi modeli. v6 bunların hepsini aynı bağımlılıksız Rust
çekirdeğinde yapar; Python tarafı yalnızca ctypes bağlayıcısı ve CLI'dır.

### 27.1 Boru hattı

```
RGB(A) ──► YCoCg ──► 8x8 DCT ──► nicemleme (RDOQ, bant başına) ──► zig-zag
                                                        │
                                     bağlam modeli ──────┤
                                                        ▼
                                            ikili aralık kodlayıcı
```

Sekiz bitlik RGB ve RGBA desteklenir. Gri görüntü tek düzlem olarak saklanır.
Düzlem başına ayrı bir akış üretilir: DC artığı, anlamlılık, işaret, büyüklük
ve kaçış kodları hep aynı aralık kodlayıcısından geçer.

### 27.2 Container (v6)

```
┌──────────────────────────────────────────────┐
│ magic            8B   "PYRA\r\n\x1A\n"       │
│ version          I    4B   (6)               │
│ width            I    4B                     │
│ height           I    4B                     │
│ bit_depth        B    1B   (8)               │
│ color_space      B    1B   (1=GRAY 2=RGB 3=RGBA) │
│ coding           B    1B   bit0 = DCT, bit1 = 4:2:0, bit2 = YCoCg │
│ quality          B    1B   (1..100)          │
│ chroma_mult      B    1B   (adım çarpanı, 1/128) │
│ reserved          11B                        │
│ plane_count      B    1B                     │
├──────────────────────────────────────────────┤
│ her düzlem için: u32 uzunluk + akış           │
├──────────────────────────────────────────────┤
│ CRC32 (tüm dosya üzerinde)  I   4B          │
└──────────────────────────────────────────────┘
```

Başlık 36 bayttır. Nicemleme tabloları dosyada taşınmaz: çözücü onları
`quality`'den yeniden üretir. Bunun tek istisnası 24. bayttaki kroma adım
çarpanıdır — `quality` tek başına onu söylemez, ve çözücünün derleyicisinde
sabit durursa eski dosyalar sessizce yanlış piksele çözülür. Bu yüzden değer
dosyada taşınır; 0 "çarpan yok" anlamına gelir ve bu alan eklenmeden önce
yazılmış dosyaların içeriğidir.
Sürüm 4 ve 5 reddedilir. İkisi de aynı yerleşimi paylaşıyordu ama entropi
modeli ve nicemleyici değiştiği için sessizce yanlış piksel üretirlerdi; sürüm
damgası bunu sessiz bir fark yerine sert bir hataya çevirir.

### 27.3 Nicemleme

Nicemleme tabloları JPEG'in taban tablolarından türetilir ve üç ayarla
biçimlendirilir:

| Ayar | Etki |
|------|------|
| `scale(q)` | Klasik JPEG kalite eğrisi: `q<50` için `5000/q`, değilse `200-2q` |
| **spektral eğim** | Adımı `1 + t·f` ile ölçekler, `f = (u+v)/14`, `t` kalitede -0,6 → -0,95 arası enterpolasyon |
| **bant tablosu** | On frekans bandının her birini ayrı bir çarpanla ölçekler (bkz. 27.6) |
| **RDOQ** | Her AC katsayısı için `D + λ·R` karşılaştırması; `λ = 1,6·step²` |
| **kroma çarpanı** | Kroma adımlarını `0,72` ile ölçekler; dosyada 1/128 birimde taşınır |

Eğim bu codec'in en büyük tek kazancı oldu: 46 dB'de %7. JPEG tabloları
Huffman çağında, yaklaşık 1 bit/piksel için ayarlanmıştı; bağlam modeli olan
4:4:4 bir coder bu oranlarda yüksek frekanslara daha az, alçak frekanslara
daha çok harcamak istiyor. Düşük kalitede (30 dB) işaret tersine döner, bu
yüzden eğim kalitede doğrusal enterpole edilir.

RDOQ başlangıçta `rate_est()` içinde bir katsayıyı 3 bit sayıyordu; gerçek
maliyet ölçümde 5,9 bit çıktı. `λ`'yı 0,1'den 1,6'ya çıkarmak sıfır olmayan
katsayıları doğru fiyatlandırdı ve orta/yüksek oranlarda %1-3 kazandırdı.

Kroma çarpanı, "göz rengi daha az çözer, kromaya daha az bit ver" sezgisinin
tam tersini ölçtü. Ters YCbCr dönüşümü kroma hatasını R için 1,402, B için
1,772 ile çarpar; yani kroma hatası, göz onu daha az umursasa da RGB MSE'ye
baskın gelir. 0,5–1,25 aralığının taraması en iyisinin 0,72 olduğunu gösterdi:
kromaya *daha çok* bit vermek, aynı RGB PSNR'de dosyayı küçültüyor. 38 dB'de
%1,7, 46 dB'de %1,1 kazanç.

### 27.4 Entropi modeli

Tek bir ikili aralık kodlayıcı, 11 bit olasılık, shift-5 uyarlama. Her sembol
sınıfı kendi bağlam tablosunu kullanır:

* **DC**: komşu bloklardan gradyan tahmini (LOCO-I `med3(W, N, NW)`), artık
  Exp-Golomb.
* **`last`**: bloğun son sıfır olmayan zig-zag konumu, 6 bitlik uyarlanabilir
  ağaç (63 düğüm). Eski şema uzunluğu unary, alt bitleri ham gönderiyordu ve
  blok başına ~6,5 bit tutuyordu.
* **Anlamlılık**: bant sınıfı (10) × komşu büyüklük kovası (4) × bir önceki
  katsayının kova değeri (3).
* **Büyüklük**: kesikli unary (`MAG_LEVELS = 8`), sonrasında Exp-Golomb kaçış.
  Komşu bağlamı yalnızca ilk seviyede kullanılır.
* **İşaret**: aynı blokta daha önce kodlanmış alçak frekanslı komşuların
  işaret toplamı (3 sınıf) × sol/üst bloğun işaret durumu (4).

İşaret tahmini, DCT katsayısının bir taban fonksiyonuna izdüşüm olmasından
gelir: güçlü bir kenar, frekans ekseninde işareti düzenli değişen bir koşu
üretir. Komşu *bloklar* bunu söylemez, komşu *frekanslar* söyler — ve zig-zag
sırası gereği `(u-1,v)` ile `(u,v-1)` her zaman `(u,v)`'den önce gelir, yani
çözücüde de hazırdır.

### 27.5 Ölçülen sonuçlar

Altı adet 800 px arka plan görüntüsü, eşleşen RGB PSNR'de megapiksel başına
bayt (JXL: `libjxl --effort 7`, mesafe -1..15 taranmış). Oranlar görüntü
başına hesaplanıp ortalanır:

| PSNR | PYRA | JXL | JXL'ye oran | n |
|------|------|-----|-------------|---|
| 30 dB | — | — | %83,2 | 4 |
| 34 dB | — | — | %87,3 | 4 |
| 38 dB | 61,15 KB | 63,67 KB | %99,2 | 6 |
| 42 dB | 109,66 KB | 123,31 KB | %97,6 | 6 |
| 46 dB | 183,36 KB | 230,08 KB | %88,2 | 6 |

Hız ve onun nasıl ölçüldüğü 27.10'da.

PYRA beş hedefte de JXL'den küçük. 30 ve 34 dB satırları altı değil dört
görüntü kapsıyor: JXL hiçbir mesafede Clouds'ta 34,64 dB'in, Fuji'de
35,68 dB'in altına inemiyor, dolayısıyla orada karşılaştırılacak bir nokta
yok. Kazancın büyük kısmı eskiden kaybettiğimiz düz fotoğraflardan geliyor:
38 dB'de Fuji %5,2, Clouds %3,8, Province %3,5 küçüldü; dokulu görüntülerde
(Rainbow, Northan) %30-50'lik mevcut fark korunuyor.

### 27.6 Bant tablosu

JPEG'in taban tabloları adımı `u+v`'nin bir fonksiyonu olarak biçimlendirir:
on frekans bandı her kalitede aynı şekilde ele alınır. Ölçüm bunun doğru
olmadığını gösterdi. Tablo, `class_of` ile indekslenen on çarpan olarak
tanımlanır ve spektral eğimin üstüne, hem luma hem kroma için uygulanır.

Katsayılar tahminle değil aramayla bulundu: eşleşen PSNR'de gerçek codec
üzerinde koordinat inişi, her hedefte dokuz tur. Ortaya çıkan şekil log
uzayında neredeyse doğrusal ama banttan banda farklı: alçak bantlar daha kaba
(1,10-1,25), yüksek bantlar daha ince (0,82-0,95) bir adım istiyor. Yani
"yüksek frekansa daha çok bit ver" sezgisi yalnızca eğimde değil, bant
dağılımında da geçerli.

Tablo kaliteyle birlikte değişir. Tek bir tabloyu her kalitede kullanmak 46
dB'de %2,7 kazandırıyor ama 34 dB'de düz görüntüleri geriletiyor (Fuji +%5,4,
Clouds +%3,2). Şekil kalınlıkla değişiyor, dolayısıyla üç çıpa arasında
parçalı doğrusal olarak enterpole edilir:

| Çıpa | Kalite | Rolü |
|------|--------|------|
| `BAND_LOW` | 10 | Alçak oran şekli |
| `BAND_MID` | 56 | Orta bantlarda yukarı kabaran şekil |
| `BAND_HIGH` | 90 | Yüksek oran şekli |

Çıpaların dışında şekil sabit tutulur. Ölçülen aralığın ötesine ekstrapolasyon
denendi ve reddedildi: doğru, hiçbir fitin gerekçelendirmediği kadar kaba
adımlar üretiyor.

Neden iki değil üç çıpa: iki çıpa tek bir doğru parçası demek, ve şekil orta
kalitede yukarı kabarıyor. 38 dB'de — PYRA'nın JXL'in tek gerisinde kaldığı
nokta — iki çıpalı sürüm Fuji'de hâlâ %2,1 kaybediyordu. Üç çıpa 30 dB'de %1
daha kötü ama 38 dB'yi düzeltiyor, ve 30 dB zaten rahat kazanılan uç.

### 27.7 Renk dönüşümü

Dönüşüm başlangıçta YCbCr idi ve kroma hatası RGB MSE'ye baskın geliyordu:
ters dönüşüm onu R için 1,402, B için 1,772 ile çarpıyor. YCoCg'nin tersi
birim katsayılıdır:

```
R = Y + Co - Cg
G = Y + Cg
B = Y - Co - Cg
```

Aynı kroma hatası, RGB'de yaklaşık 1,4-1,8 kat daha az hata demek. Ölçüm
beklentiyi doğruladı: 38 dB'de %7,4, 46 dB'de %6,0 kazanç — spektral eğimden
sonraki en büyük tek kazanç. Dönüşüm `coding` baytının 2. bitinde işaretlenir,
yani eski YCbCr dosyaları okunmaya devam eder. Kroma çarpanının optimumu
YCoCg altında da 0,72'de kaldı (0,6 / 0,8 / 0,9 / 1,0 / 1,1 / 1,25 tarandı).

### 27.8 4:2:0 neden varsayılan değil

Kroma yarıörnekleme, eşleşen RGB PSNR'de her zaman daha büyük dosya üretti.
Sebep yapısal: 4:4:4'te kroma düzlemleri zaten çok düz, bağlam modeli onları
neredeyse bedava kodluyor; 4:2:0 ise luma'yı aynı PSNR'e çekmek için daha çok
bit harcatıyor. Seçenek API'de duruyor (`--subsampling 4:2:0`), varsayılan
değil.

### 27.9 Ölçülüp reddedilenler

| Fikir | Sonuç |
|-------|-------|
| 4:2:0 kroma | Eşleşen PSNR'de her zaman daha büyük |
| Düz HF şekillendirme (`PYRA_HF`) | Daha kaba adım kaybettiriyor |
| Kroma stride çarpanı (`PYRA_CS`) | 1,4 / 2,0 / 2,8 hepsi kötü |
| İkinci derece eğim (`PYRA_T2`) | ±0,6 ve 1,2 hepsi kötü |
| 9 durumlu komşu bağlamı | Nötr (69,8 vs 69,7 KB) |
| Blok içi enerji bağlamı | Nötr ile negatif arası |
| Büyüklük için ikili ağaç | Kesikli unary'den kötü |
| Kroma adımı kaba (`PYRA_CS`/`PYRA_CQ` > 1) | Hepsi kaybettirdi; en iyisi 0,72, yani daha *ince* kroma |
| 5 alternatif DC tahmincisi (`PYRA_DCP` 1–5) | LOCO-I `med3` her hedefte kazandı; planar ve ortalamalar 2–4 KB daha büyük |
| Exp-golomb kalan bitlerini modelleme (`PYRA_REM`) | Sınıf içi kalan bitler düzgün dağılmış: 70 KB'lik düzlemde 23 bayt |
| Üst-sağ komşuyu bağlama ekleme (`PYRA_NR`) | 34 dB'de nötr/kötü, diğerlerinde fark yok |
| 16×16 DCT bloğu | Province +0,3/+3,0/+4,7 %, Rainbow −3,3/−1,0/−1,3 % (34/38/42 dB) — zayıf noktayı derinleştiriyor |
| Tek bant tablosu (38 dB'de fit) | 46 dB'de −%2,7 ama 34 dB'de düz görüntülerde +%5,4 (Fuji) / +%3,2 (Clouds) |
| İki çıpalı tek doğru (q=10..90) | 30 dB'de %1 daha iyi, ama 38 dB'de Fuji hâlâ +%2,1 — şekil ortada kabarıyor |
| Çıpaların dışına ekstrapolasyon | Fitin gerekçelendirmediği kadar kaba adımlar üretiyor |
| Blok düzeyinde thread + paylaşılan coder | Aralık kodlayıcı doğası gereği sıralı; blok başına yeniden başlatma orandan %2-4 yer |
| `-C target-cpu=native` | %3; yanında 6,4 kat varken taşınabilirliği bozmaya değmez |
| Ayarları görüntü başına okumak | Düzlem başına okumayla ölçülebilir fark yok, düzlem başına sürüm değişikliği yerel tutuyor |

Aralık kodlayıcının kendisi optimal: `PYRA_STATS` her kalitede gerçek çıktıyı
ideal maliyetle (∑−log₂p) karşılaştırıyor ve oran %100,0. Yani darboğaz
coder değil, model ve katsayılar.

### 27.10 Hız

Erken sürümlerde yazılan "~19 MP/s" sayısı yanlıştı: küçük bir görüntüde
ölçülmüştü. 1920×1080 bir fotoğrafta (2,07 MP, dört çekirdek, ısınmadan sonra
beş tur) gerçek değer **2,86 MP/s** çıktı. Aşağıdaki sayılar aynı görüntü
(Province) üzerinde, q=50/85/95 için ölçüldü:

| Aşama | q50 | q85 | q95 |
|-------|-----|-----|-----|
| başlangıç | 2,86 | 2,78 | 2,67 MP/s |
| ayarlar döngü dışına | 12,59 | 11,36 | 10,08 MP/s |
| + düzlem iş parçacıkları | **18,67** | **17,12** | **16,28 MP/s** |

Çözme 20,50 / 18,13 / 15,59'dan **31,00 / 28,11 / 25,82 MP/s**'ye çıktı.
Hiçbir aşama tek bir çıktı baytını değiştirmedi.

**Ortam değişkenleri katsayı başına okunuyordu**

`deadzone()` `rdoq`'nun 64 katsayılık döngüsünün *içinde*, `lam()` ise blok
başına çağrılıyordu. İkisi de `std::env::var` sarıyor; bu çağrı her seferinde
kilit alıp bir `String` ayırıyor. Yani blok başına 65 ortam okuması vardı, hem
de katsayı başına çalışan bir yolda. Değerler görüntü ortasında değişemez, bu
yüzden düzlem başına bir kez çözülüp içeri geçiriliyor. Kodlama 2,86 → 12,59
MP/s, 4,4 kat, baytlar aynı.

**Düzlemler paralel kodlanıp çözülüyor**

Bir düzlemin DC tahmin zinciri, komşuluk durumu ve aralık kodlayıcısı o
düzlemin içinde kalır; düzlem sırası bir düzlemin baytlarını etkileyemez. Bu
yüzden düzlemler scoped thread'lere dağıtılıp akışlar dosya sırasında
birleştiriliyor. `PAR_MIN_PIXELS` (256×256) altında sıralı yol korunuyor:
thread açma maliyeti küçük görüntünün işinden büyük.

Doğruluk, paralelliği zorla kapatılmış ikinci bir derlemeyle (`PAR_MIN_PIXELS =
usize::MAX`) karşılaştırılarak kanıtlandı: dört görüntü, iki boyut (320×240 ve
1920×1080 — eşiğin iki yanı) ve iki kalite, hem kap baytları hem çözülen
pikseller: 16/16 aynı. `ref_q50` / `ref_q85` referansları da aynı hash'i veriyor,
`tests/test_lossy.py` 72/72 geçiyor.

**JPEG XL ile hız, aynı makinede**

`ffmpeg -c:v libjxl -effort 7`, aynı 1920×1080 görüntü, mesafe 2,0/1,0/0,5
(90/128/185 KB):

* kodlama: JXL 1,92-2,00 MP/s, PYRA 16,3-18,7 MP/s → **~9 kat**
* çözme: JXL 11,2-12,6 MP/s, PYRA 25,8-31,0 MP/s → **~2,7 kat**

JXL sayıları ffmpeg'in kendi açılışını ve PPM giriş/çıkışını içeriyor, yani
JXL'in hızı için bir üst sınır; oran olduğundan küçük, abartılı değil.
