# LUMEN v3 — 6–7× Hız Planı (Rust + C++)

> **Durum: TAMAMLANDI ✓** — bu plan tarihsel bir kayıttır.
> Sonuç: encode 503 → 127 ms (100x saf Python), decode 85 → 31 ms (102x),
> dosya v1'den %10.4 küçük. Kapanan maddeler:
> A1 (tek geçişli 7-aday skorlama) ✓, A2 (halka tamponlu LZ tablosu) ✓,
> A3 (CRC32 Rust'ta) ✓, A4/A5 (tüm-görsel paralel codec) ✓,
> A6 (paralel planlayıcı) ✓, B1/B2 (C++ PPM I/O) ✓, C (Python glue) ✓,
> D (doğrulama) ✓, E (GitHub private repo + push) ✓.
> Ek olarak: v3 pack genişliği düzeltmesi (`824c713`), Fenwick sembol önbelleği
> (`1481740`), erken iptal + 32-bit bölme (`49f84ca`).

## Hedef
Mevcut native encode 640×480 RGB = **527 ms**, decode = **83 ms** (toplam 610 ms).
Hedef: **~85–95 ms** toplam → **6–7×**.

## Ölçülen darboğazlar (640×480 RGB, Clouds)
| Aşama | Süre |
|---|---|
| planner (auto transform/filter/pred) | 56 ms |
| `pipe_forward_adaptive_into` (6 tile) | 106 ms |
| `encode_lz_method` (6 tile) | 137 ms |
| `encode_rle_method` (6 tile) | 110 ms |
| `encode_raw_method` (6 tile) | 59 ms |
| `rle_encode_bytes` + `pack_residuals_buf` | 16 ms |
| ctypes/Python tile döngüsü | ~40 ms |
| **decode toplam** | **83 ms** |

## Tespit edilen veri düzeltmesi
`pack_residuals` genişliği `(mod.bit_length()+7)//8` → mod=256'da **2 bayt** (gereksiz).
Doğrusu `((mod-1).bit_length()+7)//8` → 1 bayt. Ölçüm (640×480, düzeltilmiş pack):
- clouds: RLE 598 KB → 359 KB, LZ 576 KB → 461 KB
- monument: RLE 268 KB → 187 KB (**RAW'yı geçti → %10 daha küçük dosya**)
- rainbow: LZ 434 KB → 337 KB
→ LZ/RLE girdisi yarıya iner ⇒ **LZ encode ~2× hızlı**.

**Uyum:** v1 dosyalar eski genişliği kullanır (GRAY 8/16-bit farklı) ⇒ sürüm-duyarlı
pack/unpack (v1 legacy, v2 düzeltilmiş).

## Uygulama adımları

### A. Rust çekirdek (`rust/lum_core.rs`)
- **A1** `lum_pipe_forward_adaptive_into`: 7 aday × tam satır geçişi (8 geçiş) →
  **tek geçişte 7 skorer** (7 accumulator, tek döngü) ⇒ ~4× hızlı.
- **A2** LZ eşleme tablosu: `HashMap<u32, Vec<u32>>` + `Vec::remove(0)` (128 B memmove/insert)
  → **halka tamponlu** sabit 32 slot (FIFO eviction + newest-first sırası korunur) ⇒ ~1,5–2×.
- **A3** `lum_crc32` (zlib uyumlu, IEEE tablo) — container Rust'ta kurulacak.
- **A4** `lum_encode_image(pixels, …) -> bytes`: **tek çağrıda tüm görsel**:
  plan → tile'lar × method adayları **`std::thread::scope` ile paralel** → container.
- **A5** `lum_decode_image(data, len) -> pixels`: container parse + paralel tile decode + canvas.
- **A6** `lum_plan` adayları da paralel hesaplanır (16 bağımsız maliyet).

### B. C++ çekirdek (`cpp/lum_fast.cpp`)
- **B1** `lum_ppm_read(path)` / `lum_ppm_write(path, …)`: PNM parse/yazma **Python listesine
  hiç uğramadan** (dosya seviyesi encode/decode tamamen C++).
- **B2** Mevcut adaptive residual/reconstruct korunur (fallback hızı).

### C. Python glue (`api.py`, `codec.py`, `native.py`, `fast.py`, `compress.py`)
- `api.encode_file`: C++ PPM read → `array('i')` → `native.encode_image` → dosyaya yaz.
- `api.decode_file`: `native.decode_image` → C++ PPM write.
- `api.encode`/`decode` (liste API) → aynı native çağrılar (liste dönüşümü hariç).
- `compress.py`: düzeltilmiş pack genişliği (v2) + legacy genişlik (v1).
- Fallback: native yoksa saf Python yolu (aynı baytlar).

### D. Doğrulama
- Native/pure **byte paritesi** (2520+ kombinasyon), v1 legacy okuma, bozuk dosya testleri.
- Test paketi: native + `LUMEN_DISABLE_NATIVE=1` fallback.
- Oran + hız benchmark'ı; README güncelleme.

### E. GitHub
- `gh` ile **private** repo oluştur, token ile push (local-only repo → remote eklenir).

## Riskler / geri dönüş noktaları
- Parite bozulursa: ilgili adım (A1/A2/B1) geri alınabilir; her adım ayrı commit.
- v1/v2 pack uyumsuzluğu: sürüm bayrağı `legacy_pack` decode zincirine plumb edilir.
- Paralellik determinizmi: her tile bağımsız ⇒ çıktı sırası sabit (thread sırası etkisiz).
