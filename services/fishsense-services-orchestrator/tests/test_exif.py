"""Ported from fishsense-lite@a8b2c3bc
services/fishsense-api-workflow-worker/tests/test_exif.py -- only the import path and
the fixture location changed. The fixture is the first megabyte of v1's committed
TG-6 frame (the parser reads only that much).

EXIF reader for `.ORF` — the ingest path's source of `taken_datetime` and
camera identity.

Why hand-rolled rather than a library:

  * **Pillow cannot open ORF.** Spider used Pillow 11.3.0; on current Pillow
    `Image.open` raises `cannot identify image file` — ORF's magic is `IIRO`,
    which `TiffImagePlugin` rejects. Verified against the real fixture.
  * **rawpy is a data-worker dependency** and deliberately absent from the
    api-worker (opencv + libGL in an image whose job is NAS I/O).
  * **exiftool** is what spider shelled out to for the serial. It works, at the
    cost of a Perl runtime in the image and a subprocess per batch.

ORF is TIFF, so ~100 lines of stdlib does it, and it works on a **ranged read**
of the first megabyte — which is what makes the ingest dry-run cost ~1 MB per
file instead of ~15 MB.

Two details are load-bearing and both are pinned below:

  * `taken_datetime` comes from tag **0x0132 (`DateTime`)**, not 0x9003
    (`DateTimeOriginal`). That is what spider read, and ~111k existing rows
    follow it.
  * The Olympus MakerNote's inner offsets are **relative to the MakerNote
    block**, and the Equipment pointer is **type 13** — a non-standard IFD
    type. Missing either makes the parse silently return nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._tiff_builder import OM_SYSTEM_SIGNATURE, build_orf

_REAL_ORF = Path(__file__).parent / "fixtures" / "tg6_frame_first_1mb.ORF"


def _read(data: bytes):
    from fishsense_services_orchestrator.ingest.exif import (
        read_exif,
    )

    return read_exif(data)


# ── taken_datetime ────────────────────────────────────────────────────


def test_reads_date_time_from_tag_0x0132_not_date_time_original():
    """The tag choice matters. Spider read `DateTime` (0x0132); every existing
    `Image.taken_datetime` follows that. They are usually equal, so a reader
    that took 0x9003 would look correct and drift only on files where the two
    differ."""
    data = build_orf(
        date_time="2024:08:21 08:56:51",
        date_time_original="1999:01:01 00:00:00",  # deliberately different
    )

    assert _read(data).date_time == "2024:08:21 08:56:51"


def test_falls_back_to_date_time_original_when_0x0132_is_absent():
    """Some bodies omit 0x0132. Falling back beats rejecting the frame, but the
    caller is told so it can log which tag was used."""
    data = build_orf(date_time=None, date_time_original="2024:08:21 08:56:51")

    result = _read(data)

    assert result.date_time == "2024:08:21 08:56:51"
    assert result.date_time_is_fallback is True


def test_date_time_is_none_when_neither_tag_is_present():
    """Must be None, never a default. Stage-1 clustering is pure timestamp
    maths, so a fabricated timestamp corrupts it silently — ingest rejects the
    image instead."""
    data = build_orf(date_time=None, date_time_original=None)

    assert _read(data).date_time is None


def test_offset_time_is_surfaced_but_not_applied():
    """The camera records local time plus an offset; spider discarded the
    offset and stamped UTC, and ~111k rows follow that convention. Ingest
    reproduces it, but surfaces the offset so the divergence is visible in the
    report rather than silently dropped."""
    data = build_orf(offset_time="-08:00")

    assert _read(data).offset_time == "-08:00"


# ── camera identity ───────────────────────────────────────────────────


def test_reads_the_olympus_makernote_serial():
    """The chosen camera key. Base-relative offsets and the type-13 Equipment
    pointer are both required to reach it — this is the test that fails if
    either is dropped."""
    data = build_orf(serial_number="BJ6C67989")

    assert _read(data).serial_number == "BJ6C67989"


def test_reads_the_om_system_makernote_serial():
    """The TG-7 writes the same Equipment sub-IFD under a different signature.

    Olympus sold its imaging arm, so the current bodies identify as
    `OM SYSTEM\\0\\0\\0` rather than `OLYMPUS\\0` — a signature four bytes
    longer, which moves the block's IFD from +12 to +16. A reader that knows
    only the old one finds no serial at all, and `preflight_ingest_activity`
    then refuses the dive with "No camera serial found in any frame's Olympus
    MakerNote" — correct behaviour, wrong premise, and unfixable from the
    operator's side because the serial *is* in the file.

    Prod already has TG-7 `Camera` rows (FSL-09/10/11, serials `BJPA7562x`),
    so before this every TG-7 dive was un-ingestable.
    """
    data = build_orf(
        serial_number="BJPA75627", maker_note_signature=OM_SYSTEM_SIGNATURE
    )

    assert _read(data).serial_number == "BJPA75627"


def test_an_unrecognised_makernote_signature_yields_no_serial():
    """The guard that keeps the two known layouts from becoming "any twelve
    bytes then an IFD". Guessing here would bind a frame to the wrong `Camera`
    and hand stage 14 the wrong intrinsics, which is exactly the failure the
    no-`Artist`-fallback rule exists to avoid."""
    data = build_orf(
        serial_number="BJPA75627", maker_note_signature=b"NIKON\x00\x00\x00"
    )

    assert _read(data).serial_number is None


def test_serial_is_none_when_there_is_no_makernote():
    """A non-Olympus body. Returning None lets preflight fail loudly rather
    than guessing a camera."""
    data = build_orf(serial_number=None)

    assert _read(data).serial_number is None


def test_reads_artist_make_and_model():
    """`Artist` is the rig name and is used only as a cross-check against the
    camera the serial resolved to — a disagreement means a mislabelled
    `Camera` row, which nothing else would catch."""
    result = _read(build_orf())

    assert result.artist == "FSL-07"
    assert result.make == "OLYMPUS CORPORATION"
    assert result.model == "TG-6"


def test_trailing_padding_is_stripped():
    """Olympus space-pads its ASCII fields. Unstripped, `Artist` would never
    match a `Camera.name`."""
    result = _read(build_orf(artist="FSL-07                    "))

    assert result.artist == "FSL-07"


# ── robustness ────────────────────────────────────────────────────────


def test_big_endian_files_parse():
    """`MM` byte order. No Olympus body writes it, but the reader keys on the
    header rather than assuming, and assuming is how a future camera silently
    yields garbage."""
    data = build_orf(endian=">", date_time="2024:08:21 08:56:51")

    assert _read(data).date_time == "2024:08:21 08:56:51"


def test_a_ranged_read_of_the_first_megabyte_is_enough():
    """What makes the dry-run affordable: ~1 MB per file instead of ~15 MB.
    The reader must work on a truncated buffer, not demand the whole file."""
    data = build_orf()
    truncated = data[: 1024 * 1024]

    assert _read(truncated).date_time == "2025:03:06 17:00:15"


def test_bytes_that_are_not_tiff_return_an_empty_result_rather_than_raising():
    """A NAS hiccup can hand back a JSON error body instead of image bytes —
    that exact failure corrupted stage 2 in 2026-05. The reader reports
    "nothing here" and lets the caller reject the frame."""
    result = _read(b"not a tiff at all, this is an error page")

    assert result.date_time is None
    assert result.serial_number is None


def test_truncated_mid_header_does_not_raise():
    data = build_orf()[:9]

    assert _read(data).date_time is None


# ── the real thing ────────────────────────────────────────────────────


@pytest.mark.skipif(not _REAL_ORF.exists(), reason="ORF fixture not present")
def test_parity_against_a_real_olympus_orf():
    """Synthetic bytes prove the parser handles a shape; this proves the shape
    is the one Olympus actually writes.

    Values are from the committed data-worker fixture, a real TG-6 frame.
    """
    result = _read(_REAL_ORF.read_bytes()[: 1024 * 1024])

    assert result.date_time == "2025:03:06 17:00:15"
    assert result.offset_time == "-08:00"
    assert result.artist == "FSL-07"
    assert result.model == "TG-6"
    assert result.serial_number == "BJ6C67989"
