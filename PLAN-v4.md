# PYRM v4 — "Devasa güncelleme" planı

> **Durum: TAMAMLANDI ✓** — bu plan tarihsel bir kayıttır.
> Kategori 1 (tıp/sağlık uyumu) ✓, Kategori 2 (MED predictor) ✓,
> Kategori 3 (hız: 7.5x encode / 3.1x decode) ✓, Kategori 4 (eksiklerin
> kapatılması) ✓, Kategori 5 (test + dokümantasyon + commit/push) ✓.
> Kapanan commit'ler: `540f656`, `764e835`, `dc35aff`, `287fafb`, `141e4d9`,
> `f8952a7`, `ed41cfe`, `61a6bca`.
> Kalan (bilinçli erteleme): JPEG-LS context modeling, range coder byte-refill,
> DICOM transfer syntax desteği, çoklu dilimli DICOM doğrulaması.

Hedef: sadece hız değil — **kalite, hız, sıkıştırma, tıp/sağlık uyumu** ve
**tüm hata/eksiklerin kalıcı kapatılması**.

Durum (turun başı): HEAD `5527142`, çalışma ağacı temiz.
256×256×32 u16: encode 447 ms, decode 179 ms, 1 556 684 B (2.69x).

## Kategori 1 — Tıp ve sağlık için %100 uyum

Tıbbi görüntüde "kayıpsız" yetmez: **piksel değerinin anlamı** da korunmalı.
Bugün container metadata'sı serbest JSON; oysa bir CT/MR serisi taşınırken
şu bilgiler zorunludur ve kaybolursa görüntü yanlış yorumlanır:

| bilgi | neden önemli | bugün |
|---|---|---|
| RescaleSlope / RescaleIntercept | ham piksel → HU dönüşümü | yok |
| PhotometricInterpretation | MONOCHROME1 = beyaz=siyah (ters), MONOCHROME2 = normal | yok |
| WindowCenter / WindowWidth | görüntüleme penceresi | yalnız preview parametresi |
| BitsStored / BitsAllocated / PixelRepresentation | 12-bit vs 16-bit, signed/unsigned | yalnız BitsStored var |
| StudyInstanceUID / SeriesInstanceUID | seri kimliği | yok |
| Modality / PatientID / SliceThickness | klinik bağlam | yok |

### Yapılacak
1. **`medical/dicom.py` (yeni)** — DICOM-benzeri kanonik anahtarlar:
   - `DICOM_KEYS` (küme), `REQUIRED_KEYS`, `validate_dicom_metadata(meta, geometry)`
     → `MedicalMetadataError` (eksik/geçersiz alan, uyumsuz geometri,
     örnek formatı ile PixelRepresentation çelişkisi, kabul edilmeyen
     PhotometricInterpretation, pencere genişliği ≤ 0 ...).
   - `build_dicom_metadata(...)` — geometri + klinik parametrelerden
     tam bir metadata kümesi üretir (Rows/Columns/NumberOfFrames/
     BitsAllocated/BitsStored/PixelRepresentation/SamplesPerPixel/
     PhotometricInterpretation otomatik).
   - `is_dicom_compatible(meta, geometry) -> bool`, `describe_dicom(meta)`.
2. **`medical/preview.py`** — pencere/level **rescale** (slope·v+intercept)
   ve **MONOCHROME1 tersine çevirme** uygular; C++ ve Python yolları
   birebir aynı baytları üretir.
3. **`medical/codec.py`** — `encode(..., require_dicom=False)`; strict modda
   metadata doğrulanır (klinik kullanımda geçersiz metadata reddedilir).
4. **CLI** — `pyra med verify` (decode etmeden tüm bütünlük kontrolleri),
   `pyra med dump` (metadata JSON), `pyra med info`'da DICOM özeti.

## Kategori 2 — Sıkıştırma kalitesi

- **MED predictor (JPEG-LS / LOCO-I)** + **gradient (bias) correction**:
  `PRED_MED=8`. LOCO-I'deki düzeltme terimi, son residual'ların işaret
  toplamından türetilir ve tahmine eklenir → düzgün gradyanlı tıbbi veride
  belirgin oran kazancı. Deterministik (sabit durum) ✓.
- Her iki yolda (Python referans + Rust) birebir aynı ✓.

## Kategori 3 — Hız

- **Range coder'da byte-refill renormalizasyon**: encoder ve decoder birlikte
  değişir (bayt formatı değişir ama parite testleri doğrular) → hedef 1.5–2x
  coder hızı.
- Ölçüm önce/sonra; **daha yavaşsa geri alınır** (daha önce iki deneme
  reddedildi: iki kademeli CDF modeli ve branchless Fenwick).

## Kategori 4 — Eksiklerin kalıcı kapatılması

- `test_medical.py`: DICOM doğrulama testleri, MED predictor testleri,
  `verify` komutu testi, her dosya bölgesinde bit-flip (şu an sadece 400
  rastgele), bozuk metadata, hatalı geometri.
- `docs/FORMAT-PYRM.md`: yeni bölümler (tıbbi anlamlar, MED predictor).
- README: güncel tablolar.

## Kategori 5 — Commit + push

Her adım ayrı commit; sonunda
`GH_TOKEN=... git push origin main` (GitHub: muhammetodosks/pyra, PRIVATE).

## Sıra
1. `medical/dicom.py` + preview rescale/photometric + codec strict mod
2. CLI `verify`/`dump` + info DICOM özeti
3. MED predictor + bias correction (Python + Rust)
4. Byte-refill renormalizasyon (ölç, kazandırmazsa geri al)
5. Testler genişletme + docs + README
6. Commit'ler + push + rapor
