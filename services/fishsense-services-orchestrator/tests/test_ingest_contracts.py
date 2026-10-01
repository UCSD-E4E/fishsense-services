"""The ingest contract: request, preflight, progress, report.

Ported from fishsense-lite@a8b2c3bc libs/fishsense-shared/tests/
test_ingest_contracts.py (the ingest half; the checksum-verification contracts
port with their workflows). v1's tests and reasons are kept. Where v2 changes
the contract, the test says so and why: a request names its **tenant**, and
refers to things the way an operator knows them -- a device serial, a slate
template name, a calibration source's NAS path -- instead of v1 row ids.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from fishsense_services_orchestrator.ingest.contracts import (
    DuplicateOverlap,
    IngestDiveRequest,
    IngestPreflight,
    IngestProgress,
    IngestReport,
    PreflightImage,
    RejectedImage,
    SubfolderReport,
)


def test_a_request_needs_only_a_tenant_and_a_dive_path():
    """Everything else has a defensible default; the folder is the one thing
    only the operator knows. v2: so is the tenant the dive belongs to."""
    request = IngestDiveRequest(tenant="lab", dive_path="2024 REEF/082124_FSL06")

    assert request.dive_path == "2024 REEF/082124_FSL06"
    assert request.dive_name is None
    with pytest.raises(ValidationError):
        IngestDiveRequest(dive_path="d")


def test_priority_defaults_to_high():
    """A dive ingested at LOW is invisible to every cohort selector, so it
    would sit there looking successful and never process."""
    assert IngestDiveRequest(tenant="lab", dive_path="d").priority == "high"


def test_priority_is_high_or_low_only():
    """v2: v2's lowercase priorities, and never `none` -- parking a dive is a
    deliberate act on an existing dive, not an ingest option."""
    with pytest.raises(ValidationError):
        IngestDiveRequest(tenant="lab", dive_path="d", priority="none")


def test_calibration_intent_defaults_to_neither():
    """Deliberately not defaulted to `self_calibrates=True`.

    A fish-only dive with no slate frames can never self-calibrate, and that is
    not detectable from the files -- so preflight requires the operator to state
    intent. Defaulting either way would silently pick for them.
    v2: the source dive is named by its NAS path, not a v1 row id.
    """
    request = IngestDiveRequest(tenant="lab", dive_path="d")

    assert request.calibration_source_path is None
    assert request.self_calibrates is False


def test_overrides_name_things_the_way_an_operator_knows_them():
    """v2: a device serial and a slate template name, not v1 row ids."""
    request = IngestDiveRequest(
        tenant="lab", dive_path="d", device_serial="BJ6C67989", slate_template="H-Slate"
    )

    assert (request.device_serial, request.slate_template) == ("BJ6C67989", "H-Slate")


def test_writing_nothing_is_opt_in():
    """`dry_run` defaults False: the common case is an operator who means to
    ingest. Preflight still runs either way, so a fault is caught before
    anything is written regardless."""
    request = IngestDiveRequest(tenant="lab", dive_path="d", self_calibrates=True)

    assert request.dry_run is False


def test_the_request_carries_no_verify_existing_flag():
    """Removed in v1 (#618), not renamed: declared and honoured by no code, so
    setting it produced a normal ingest and no warning. Pydantic ignores
    unknown keys, so assert the field is genuinely gone."""
    stale = IngestDiveRequest(
        tenant="lab", dive_path="d", self_calibrates=True, verify_existing=True
    )

    assert not hasattr(stale, "verify_existing")
    assert "verify_existing" not in stale.model_dump()


def test_a_preflight_image_may_have_no_timestamp():
    """None means "reject this frame", never "use a default" -- stage-1
    clustering is pure timestamp maths."""
    image = PreflightImage(path="d/P1.ORF", size=15_232_982)

    assert image.taken_datetime is None
    assert image.exif_offset is None


def test_preflight_image_records_the_offset_without_applying_it():
    """The camera writes local time plus an offset; the existing rows store the
    local value stamped UTC. Ingest reproduces that, but keeps the offset
    visible so the divergence is reported rather than lost."""
    image = PreflightImage(
        path="d/P1.ORF",
        size=1,
        taken_datetime=datetime(2025, 3, 6, 17, 0, 15, tzinfo=timezone.utc),
        exif_offset="-08:00",
    )

    assert image.exif_offset == "-08:00"
    assert image.taken_datetime.hour == 17  # not shifted by the offset


def test_a_fresh_preflight_reports_no_problems():
    preflight = IngestPreflight(dive_path="d")

    assert not preflight.errors
    assert not preflight.warnings
    assert not preflight.images
    assert not preflight.subfolders


def test_subfolders_are_reported_as_separate_dives():
    """The Olympus rollover case. Reported, never ingested -- recursing would
    merge dives that are distinct rows."""
    report = SubfolderReport(path="d/101923_Alligator1_FSL06", orf_count=47)

    assert report.orf_count == 47


def test_duplicate_overlap_is_a_containment_ratio():
    """A set operation over checksums, so it degrades to a partial overlap
    instead of the legacy digest's all-or-nothing answer. v2: the other dive is
    named by id and path."""
    overlap = DuplicateOverlap(
        dive_id=uuid.uuid4(), dive_path="x", shared_images=48, containment=48 / 55
    )

    assert overlap.containment == pytest.approx(0.8727, abs=1e-4)


def test_a_report_is_uncommitted_until_proven_otherwise():
    """`committed` is the commit flag: priority only flips when every listed
    frame landed. A partially ingested dive must never enter the pipeline, so
    the default has to be False."""
    report = IngestReport(dive_path="d")

    assert report.committed is False
    assert report.dive_id is None
    assert not report.rejected


def test_a_rejection_carries_its_reason():
    rejected = RejectedImage(path="d/P1.ORF", reason="no EXIF DateTime")

    assert rejected.reason == "no EXIF DateTime"


def test_progress_starts_empty_so_a_poller_can_read_it_immediately():
    progress = IngestProgress()

    assert progress.state == "starting"
    assert (progress.total, progress.scanned, progress.registered) == (0, 0, 0)


def test_the_contract_round_trips_through_json():
    """Temporal serializes these across the process boundary. A field that
    cannot round-trip fails in production, not here."""
    report = IngestReport(
        dive_path="d",
        dive_id=uuid.uuid4(),
        total=2,
        registered=2,
        dive_datetime=datetime(2024, 8, 21, 8, 56, 51, tzinfo=timezone.utc),
        committed=True,
        rejected=[RejectedImage(path="x", reason="y")],
        duplicate_overlap=[
            DuplicateOverlap(
                dive_id=uuid.uuid4(), dive_path="o", shared_images=1, containment=0.5
            )
        ],
    )

    restored = IngestReport.model_validate_json(report.model_dump_json())

    assert restored == report
