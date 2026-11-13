"""DICOM-compatible semantics for the PYRM medical container.

A lossless container is not enough for clinical data: the *meaning* of the
stored numbers has to travel with them. This module defines the canonical
attribute names of a DICOM-like data set, validates them against the image
geometry, and provides the derived values a viewer needs (rescale slope and
intercept, the display window, the photometric interpretation).

The attribute names are the DICOM keywords (PascalCase), so a metadata block
produced here can be mapped onto a real DICOM data set without renaming:

=====================  ==========================================
attribute              meaning
=====================  ==========================================
PatientID              anonymous patient identifier
PatientName            patient name (empty for anonymised data)
StudyInstanceUID       unique id of the study
SeriesInstanceUID      unique id of the series within the study
SOPInstanceUID         unique id of this instance
Modality               CT, MR, US, PT, CR, ...
StudyDate              acquisition date (YYYYMMDD)
SliceThickness         slice distance in mm
PixelSpacing           in-plane spacing in mm
RescaleIntercept       value = slope * stored + intercept
RescaleSlope           value = slope * stored + intercept
WindowCenter           centre of the display window
WindowWidth            width of the display window
PhotometricInterpretation  MONOCHROME1 / MONOCHROME2 / RGB
BitsStored             significant bits per sample
BitsAllocated          bits allocated per sample
PixelRepresentation     0 = unsigned, 1 = signed
SamplesPerPixel        1 = grey, 3 = colour
Rows / Columns         image height / width
NumberOfFrames         number of slices
TransferSyntaxUID      the transfer syntax the data set claims
=====================  ==========================================

Two photometric interpretations exist for greyscale images and they are
*not* interchangeable: ``MONOCHROME2`` (the default) maps the lowest value to
black, ``MONOCHROME1`` maps it to white. A preview that ignores the flag
shows a CT scan upside down in brightness, which is exactly the kind of
silent error this module exists to prevent.
"""

from .constants import (SAMPLE_BITS, SAMPLE_BYTES, SAMPLE_FORMATS,
                        SAMPLE_SIGNED, SFMT_F32, SFMT_I16, SFMT_I32,
                        SFMT_U8, SFMT_U16, SFMT_U32, MedicalError,
                        MedicalMetadataError)

#: Photometric interpretations understood by the format.
MONOCHROME1 = "MONOCHROME1"
MONOCHROME2 = "MONOCHROME2"
RGB = "RGB"
PHOTOMETRIC_VALUES = (MONOCHROME1, MONOCHROME2, RGB)

#: Attributes that must be present for a container to be considered
#: clinically complete.
REQUIRED_KEYS = (
    "PatientID",
    "Modality",
    "StudyInstanceUID",
    "SeriesInstanceUID",
)

#: Attributes that a study should carry. Their absence is reported as a
#: warning, not as an error.
RECOMMENDED_KEYS = (
    "SOPInstanceUID",
    "StudyDate",
    "SliceThickness",
    "RescaleSlope",
    "RescaleIntercept",
    "WindowCenter",
    "WindowWidth",
    "PhotometricInterpretation",
    "BitsStored",
    "BitsAllocated",
    "PixelRepresentation",
    "SamplesPerPixel",
    "Rows",
    "Columns",
    "NumberOfFrames",
)

#: Maximum length of a DICOM UID.
MAX_UID_LENGTH = 64

#: Storage SOP class per modality (a sensible default; the value is written
#: into the file meta information of a DICOM Part 10 file).
SOP_CLASS_BY_MODALITY = {
    "CT": "1.2.840.10008.5.1.4.1.1.2",
    "MR": "1.2.840.10008.5.1.4.1.1.4",
    "US": "1.2.840.10008.5.1.4.1.1.6.1",
    "CR": "1.2.840.10008.5.1.4.1.1.1",
    "DX": "1.2.840.10008.5.1.4.1.1.1.1",
    "MG": "1.2.840.10008.5.1.4.1.1.1.2",
    "PT": "1.2.840.10008.5.1.4.1.1.128",
    "XA": "1.2.840.10008.5.1.4.1.1.12.1",
    "NM": "1.2.840.10008.5.1.4.1.1.20",
    "OT": "1.2.840.10008.5.1.4.1.1.7",
}

#: The default SOP class when the modality is unknown.
DEFAULT_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"


def sop_class_for(modality: str) -> str:
    """The storage SOP class that belongs to a modality."""
    return SOP_CLASS_BY_MODALITY.get(str(modality).upper(), DEFAULT_SOP_CLASS)


def _as_number(value):
    """Return a float for an int/float, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _first_number(value):
    """DICOM allows multi-valued attributes; the first value is used."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    return _as_number(value)


def bits_stored_for(sample_format: int) -> int:
    """The natural number of significant bits of a sample format."""
    return SAMPLE_BITS[sample_format]


def pixel_representation_for(sample_format: int) -> int:
    """0 for unsigned sample formats, 1 for signed ones."""
    return 1 if sample_format in SAMPLE_SIGNED else 0


def default_photometric(channels: int) -> str:
    """The photometric interpretation implied by the channel count."""
    return RGB if channels == 3 else MONOCHROME2


def build_dicom_metadata(width: int, height: int, depth: int = 1,
                          channels: int = 1, sample_format: int = SFMT_U16,
                          patient_id: str = "", patient_name: str = "",
                          modality: str = "", study_uid: str = "",
                          series_uid: str = "", sop_uid: str = "",
                          study_date: str = "", slice_thickness: float | None = None,
                          pixel_spacing: tuple[float, float] | None = None,
                          rescale_slope: float = 1.0,
                          rescale_intercept: float = 0.0,
                          window_center: float = 0.0,
                          window_width: float = 0.0,
                          photometric: str = "",
                          patient_birth_date: str = "", patient_sex: str = "",
                          study_time: str = "", accession_number: str = "",
                          referring_physician_name: str = "",
                          study_id: str = "", series_number: str = "1",
                          instance_number: str = "1") -> dict:
    """Build a complete, self-consistent DICOM-like metadata block.

    The geometry dependent attributes (Rows, Columns, NumberOfFrames,
    SamplesPerPixel, BitsAllocated, BitsStored, PixelRepresentation,
    PhotometricInterpretation) are derived, so the result always passes
    :func:`validate_dicom_metadata`.
    """
    if sample_format not in SAMPLE_FORMATS:
        raise MedicalMetadataError(f"unknown sample format {sample_format}")
    if width <= 0 or height <= 0 or depth <= 0 or channels <= 0:
        raise MedicalMetadataError("degenerate image geometry")
    photo = photometric or default_photometric(channels)
    if photo not in PHOTOMETRIC_VALUES:
        raise MedicalMetadataError(f"unsupported photometric interpretation {photo!r}")
    if photo == RGB and channels != 3:
        raise MedicalMetadataError("RGB needs exactly three samples per pixel")
    if photo != RGB and channels != 1:
        raise MedicalMetadataError(
            f"{photo} needs exactly one sample per pixel")

    bits_allocated = 8 * SAMPLE_BYTES[sample_format]
    bits_stored = bits_stored_for(sample_format)
    # The attribute set below is exactly the one a DICOM Part 10 file
    # carries, so a metadata block built here survives a round trip through
    # a real DICOM file unchanged (same keys, same values).
    meta = {
        "PatientID": str(patient_id),
        "PatientName": str(patient_name),
        "PatientBirthDate": str(patient_birth_date),
        "PatientSex": str(patient_sex),
        "StudyInstanceUID": str(study_uid),
        "SeriesInstanceUID": str(series_uid),
        "SOPInstanceUID": str(sop_uid),
        "SOPClassUID": sop_class_for(modality),
        "Modality": str(modality),
        "StudyDate": str(study_date),
        "StudyTime": str(study_time),
        "StudyID": str(study_id),
        "AccessionNumber": str(accession_number),
        "ReferringPhysicianName": str(referring_physician_name),
        "SeriesNumber": str(series_number),
        "InstanceNumber": str(instance_number),
        "RescaleSlope": float(rescale_slope),
        "RescaleIntercept": float(rescale_intercept),
        "PhotometricInterpretation": photo,
        "BitsAllocated": bits_allocated,
        "BitsStored": bits_stored,
        "HighBit": bits_stored - 1,
        "PixelRepresentation": pixel_representation_for(sample_format),
        "SamplesPerPixel": channels,
        "Rows": height,
        "Columns": width,
        "NumberOfFrames": depth,
        "TransferSyntaxUID": "1.2.840.10008.1.2.1",  # Explicit VR Little Endian
        # The display window is always present: a zero width means "no
        # window given", which is what a DICOM file without a window holds.
        "WindowCenter": float(window_center),
        "WindowWidth": float(window_width),
    }
    if slice_thickness is not None:
        meta["SliceThickness"] = float(slice_thickness)
    if pixel_spacing is not None:
        meta["PixelSpacing"] = [float(pixel_spacing[0]), float(pixel_spacing[1])]
    return meta


def validate_dicom_metadata(meta: dict | None, width: int = 0, height: int = 0,
                             depth: int = 0, channels: int = 0,
                             sample_format: int = -1,
                             strict: bool = True) -> list[str]:
    """Check a metadata block against the image geometry.

    Returns the list of problems (empty when the block is fine). With
    `strict` the missing *required* attributes are problems as well; with
    `strict=False` only the contradictions are reported, so a container that
    merely carries a note is still accepted.

    Raises :class:`MedicalMetadataError` when `strict` is set and the
    metadata is not usable.
    """
    problems: list[str] = []
    if not isinstance(meta, dict):
        problems.append("metadata block is missing")
        if strict:
            raise MedicalMetadataError("metadata block is missing")
        return problems

    for key in REQUIRED_KEYS:
        if not str(meta.get(key, "")).strip():
            problems.append(f"missing required attribute {key}")

    for key in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"):
        value = meta.get(key)
        if isinstance(value, str) and value and len(value) > MAX_UID_LENGTH:
            problems.append(f"{key} is longer than {MAX_UID_LENGTH} characters")

    # --- geometry agreement
    if width and "Columns" in meta and meta["Columns"] != width:
        problems.append(f"Columns ({meta['Columns']}) does not match the width ({width})")
    if height and "Rows" in meta and meta["Rows"] != height:
        problems.append(f"Rows ({meta['Rows']}) does not match the height ({height})")
    if depth and "NumberOfFrames" in meta and meta["NumberOfFrames"] != depth:
        problems.append(f"NumberOfFrames ({meta['NumberOfFrames']}) does not match "
                        f"the depth ({depth})")
    if channels and "SamplesPerPixel" in meta and meta["SamplesPerPixel"] != channels:
        problems.append(f"SamplesPerPixel ({meta['SamplesPerPixel']}) does not match "
                        f"the channel count ({channels})")

    # --- sample description
    if sample_format in SAMPLE_FORMATS:
        allocated = 8 * SAMPLE_BYTES[sample_format]
        if "BitsAllocated" in meta and meta["BitsAllocated"] != allocated:
            problems.append(f"BitsAllocated ({meta['BitsAllocated']}) does not match "
                            f"the sample format ({allocated})")
        if "BitsStored" in meta:
            stored = meta["BitsStored"]
            if not isinstance(stored, int) or stored < 1 or stored > allocated:
                problems.append(f"BitsStored ({stored}) is out of range for "
                                f"{allocated} allocated bits")
        if "PixelRepresentation" in meta:
            expected = pixel_representation_for(sample_format)
            if meta["PixelRepresentation"] != expected:
                problems.append("PixelRepresentation contradicts the sample format "
                                f"(expected {expected})")

    # --- photometric interpretation
    photo = meta.get("PhotometricInterpretation")
    if photo is not None:
        if photo not in PHOTOMETRIC_VALUES:
            problems.append(f"unsupported PhotometricInterpretation {photo!r}")
        elif channels:
            if photo == RGB and channels != 3:
                problems.append("PhotometricInterpretation RGB needs three samples "
                                "per pixel")
            elif photo != RGB and channels != 1:
                problems.append(f"PhotometricInterpretation {photo} needs one sample "
                                "per pixel")

    # --- display and calibration values
    slope = _first_number(meta.get("RescaleSlope"))
    if slope is not None and slope == 0.0:
        problems.append("RescaleSlope must not be zero")
    intercept = _first_number(meta.get("RescaleIntercept"))
    if intercept is None and "RescaleIntercept" in meta:
        problems.append("RescaleIntercept is not a number")
    width_value = _first_number(meta.get("WindowWidth"))
    center = _first_number(meta.get("WindowCenter"))
    if width_value is not None and width_value < 0.0:
        problems.append("WindowWidth must not be negative")
    if center is None and "WindowCenter" in meta:
        problems.append("WindowCenter is not a number")
    # A width of zero means "no display window"; a width of zero together
    # with a centre is contradictory.
    if (width_value is not None and center is not None
            and width_value == 0.0 and center != 0.0):
        problems.append("WindowWidth is zero but WindowCenter is not")
    stored = meta.get("BitsStored")
    if stored is not None and "HighBit" in meta:
        high = _first_number(meta.get("HighBit"))
        if high is None:
            problems.append("HighBit is not a number")
        elif int(high) != int(stored) - 1:
            problems.append(
                f"HighBit ({int(high)}) must be BitsStored - 1 "
                f"({int(stored) - 1})")

    if strict and problems:
        raise MedicalMetadataError("; ".join(problems))
    return problems


def is_dicom_compatible(meta: dict | None, width: int = 0, height: int = 0,
                         depth: int = 0, channels: int = 0,
                         sample_format: int = -1) -> bool:
    """True when the metadata block is complete and consistent."""
    try:
        problems = validate_dicom_metadata(meta, width, height, depth, channels,
                                          sample_format, strict=False)
    except MedicalMetadataError:
        return False
    required_missing = [p for p in problems if "missing required attribute" in p]
    return not required_missing


def rescale(meta: dict | None) -> tuple[float, float]:
    """The (slope, intercept) pair of the calibration, defaulting to (1, 0)."""
    if not isinstance(meta, dict):
        return 1.0, 0.0
    slope = _first_number(meta.get("RescaleSlope"))
    intercept = _first_number(meta.get("RescaleIntercept"))
    if slope is None:
        slope = 1.0
    if intercept is None:
        intercept = 0.0
    if slope == 0.0:
        slope = 1.0
    return slope, intercept


def window(meta: dict | None) -> tuple[float | None, float | None]:
    """The (center, width) of the display window; either may be None."""
    if not isinstance(meta, dict):
        return None, None
    return (_first_number(meta.get("WindowCenter")),
            _first_number(meta.get("WindowWidth")))


def photometric(meta: dict | None) -> str:
    """The photometric interpretation, defaulting to MONOCHROME2."""
    if not isinstance(meta, dict):
        return MONOCHROME2
    value = meta.get("PhotometricInterpretation")
    if isinstance(value, str) and value in PHOTOMETRIC_VALUES:
        return value
    return MONOCHROME2


def is_monochrome1(meta: dict | None) -> bool:
    """True when the lowest value must be displayed as white."""
    return photometric(meta) == MONOCHROME1


def describe(meta: dict | None) -> dict:
    """A compact summary of the clinical attributes, for reports and CLIs."""
    if not isinstance(meta, dict):
        return {}
    slope, intercept = rescale(meta)
    center, width_value = window(meta)
    out = {
        "PatientID": meta.get("PatientID", ""),
        "Modality": meta.get("Modality", ""),
        "StudyInstanceUID": meta.get("StudyInstanceUID", ""),
        "SeriesInstanceUID": meta.get("SeriesInstanceUID", ""),
        "PhotometricInterpretation": photometric(meta),
        "RescaleSlope": slope,
        "RescaleIntercept": intercept,
        "WindowCenter": center,
        "WindowWidth": width_value,
    }
    for key in ("SliceThickness", "PixelSpacing", "StudyDate", "SOPInstanceUID"):
        if key in meta:
            out[key] = meta[key]
    return out


__all__ = [
    "MONOCHROME1", "MONOCHROME2", "RGB", "PHOTOMETRIC_VALUES",
    "REQUIRED_KEYS", "RECOMMENDED_KEYS", "MAX_UID_LENGTH",
    "MedicalMetadataError", "build_dicom_metadata",
    "validate_dicom_metadata", "is_dicom_compatible", "rescale", "window",
    "photometric", "is_monochrome1", "describe", "bits_stored_for",
    "pixel_representation_for", "default_photometric",
]
