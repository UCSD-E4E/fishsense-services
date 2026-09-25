"""Ported verbatim from fishsense-lite@a8b2c3bc
services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/exif.py.

Minimal EXIF reader for Olympus `.ORF`, stdlib only.

ORF is a TIFF variant (magic `IIRO` rather than the standard 42), so IFD
walking is all that's needed. Deliberately not using a library:

  * **Pillow cannot open ORF.** `TiffImagePlugin` rejects the magic; the legacy
    spider got away with it on Pillow 11.3.0, but current Pillow raises
    `cannot identify image file`.
  * **rawpy** is a data-worker dependency, kept out of the api-worker image on
    purpose (it drags in opencv and libGL for a service whose job is NAS I/O).
  * **exiftool** is what spider shelled out to. It works, at the cost of a Perl
    runtime in the image and a subprocess per batch.

It also works on a **ranged read of the first megabyte**, which is what lets
the ingest dry-run inspect a dive for ~1 MB per file instead of ~15 MB.

Two details are load-bearing:

1. `taken_datetime` comes from tag **0x0132 (`DateTime`)**, not 0x9003
   (`DateTimeOriginal`). That is what spider read, so ~111k existing
   `Image.taken_datetime` values follow it. The two are usually equal, which
   is precisely why reading the wrong one would look fine and drift quietly.
2. Olympus MakerNote offsets are **relative to the start of the MakerNote
   block**, and the Equipment sub-IFD pointer has **type 13** — a non-standard
   IFD type. Miss either and the walk silently finds nothing, which is what
   made MakerNote parsing look infeasible on the first attempt.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

__all__ = ["ExifData", "read_exif"]

# TIFF field type -> byte width. Type 13 (IFD pointer) is non-standard; Olympus
# uses it for the Equipment sub-IFD and a reader that doesn't know it will skip
# the entry and find no serial number.
_TYPE_WIDTH = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}

_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_DATE_TIME = 0x0132
_TAG_ARTIST = 0x013B
_TAG_EXIF_IFD = 0x8769
_TAG_DATE_TIME_ORIGINAL = 0x9003
_TAG_OFFSET_TIME_ORIGINAL = 0x9011
_TAG_MAKER_NOTE = 0x927C

_TAG_OLYMPUS_EQUIPMENT = 0x2010
_TAG_OLYMPUS_SERIAL = 0x0101

# A MakerNote block is `<signature><2-byte order><2-byte version><IFD>`, so its
# IFD starts at `len(signature) + 4`. Two signatures are in the fleet: Olympus
# wrote the first, and the TG-7 — built by OM Digital Solutions after the
# imaging arm was sold — writes the second, which is FOUR BYTES LONGER.
#
# Recognising only `OLYMPUS\0` did not read a TG-7 wrongly; it read nothing, so
# every TG-7 frame reported no serial and `preflight_ingest_activity` refused
# the dive with "No camera serial found in any frame's Olympus MakerNote" —
# true as stated, and impossible for an operator to act on, because the serial
# is right there in the file. Prod has had TG-7 `Camera` rows (FSL-09/10/11,
# `BJPA7562x`) the whole time.
#
# This stays an explicit list rather than "any signature then an IFD". A
# mis-parse would bind a frame to the wrong `Camera` and hand stage 14 the
# wrong intrinsics, which is the same failure the no-`Artist`-fallback rule
# refuses; an unknown body must read as None so preflight can say so.
_MAKER_NOTE_SIGNATURES = (
    b"OLYMPUS\x00",  # Olympus, through the TG-6
    b"OM SYSTEM\x00\x00\x00",  # OM Digital Solutions, TG-7 onwards
)


@dataclass(frozen=True)
class ExifData:
    """What ingest needs from a frame. Every field is optional — a camera that
    didn't write a tag must surface as None so the caller can reject the image
    rather than invent a value."""

    date_time: str | None = None
    #: True when `date_time` came from 0x9003 because 0x0132 was absent. Worth
    #: logging: it means this file's convention differs from the ~111k rows.
    date_time_is_fallback: bool = False
    offset_time: str | None = None
    make: str | None = None
    model: str | None = None
    artist: str | None = None
    serial_number: str | None = None


def _entries(buf: bytes, ifd_offset: int, endian: str, base: int):
    """Yield `(tag, type, count, raw_bytes)` for one IFD.

    `base` is what value offsets are measured from: 0 for the main TIFF IFDs,
    the MakerNote's own start for Olympus sub-IFDs. Everything out of range is
    skipped rather than raising, because the buffer is routinely a truncated
    ranged read.
    """
    if ifd_offset + 2 > len(buf):
        return
    (count,) = struct.unpack_from(endian + "H", buf, ifd_offset)
    for index in range(count):
        pos = ifd_offset + 2 + index * 12
        if pos + 12 > len(buf):
            return
        tag, typ, n = struct.unpack_from(endian + "HHI", buf, pos)
        width = _TYPE_WIDTH.get(typ)
        if width is None:
            continue
        total = width * n
        if total <= 4:
            raw = buf[pos + 8 : pos + 8 + total]
        else:
            (value_offset,) = struct.unpack_from(endian + "I", buf, pos + 8)
            start = base + value_offset
            if start + total > len(buf):
                continue  # points past a truncated read
            raw = buf[start : start + total]
        yield tag, typ, n, raw


def _ascii(raw: bytes) -> str | None:
    """Decode a TIFF ASCII value. Olympus space-pads, so strip — unstripped,
    `Artist` would never match a `Camera.name`."""
    text = raw.split(b"\x00")[0].decode("ascii", "replace").strip()
    return text or None


def _long(raw: bytes, endian: str) -> int | None:
    if len(raw) < 4:
        return None
    return struct.unpack_from(endian + "I", raw, 0)[0]


def _maker_note_ifd_offset(buf: bytes, maker_note_offset: int) -> int | None:
    """Bytes from the block's start to its IFD, or None if unrecognised.

    Keyed off the signature's own length rather than a constant, because that
    length is exactly what differs between the two bodies.
    """
    for signature in _MAKER_NOTE_SIGNATURES:
        end = maker_note_offset + len(signature)
        if buf[maker_note_offset:end] == signature:
            return len(signature) + 4  # + 2-byte order + 2-byte version
    return None


def _maker_note_serial(buf: bytes, maker_note_offset: int) -> str | None:
    """Walk an Olympus / OM System MakerNote to the Equipment sub-IFD's
    SerialNumber.

    Offsets inside are relative to `maker_note_offset`, NOT to the file. That
    single fact is the difference between reading `BJ6C67989` and reading
    garbage.

    The block declares its own byte order, so the file's is deliberately not
    passed in — a MakerNote may disagree with its container.
    """
    header_length = _maker_note_ifd_offset(buf, maker_note_offset)
    if header_length is None:
        return None  # not a layout we understand
    order_at = maker_note_offset + header_length - 4
    mn_endian = "<" if buf[order_at : order_at + 2] == b"II" else ">"
    base = maker_note_offset
    ifd = maker_note_offset + header_length

    for tag, _typ, _n, raw in _entries(buf, ifd, mn_endian, base):
        if tag != _TAG_OLYMPUS_EQUIPMENT:
            continue
        equipment_offset = _long(raw, mn_endian)
        if equipment_offset is None:
            return None
        for eq_tag, _t, _c, eq_raw in _entries(
            buf, base + equipment_offset, mn_endian, base
        ):
            if eq_tag == _TAG_OLYMPUS_SERIAL:
                return _ascii(eq_raw)
    return None


def read_exif(data: bytes) -> ExifData:
    # pylint: disable=too-many-branches
    # One branch per tag. Dispatching through a table would hide which tag maps
    # to which field, and the 0x0132-not-0x9003 choice is the whole point.
    """Parse what ingest needs out of `data`.

    Never raises on malformed input: a NAS hiccup can return a JSON error body
    instead of image bytes (that exact failure corrupted stage 2 in 2026-05),
    and the right response is an empty result the caller rejects, not an
    exception mid-batch.
    """
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return ExifData()
    endian = "<" if data[:2] == b"II" else ">"

    try:
        (ifd0_offset,) = struct.unpack_from(endian + "I", data, 4)

        make = model = artist = date_time = None
        exif_offset = None
        for tag, _typ, _n, raw in _entries(data, ifd0_offset, endian, 0):
            if tag == _TAG_MAKE:
                make = _ascii(raw)
            elif tag == _TAG_MODEL:
                model = _ascii(raw)
            elif tag == _TAG_ARTIST:
                artist = _ascii(raw)
            elif tag == _TAG_DATE_TIME:
                date_time = _ascii(raw)
            elif tag == _TAG_EXIF_IFD:
                exif_offset = _long(raw, endian)

        date_time_original = offset_time = serial_number = None
        if exif_offset is not None:
            for tag, _typ, _n, raw in _entries(data, exif_offset, endian, 0):
                if tag == _TAG_DATE_TIME_ORIGINAL:
                    date_time_original = _ascii(raw)
                elif tag == _TAG_OFFSET_TIME_ORIGINAL:
                    offset_time = _ascii(raw)

            # The MakerNote is resolved separately, NOT from the loop above.
            # Its declared length is the whole raw block (~1.5 MB on a real
            # TG-6 frame), so `_entries` correctly skips it as pointing past a
            # truncated read — which would mean never seeing the entry at all.
            # Only its *offset* is needed; the walk reads from there.
            maker_note_offset = _maker_note_offset(data, exif_offset, endian)
            if maker_note_offset is not None:
                serial_number = _maker_note_serial(data, maker_note_offset)
    except struct.error:
        # Truncated mid-structure. Whatever was read before the tear is not
        # trustworthy, so report nothing.
        return ExifData()

    fallback = date_time is None and date_time_original is not None
    return ExifData(
        date_time=date_time or date_time_original,
        date_time_is_fallback=fallback,
        offset_time=offset_time,
        make=make,
        model=model,
        artist=artist,
        serial_number=serial_number,
    )


def _maker_note_offset(data: bytes, exif_offset: int, endian: str) -> int | None:
    """The MakerNote's *file* offset.

    `_entries` hands back sliced value bytes, but the Olympus walk needs the
    block's position in the file to resolve its base-relative offsets, so the
    pointer is re-read here rather than reconstructed.
    """
    if exif_offset + 2 > len(data):
        return None
    (count,) = struct.unpack_from(endian + "H", data, exif_offset)
    for index in range(count):
        pos = exif_offset + 2 + index * 12
        if pos + 12 > len(data):
            return None
        tag, _typ, _n = struct.unpack_from(endian + "HHI", data, pos)
        if tag == _TAG_MAKER_NOTE:
            (offset,) = struct.unpack_from(endian + "I", data, pos + 8)
            return offset if offset < len(data) else None
    return None
