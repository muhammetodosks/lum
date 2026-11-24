"""DICOM Part 10 file support (reading and writing).

The LUMM container is the native format of this project, but a clinical
system has to speak DICOM: a scanner writes DICOM, a PACS stores DICOM, and
a radiologist looks at DICOM. This module therefore reads and writes real
DICOM Part 10 files (the "DICM" wrapper) with the *Explicit VR Little
Endian* transfer syntax and **uncompressed** pixel data, which is the
safest possible choice for medical data: no lossy step is ever involved.

Only what an imaging archive needs is implemented:

* **Explicit VR Little Endian** datasets, which is the transfer syntax
  every modern system supports;
* uncompressed pixel data (``BitsAllocated`` 8, 16 or 32), which covers
  CT, MR, ultrasound, mammography and the rest;
* single frame and multi frame (a volume is one file with
  ``NumberOfFrames``);
* the attributes that carry the clinical meaning: patient, study, series,
  instance, the geometry, the calibration and the display window.

Everything that is read is checked, and everything that is written is
derived from the LUMM metadata, so a round trip through DICOM keeps both
the pixels and the meaning.
"""

import struct
import zlib

from .constants import (SAMPLE_BYTES, SAMPLE_FORMATS, SAMPLE_SIGNED, SFMT_F32,
                        SFMT_I16, SFMT_I32, SFMT_U8, SFMT_U16, SFMT_U32,
                        MedicalError, MedicalMetadataError)
from .dicom import (DEFAULT_SOP_CLASS, MONOCHROME1, MONOCHROME2, RGB,
                    SOP_CLASS_BY_MODALITY, build_dicom_metadata,
                    is_monochrome1, photometric, rescale,
                    validate_dicom_metadata)

#: The transfer syntax this module reads and writes.
EXPLICIT_VR_LITTLE_ENDIAN = "1.2.840.10008.1.2.1"

#: Tag of the pixel data element.
TAG_PIXEL_DATA = (0x7FE0, 0x0010)

#: VR of the pixel data element, per BitsAllocated.
_PIXEL_VR = {8: "OB", 16: "OW", 32: "OW"}

#: The explicit VR headers that use a 32 bit length.
_LONG_VR = {"OB", "OW", "OF", "SQ", "UT", "UN"}


class DicomError(MedicalError):
    """The DICOM file is damaged or uses a feature we do not support."""


# ------------------------------------------------------------------ writing

def _element(group: int, element: int, vr: str, value: bytes) -> bytes:
    """Serialise one explicit VR little endian data element."""
    if vr in _LONG_VR:
        return struct.pack("<HH2sHI", group, element, vr.encode("ascii"),
                           0, len(value)) + value
    if len(value) % 2:
        value += b"\x00" if vr in ("UI", "OB", "OW") else b" "
    return struct.pack("<HH2sH", group, element, vr.encode("ascii"),
                       len(value)) + value


def _text(value) -> bytes:
    return str(value).encode("utf-8")


def _number(value) -> bytes:
    if isinstance(value, float):
        return repr(value).encode("ascii")
    return str(int(value)).encode("ascii")


def _string_list(values) -> bytes:
    if isinstance(values, (list, tuple)):
        return "\\".join(str(v) for v in values).encode("utf-8")
    return str(values).encode("utf-8")


class _Writer:
    """Builds a dataset out of ordered elements."""

    def __init__(self):
        self.parts: list[bytes] = []

    def add(self, group: int, element: int, vr: str, value: bytes) -> None:
        if value is None:
            return
        self.parts.append(_element(group, element, vr, value))

    def text(self, group: int, element: int, vr: str, value) -> None:
        if value is None or value == "":
            return
        self.add(group, element, vr, _text(value))

    def number(self, group: int, element: int, vr: str, value) -> None:
        if value is None:
            return
        self.add(group, element, vr, _number(value))

    def us(self, group: int, element: int, value) -> None:
        """An unsigned short value (binary, little endian)."""
        if value is None:
            return
        self.add(group, element, "US", struct.pack("<H", int(value) & 0xFFFF))

    def list(self, group: int, element: int, vr: str, values) -> None:
        if values is None:
            return
        self.add(group, element, vr, _string_list(values))

    def build(self) -> bytes:
        return b"".join(self.parts)


def write_dicom(samples: bytes, width: int, height: int, depth: int = 1,
                channels: int = 1, sample_format: int = SFMT_U16,
                metadata: dict | None = None,
                sop_class_uid: str | None = None) -> bytes:
    """Build a DICOM Part 10 file from raw samples and a metadata block.

    The metadata block is the same one the LUMM container carries, so the
    clinical meaning travels unchanged. Attributes that the geometry
    determines are always derived here, never taken from the metadata.
    """
    if sample_format not in SAMPLE_FORMATS:
        raise MedicalMetadataError(f"unknown sample format {sample_format}")
    if width <= 0 or height <= 0 or depth <= 0 or channels <= 0:
        raise MedicalMetadataError("degenerate image geometry")
    if sample_format == SFMT_F32:
        raise MedicalMetadataError("float samples have no DICOM equivalent")
    meta = dict(metadata or {})

    sb = SAMPLE_BYTES[sample_format]
    need = width * height * depth * channels * sb
    if len(samples) < need:
        raise MedicalMetadataError("sample buffer too small for the geometry")

    bits = 8 * sb
    signed = sample_format in SAMPLE_SIGNED
    modality = str(meta.get("Modality", "") or "OT")
    sop_class = sop_class_uid or SOP_CLASS_BY_MODALITY.get(
        modality.upper(), DEFAULT_SOP_CLASS)
    sop_instance = str(meta.get("SOPInstanceUID", "") or "")
    if not sop_instance:
        raise MedicalMetadataError("a DICOM file needs a SOPInstanceUID")

    photo = photometric(meta) or (RGB if channels == 3 else MONOCHROME2)
    slope, intercept = rescale(meta)

    ds = _Writer()
    # ---- patient
    ds.text(0x0010, 0x0010, "PN", meta.get("PatientName", ""))
    ds.text(0x0010, 0x0020, "LO", meta.get("PatientID", ""))
    ds.text(0x0010, 0x0030, "DA", meta.get("PatientBirthDate", ""))
    ds.text(0x0010, 0x0040, "CS", meta.get("PatientSex", ""))
    # ---- study
    ds.text(0x0008, 0x0020, "DA", meta.get("StudyDate", ""))
    ds.text(0x0008, 0x0030, "TM", meta.get("StudyTime", ""))
    ds.text(0x0008, 0x0050, "SH", meta.get("AccessionNumber", ""))
    ds.text(0x0008, 0x0090, "PN", meta.get("ReferringPhysicianName", ""))
    # series level, in ascending tag order
    ds.text(0x0020, 0x000D, "UI", meta.get("StudyInstanceUID", ""))
    ds.text(0x0020, 0x000E, "UI", meta.get("SeriesInstanceUID", ""))
    ds.text(0x0020, 0x0010, "SH", meta.get("StudyID", ""))
    ds.text(0x0020, 0x0011, "IS", meta.get("SeriesNumber", "1"))
    ds.text(0x0020, 0x0013, "IS", meta.get("InstanceNumber", "1"))
    ds.text(0x0008, 0x0060, "CS", modality)
    ds.text(0x0008, 0x0016, "UI", sop_class)
    ds.text(0x0008, 0x0018, "UI", sop_instance)
    # ---- image pixel module
    ds.us(0x0028, 0x0002, channels)                     # SamplesPerPixel
    ds.text(0x0028, 0x0004, "CS", photo)
    ds.number(0x0028, 0x0008, "IS", meta.get("NumberOfFrames", depth))
    ds.us(0x0028, 0x0010, meta.get("Rows", height))
    ds.us(0x0028, 0x0011, meta.get("Columns", width))
    ds.us(0x0028, 0x0100, bits)                         # BitsAllocated
    ds.us(0x0028, 0x0101, meta.get("BitsStored", bits))
    ds.us(0x0028, 0x0102, meta.get("HighBit", bits - 1))
    ds.us(0x0028, 0x0103, 1 if signed else 0)           # PixelRepresentation
    ds.number(0x0028, 0x1050, "DS", meta.get("WindowCenter", 0.0))
    ds.number(0x0028, 0x1051, "DS", meta.get("WindowWidth", 0.0))
    ds.number(0x0028, 0x1052, "DS", meta.get("RescaleIntercept", 0.0))
    ds.number(0x0028, 0x1053, "DS", meta.get("RescaleSlope", 1.0))
    if meta.get("PixelSpacing") is not None:
        ds.list(0x0028, 0x0030, "DS", meta["PixelSpacing"])
    if meta.get("SliceThickness") is not None:
        ds.number(0x0018, 0x0050, "DS", meta["SliceThickness"])
    if meta.get("ImagePositionPatient") is not None:
        ds.list(0x0020, 0x0032, "DS", meta["ImagePositionPatient"])
    if meta.get("ImageOrientationPatient") is not None:
        ds.list(0x0020, 0x0037, "DS", meta["ImageOrientationPatient"])
    if meta.get("SpacingBetweenSlices") is not None:
        ds.number(0x0018, 0x0088, "DS", meta["SpacingBetweenSlices"])
    # ---- pixel data
    ds.add(TAG_PIXEL_DATA[0], TAG_PIXEL_DATA[1], _PIXEL_VR[bits],
           samples[:need])

    dataset = ds.build()

    # ---- file meta information (group 0002, always explicit VR little endian)
    meta_group = _Writer()
    meta_group.add(0x0002, 0x0002, "UI", sop_class.encode("ascii"))
    meta_group.add(0x0002, 0x0003, "UI", sop_instance.encode("ascii"))
    meta_group.add(0x0002, 0x0010, "UI",
                   EXPLICIT_VR_LITTLE_ENDIAN.encode("ascii"))
    meta_group.add(0x0002, 0x0012, "UI", b"1.2.840.10008.5.1.4.1.1.7")
    meta_group.add(0x0002, 0x0013, "SH", b"LUMEN-LUMM")
    group_bytes = meta_group.build()

    out = bytearray()
    out += b"\x00" * 128
    out += b"DICM"
    out += _element(0x0002, 0x0000, "UL",
                    struct.pack("<I", len(group_bytes)))
    out += group_bytes
    out += dataset
    return bytes(out)


# ------------------------------------------------------------------ reading

class _Reader:
    """Parses an explicit VR little endian dataset."""

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos
        self.elements: dict[tuple[int, int], object] = {}

    def _u16(self) -> int:
        value = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return value

    def _u32(self) -> int:
        value = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return value

    def read(self, until: int | None = None) -> None:
        limit = len(self.data) if until is None else until
        while self.pos < limit:
            if self.pos + 8 > limit:
                break
            group = self._u16()
            element = self._u16()
            vr = self.data[self.pos:self.pos + 2].decode("ascii", "replace")
            self.pos += 2
            if vr in _LONG_VR:
                self.pos += 2  # reserved
                length = self._u32()
            else:
                length = self._u16()
            if self.pos + length > limit:
                raise DicomError("data element runs past the end of the file")
            value = self.data[self.pos:self.pos + length]
            self.pos += length
            self.elements[(group, element)] = (vr, value)

    def text(self, group: int, element: int) -> str | None:
        item = self.elements.get((group, element))
        if item is None:
            return None
        vr, value = item
        if vr == "US" and len(value) == 2:
            # binary unsigned short
            return str(struct.unpack("<H", value)[0])
        if vr == "UL" and len(value) == 4:
            return str(struct.unpack("<I", value)[0])
        text = value.decode("utf-8", "replace").strip("\x00").strip()
        return text or None

    def number(self, group: int, element: int, default=None):
        text = self.text(group, element)
        if text is None:
            return default
        text = text.split("\\")[0].strip()
        try:
            return float(text) if ("." in text or "e" in text.lower()) \
                else int(text)
        except ValueError:
            return default

    def numbers(self, group: int, element: int) -> list[float] | None:
        text = self.text(group, element)
        if text is None:
            return None
        out = []
        for part in text.split("\\"):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(float(part))
            except ValueError:
                continue
        return out or None


def _sample_format_from_dicom(bits: int, signed: bool) -> int:
    if bits == 8:
        return SFMT_U8
    if bits == 16:
        return SFMT_I16 if signed else SFMT_U16
    if bits == 32:
        return SFMT_I32 if signed else SFMT_U32
    raise DicomError(f"unsupported BitsAllocated {bits}")


def read_dicom(blob: bytes) -> tuple[bytes, dict]:
    """Read a DICOM Part 10 file.

    Returns (raw samples, metadata block) where the metadata block uses the
    same attribute names as the LUMM container, so a DICOM file can be
    re-encoded without renaming anything.
    """
    if len(blob) < 132 or blob[128:132] != b"DICM":
        raise DicomError("not a DICOM Part 10 file")
    reader = _Reader(blob, 132)
    # the file meta information group is always explicit VR little endian
    group_length = None
    if blob[132:134] == b"\x02\x00":
        pass
    # read the group length element first
    pos = reader.pos
    if pos + 12 > len(blob):
        raise DicomError("truncated file meta information")
    group = struct.unpack_from("<H", blob, pos)[0]
    if group != 0x0002:
        raise DicomError("missing file meta information")
    element = struct.unpack_from("<H", blob, pos + 2)[0]
    vr = blob[pos + 4:pos + 6]
    if element != 0x0000 or vr != b"UL":
        raise DicomError("missing file meta information group length")
    length = struct.unpack_from("<I", blob, pos + 8)[0]
    meta_start = pos + 12
    meta_end = meta_start + length
    if meta_end > len(blob):
        raise DicomError("truncated file meta information")
    meta_reader = _Reader(blob, meta_start)
    meta_reader.read(meta_end)

    transfer_syntax = meta_reader.text(0x0002, 0x0010)
    if transfer_syntax and transfer_syntax != EXPLICIT_VR_LITTLE_ENDIAN:
        raise DicomError(
            f"unsupported transfer syntax {transfer_syntax}; only explicit "
            "VR little endian with uncompressed pixel data is supported")

    reader = _Reader(blob, meta_end)
    reader.read()

    if TAG_PIXEL_DATA not in reader.elements:
        raise DicomError("the data set has no pixel data")
    vr, pixel_data = reader.elements[TAG_PIXEL_DATA]

    rows = reader.number(0x0028, 0x0010)
    columns = reader.number(0x0028, 0x0011)
    samples_per_pixel = reader.number(0x0028, 0x0002, 1)
    bits = reader.number(0x0028, 0x0100)
    signed = reader.number(0x0028, 0x0103, 0) == 1
    frames = reader.number(0x0028, 0x0008, 1)
    if rows is None or columns is None or bits is None:
        raise DicomError("the data set does not describe its geometry")
    rows = int(rows)
    columns = int(columns)
    frames = int(frames)
    channels = int(samples_per_pixel)
    sb = bits // 8
    expected = rows * columns * frames * channels * sb
    if len(pixel_data) < expected:
        raise DicomError("the pixel data is shorter than the geometry says")
    if vr == "OW" and sb == 2 and len(pixel_data) % 2:
        raise DicomError("the pixel data has an odd length")

    sample_format = _sample_format_from_dicom(int(bits), signed)

    metadata = {
        "PatientName": reader.text(0x0010, 0x0010) or "",
        "PatientID": reader.text(0x0010, 0x0020) or "",
        "PatientBirthDate": reader.text(0x0010, 0x0030) or "",
        "PatientSex": reader.text(0x0010, 0x0040) or "",
        "StudyDate": reader.text(0x0008, 0x0020) or "",
        "StudyTime": reader.text(0x0008, 0x0030) or "",
        "AccessionNumber": reader.text(0x0008, 0x0050) or "",
        "ReferringPhysicianName": reader.text(0x0008, 0x0090) or "",
        "StudyInstanceUID": reader.text(0x0020, 0x000D) or "",
        "StudyID": reader.text(0x0020, 0x0010) or "",
        "SeriesNumber": reader.text(0x0020, 0x0011) or "",
        "InstanceNumber": reader.text(0x0020, 0x0013) or "",
        "SeriesInstanceUID": reader.text(0x0020, 0x000E) or "",
        "Modality": reader.text(0x0008, 0x0060) or "",
        "SOPClassUID": reader.text(0x0008, 0x0016) or "",
        "SOPInstanceUID": reader.text(0x0008, 0x0018) or "",
        "SamplesPerPixel": channels,
        "PhotometricInterpretation": reader.text(0x0028, 0x0004) or MONOCHROME2,
        "NumberOfFrames": frames,
        "Rows": rows,
        "Columns": columns,
        "BitsAllocated": int(bits),
        "BitsStored": int(reader.number(0x0028, 0x0101, bits)),
        "HighBit": int(reader.number(0x0028, 0x0102, int(bits) - 1)),
        "PixelRepresentation": 1 if signed else 0,
        "TransferSyntaxUID": transfer_syntax or EXPLICIT_VR_LITTLE_ENDIAN,
        "WindowCenter": reader.number(0x0028, 0x1050, 0.0),
        "WindowWidth": reader.number(0x0028, 0x1051, 0.0),
        "RescaleIntercept": reader.number(0x0028, 0x1052, 0.0),
        "RescaleSlope": reader.number(0x0028, 0x1053, 1.0),
    }
    spacing = reader.numbers(0x0028, 0x0030)
    if spacing and len(spacing) >= 2:
        metadata["PixelSpacing"] = spacing[:2]
    thickness = reader.number(0x0018, 0x0050)
    if thickness is not None:
        metadata["SliceThickness"] = float(thickness)
    between = reader.number(0x0018, 0x0088)
    if between is not None:
        metadata["SpacingBetweenSlices"] = float(between)
    position = reader.numbers(0x0020, 0x0032)
    if position and len(position) >= 3:
        metadata["ImagePositionPatient"] = position[:3]
    orientation = reader.numbers(0x0020, 0x0037)
    if orientation and len(orientation) >= 6:
        metadata["ImageOrientationPatient"] = orientation[:6]

    return pixel_data[:expected], metadata


__all__ = ["read_dicom", "write_dicom", "DicomError",
           "EXPLICIT_VR_LITTLE_ENDIAN", "SOP_CLASS_BY_MODALITY"]
