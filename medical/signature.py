"""Optional HMAC-SHA256 signature and provenance audit for PYRM containers.

A CRC-32 or a plain SHA-256 tells you that a file was *not* damaged. It
does not tell you that the file is *yours*: anybody who can rewrite the
container can also rewrite its CRC and its content hash. A keyed signature
closes that gap - it proves that the holder of the secret key produced the
file and that nobody touched it afterwards.

The signature lives in the reserved header area (offset 112..144, exactly
32 bytes) and covers **everything else in the file**: the header fields
(including the content hash and the metadata CRC), the metadata block, the
tile index and the payload. Only the signature field itself is excluded,
which is what makes the scheme self-consistent.

The container level flag ``FLAG_SIGNED`` in the header marks a signed
container. Unsigned containers keep working exactly as before - the flag is
simply not set and the reserved area stays zero.

The implementation uses nothing but the standard library (``hmac`` and
``hashlib``), so a container signed on one machine can be verified on any
other.
"""

import hashlib
import hmac
import struct
import zlib

from .constants import FLAG_SIGNED, CorruptedFileError

#: Offset of the signature field (the reserved area of the header).
SIGNATURE_OFFSET = 112
#: Length of the signature (a full SHA-256 digest).
SIGNATURE_SIZE = 32
#: Offset of the container flags field.
FLAGS_OFFSET = 12
#: Offset of the header CRC-32 field.
HEADER_CRC_OFFSET = 72


def signed_region(blob: bytes) -> bytes:
    """Return the bytes the signature covers.

    Everything except the signature field itself: the header fields before
    it, the metadata block, the tile index and the payload.
    """
    if len(blob) < SIGNATURE_OFFSET + SIGNATURE_SIZE:
        raise CorruptedFileError("truncated PYRM header")
    return blob[:SIGNATURE_OFFSET] + blob[SIGNATURE_OFFSET + SIGNATURE_SIZE:]


def is_signed(blob: bytes) -> bool:
    """True when the container carries a signature."""
    if len(blob) < SIGNATURE_OFFSET + SIGNATURE_SIZE:
        return False
    flags = struct.unpack_from("<I", blob, FLAGS_OFFSET)[0]
    return bool(flags & FLAG_SIGNED)


def compute(blob: bytes, key: bytes) -> bytes:
    """Compute the HMAC-SHA256 of a container over its signed region."""
    if not key:
        raise ValueError("the signing key must not be empty")
    return hmac.new(key, signed_region(blob), hashlib.sha256).digest()


def sign(blob: bytes, key: bytes) -> bytes:
    """Return a signed copy of the container.

    The container flags are updated first, then the header CRC-32 (which
    covers the flags), and only then the signature is computed over the
    signed region. The result is byte-identical no matter which backend
    produced the unsigned container.
    """
    if not key:
        raise ValueError("the signing key must not be empty")
    if len(blob) < SIGNATURE_OFFSET + SIGNATURE_SIZE:
        raise CorruptedFileError("truncated PYRM header")
    out = bytearray(blob)
    flags = struct.unpack_from("<I", out, FLAGS_OFFSET)[0] | FLAG_SIGNED
    struct.pack_into("<I", out, FLAGS_OFFSET, flags)
    crc = zlib.crc32(bytes(out[:HEADER_CRC_OFFSET])) & 0xFFFFFFFF
    struct.pack_into("<I", out, HEADER_CRC_OFFSET, crc)
    mac = hmac.new(key, signed_region(bytes(out)), hashlib.sha256).digest()
    out[SIGNATURE_OFFSET:SIGNATURE_OFFSET + SIGNATURE_SIZE] = mac
    return bytes(out)


def status(blob: bytes, key: bytes | None = None) -> str:
    """Return the signature state of a container.

    * ``"unsigned"`` - the container carries no signature;
    * ``"unverified"`` - it is signed, but no key was supplied;
    * ``"verified"`` - it is signed and the key matches.

    A key that does not match raises :class:`CorruptedFileError`.
    """
    if not is_signed(blob):
        return "unsigned"
    if key is None:
        return "unverified"
    stored = blob[SIGNATURE_OFFSET:SIGNATURE_OFFSET + SIGNATURE_SIZE]
    mac = compute(blob, key)
    if not hmac.compare_digest(mac, stored):
        raise CorruptedFileError(
            "container signature does not match the supplied key")
    return "verified"


def audit_block(samples: bytes, width: int, height: int, depth: int,
                channels: int, sample_format: int, encoder: str,
                codec: str, timestamp: str | None = None) -> dict:
    """Build a deterministic provenance block for the metadata.

    The block describes *what* was encoded and *by whom*, so a series can
    be traced back to the encoder that produced it. It carries no
    timestamp unless the caller supplies one: a timestamp would make every
    encoding of the same data different, which would break the byte-exact
    parity between the native and the pure-Python backend.
    """
    from .constants import SAMPLE_BYTES
    block = {
        "Encoder": encoder,
        "Codec": codec,
        "Columns": width,
        "Rows": height,
        "NumberOfFrames": depth,
        "SamplesPerPixel": channels,
        "BytesPerSample": SAMPLE_BYTES.get(sample_format, 0),
        "SourceSHA256": hashlib.sha256(samples).hexdigest(),
    }
    if timestamp:
        block["Timestamp"] = timestamp
    return block


__all__ = ["sign", "status", "compute", "is_signed", "signed_region",
           "audit_block", "SIGNATURE_OFFSET", "SIGNATURE_SIZE"]
