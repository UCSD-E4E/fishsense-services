"""Ported verbatim from fishsense-lite@a8b2c3bc
services/fishsense-api-workflow-worker/tests/_tiff_builder.py.

Build synthetic TIFF/ORF bytes for the EXIF reader's tests.

The alternative is committing a 15 MB `.ORF`, which makes the tests slow, the
diff unreviewable, and every edge case ("what if `DateTime` is missing?")
impossible to express. Constructing the bytes means each test states exactly
the file it is about.

A real `.ORF` is still exercised — see the parity test in `test_exif.py`, which
reads the data-worker's committed fixture when present. Synthetic bytes prove
the parser handles a shape; the real file proves the shape is the one Olympus
actually writes.

Layout produced:

    header | IFD0 | <value area: MakerNote, Exif IFD, ASCII strings>

Offsets are resolved in two passes — one to learn IFD0's length, one to emit
real offsets. Solving it analytically would be shorter and much harder to read,
and this is test scaffolding.
"""

from __future__ import annotations

import struct

# TIFF field types used here.
ASCII = 2
LONG = 4
UNDEFINED = 7
IFD = 13  # non-standard; Olympus uses it for the Equipment sub-IFD pointer

# The tags the reader cares about.
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_DATE_TIME = 0x0132  # what spider read — NOT DateTimeOriginal
TAG_ARTIST = 0x013B
TAG_EXIF_IFD = 0x8769
TAG_DATE_TIME_ORIGINAL = 0x9003
TAG_OFFSET_TIME_ORIGINAL = 0x9011
TAG_MAKER_NOTE = 0x927C
TAG_OLYMPUS_EQUIPMENT = 0x2010
TAG_OLYMPUS_SERIAL = 0x0101

# Signature, then a 2-byte order marker and a 2-byte version — so the block's
# IFD starts at len(signature) + 4. Olympus TG-6 writes the first; the TG-7,
# built by OM Digital Solutions after the Olympus imaging sale, writes the
# second, and its signature is FOUR BYTES LONGER. That difference is the whole
# reason a TG-7 frame reads as having no serial.
OLYMPUS_SIGNATURE = b"OLYMPUS\x00"
OM_SYSTEM_SIGNATURE = b"OM SYSTEM\x00\x00\x00"


def _entry(endian: str, tag: int, typ: int, count: int, payload: bytes | int) -> bytes:
    """One 12-byte IFD entry. `payload` is an inline value or an offset."""
    value = (
        struct.pack(endian + "I", payload)
        if isinstance(payload, int)
        else payload.ljust(4, b"\x00")[:4]
    )
    return struct.pack(endian + "HHI", tag, typ, count) + value


def _ifd(endian: str, entries: list[bytes], next_ifd: int = 0) -> bytes:
    return (
        struct.pack(endian + "H", len(entries))
        + b"".join(entries)
        + struct.pack(endian + "I", next_ifd)
    )


def _maker_note(endian: str, serial_number: str, signature: bytes) -> bytes:
    """An Olympus2 MakerNote whose inner offsets are **base-relative**.

    That is the property worth encoding: the Equipment sub-IFD pointer and the
    serial's offset are measured from the start of the MakerNote block, not
    from the start of the file. A reader that treats them as file offsets finds
    garbage — which is exactly the bug that made MakerNote parsing look
    infeasible the first time round.
    """
    header = signature + (b"II" if endian == "<" else b"MM") + b"\x03\x00"
    serial_raw = serial_number.encode("ascii") + b"\x00"
    # Sizes are fixed for a single-entry IFD, so one probe pass is enough.
    probe_equipment = _ifd(endian, [_entry(endian, TAG_OLYMPUS_SERIAL, ASCII, 1, 0)])
    probe_mn = _ifd(endian, [_entry(endian, TAG_OLYMPUS_EQUIPMENT, IFD, 1, 0)])

    equipment_rel = len(header) + len(probe_mn)
    serial_rel = equipment_rel + len(probe_equipment)

    equipment_ifd = _ifd(
        endian,
        [_entry(endian, TAG_OLYMPUS_SERIAL, ASCII, len(serial_raw), serial_rel)],
    )
    mn_ifd = _ifd(
        endian, [_entry(endian, TAG_OLYMPUS_EQUIPMENT, IFD, 1, equipment_rel)]
    )
    return header + mn_ifd + equipment_ifd + serial_raw


def _body(value_base: int, endian: str, fields: dict) -> tuple[bytes, bytes]:
    """Emit (ifd0, value_area) with values placed from `value_base` onwards."""
    blobs: list[bytes] = []
    cursor = value_base

    def place(data: bytes) -> int:
        nonlocal cursor
        offset = cursor
        blobs.append(data)
        cursor += len(data)
        if cursor % 2:  # real files stay word-aligned
            blobs.append(b"\x00")
            cursor += 1
        return offset

    def ascii_entry(tag: int, text: str | None) -> bytes | None:
        if text is None:
            return None
        raw = text.encode("ascii") + b"\x00"
        payload = raw if len(raw) <= 4 else place(raw)
        return _entry(endian, tag, ASCII, len(raw), payload)

    maker_note_offset = (
        place(
            _maker_note(endian, fields["serial_number"], fields["maker_note_signature"])
        )
        if fields["serial_number"] is not None
        else None
    )

    exif_entries = [
        e
        for e in (
            ascii_entry(TAG_DATE_TIME_ORIGINAL, fields["date_time_original"]),
            ascii_entry(TAG_OFFSET_TIME_ORIGINAL, fields["offset_time"]),
        )
        if e is not None
    ]
    if maker_note_offset is not None:
        exif_entries.append(
            _entry(endian, TAG_MAKER_NOTE, UNDEFINED, 1024, maker_note_offset)
        )
    exif_ifd_offset = place(_ifd(endian, exif_entries))

    ifd0_entries = [
        e
        for e in (
            ascii_entry(TAG_MAKE, fields["make"]),
            ascii_entry(TAG_MODEL, fields["model"]),
            ascii_entry(TAG_DATE_TIME, fields["date_time"]),
            ascii_entry(TAG_ARTIST, fields["artist"]),
        )
        if e is not None
    ]
    ifd0_entries.append(_entry(endian, TAG_EXIF_IFD, LONG, 1, exif_ifd_offset))

    return _ifd(endian, ifd0_entries), b"".join(blobs)


def build_orf(  # pylint: disable=too-many-arguments
    *,
    endian: str = "<",
    magic: int = 0x4F52,  # 'RO' — what Olympus writes instead of 42
    date_time: str | None = "2025:03:06 17:00:15",
    date_time_original: str | None = "2025:03:06 17:00:15",
    make: str | None = "OLYMPUS CORPORATION    ",
    model: str | None = "TG-6            ",
    artist: str | None = "FSL-07                         ",
    serial_number: str | None = "BJ6C67989",
    offset_time: str | None = "-08:00",
    maker_note_signature: bytes = OLYMPUS_SIGNATURE,
) -> bytes:
    """A minimal but structurally faithful ORF.

    Any argument set to None omits that tag — how the tests express "this
    camera didn't write a DateTime".
    """
    fields = {
        "date_time": date_time,
        "date_time_original": date_time_original,
        "make": make,
        "model": model,
        "artist": artist,
        "serial_number": serial_number,
        "offset_time": offset_time,
        "maker_note_signature": maker_note_signature,
    }
    header = struct.pack(endian + "2sHI", b"II" if endian == "<" else b"MM", magic, 8)
    # Pass 1: learn IFD0's length. Pass 2: place values after it.
    probe_ifd0, _ = _body(0, endian, fields)
    ifd0, values = _body(len(header) + len(probe_ifd0), endian, fields)
    return header + ifd0 + values
