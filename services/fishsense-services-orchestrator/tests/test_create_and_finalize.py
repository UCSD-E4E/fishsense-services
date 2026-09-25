"""`create_dive` and `finalize_dive` -- the commit protocol.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/tests/
test_create_and_finalize_dive_activities.py. Test names, bodies and reasons are
v1's; the harness changed (a recording fake **catalog** stands in for v1's fake
API client). v2 adaptations are marked: ids are the ones preflight resolved,
and finalize takes the tenant.

Ingest writes a dive twice, and the pair is a two-phase commit against a
table that has no transactions across activities:

  * **create** writes the dive at `low`, *whatever the request asked for*. Low
    keeps it out of every hourly cohort, so a half-ingested dive cannot be
    picked up and processed.
  * **finalize** flips it to the requested priority -- but only if every listed
    frame landed. **Priority is the commit flag.**

That is what makes a crashed or retried ingest safe: the dive exists, its images
exist, and the pipeline ignores all of it until someone can say the set is
complete.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.ingest_store import ContentOverlap
from fishsense_services_orchestrator.ingest.activities import (
    INCOMPLETE_INGEST_TYPE,
    IngestActivities,
    IngestTotals,
)
from fishsense_services_orchestrator.ingest.contracts import (
    IngestDiveRequest,
    IngestPreflight,
    PreflightImage,
    RejectedImage,
)
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings

FOLDER = "2024.06.20.REEF/082929_FishModels_FSL07"
T0 = datetime(2024, 8, 21, 8, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2024, 8, 21, 9, 30, 0, tzinfo=timezone.utc)
TENANT = uuid.uuid4()
DEVICE = uuid.uuid4()
DIVE = uuid.uuid4()
OTHER_DIVE = uuid.uuid4()


class RecordingCatalog:
    """Records the writes; answers overlap from memory."""

    def __init__(self, overlap=()):
        self.created: list[dict] = []
        self.finalized: list[tuple] = []
        self.overlap = list(overlap)

    async def create_dive(self, tenant_id, **dive):
        self.created.append({"tenant_id": tenant_id, **dive})
        return DIVE

    async def finalize_dive(self, tenant_id, dive_id, **fields):
        self.finalized.append((tenant_id, dive_id, fields))

    async def content_overlap(self, tenant_id, dive_id):
        return self.overlap


def _request(**kwargs):
    kwargs.setdefault("tenant", "lab")
    kwargs.setdefault("dive_path", FOLDER)
    kwargs.setdefault("self_calibrates", True)
    return IngestDiveRequest(**kwargs)


def _preflight(**kwargs):
    kwargs.setdefault("dive_path", f"/fishsense_data/REEF/data/{FOLDER}")
    kwargs.setdefault("tenant_id", TENANT)
    kwargs.setdefault("resolved_device_id", DEVICE)
    kwargs.setdefault(
        "images",
        [
            PreflightImage(path=f"{FOLDER}/A.ORF", size=1, taken_datetime=T0),
            PreflightImage(path=f"{FOLDER}/B.ORF", size=1, taken_datetime=T1),
        ],
    )
    return IngestPreflight(**kwargs)


def _totals(**kwargs):
    kwargs.setdefault("total", 2)
    kwargs.setdefault("registered", 2)
    kwargs.setdefault("skipped_existing", 0)
    kwargs.setdefault("rejected", [])
    kwargs.setdefault("max_taken_datetime", T1)
    return IngestTotals(**kwargs)


def _activities(catalog):
    return IngestActivities(
        nas_settings=NasSettings(url="https://nas.test:6021", username="u",
                                 password="p", raw_root_path="/fishsense_data"),
        nas_client_factory=lambda: None,
        catalog=catalog,
    )  # fmt: skip


async def _create(request, preflight, catalog):
    return await ActivityEnvironment().run(
        _activities(catalog).create_dive, request, preflight
    )


async def _finalize(dive_id, request, totals, catalog):
    return await ActivityEnvironment().run(
        _activities(catalog).finalize_dive, TENANT, dive_id, request, totals
    )


# -- create: low is not negotiable -------------------------------------------


async def test_creates_the_dive_at_low_even_when_high_was_requested():
    """The whole safety property. High is what the hourly cohorts select on, so
    a dive created high before its images land would be picked up mid-ingest and
    processed against a partial set.

    v2: the store function *cannot* create at anything but low -- it takes no
    priority -- so this pins that the activity passes none."""
    catalog = RecordingCatalog()

    dive_id = await _create(_request(priority="high"), _preflight(), catalog)

    assert dive_id == DIVE
    (created,) = catalog.created
    assert "priority" not in created
    assert created["tenant_id"] == TENANT


async def test_seeds_a_provisional_dive_datetime_from_preflight():
    """`dived_at` is NOT NULL, so create needs a value before any frame has
    been hashed. Preflight already read every header, so use its max -- finalize
    replaces it with the scan's."""
    catalog = RecordingCatalog()

    await _create(_request(), _preflight(), catalog)

    assert catalog.created[0]["dived_at"] == T1


async def test_refuses_to_fabricate_a_dive_datetime():
    catalog = RecordingCatalog()

    with pytest.raises(ApplicationError) as excinfo:
        await _create(_request(), _preflight(images=[]), catalog)

    assert excinfo.value.non_retryable
    assert catalog.created == []


async def test_refuses_to_create_from_a_failed_preflight():
    """v2: the workflow gates on preflight errors; create does too, so no
    caller can skip the gate and write a dive preflight refused."""
    catalog = RecordingCatalog()

    with pytest.raises(ApplicationError) as excinfo:
        await _create(_request(), _preflight(errors=["no intrinsics"]), catalog)

    assert excinfo.value.non_retryable
    assert catalog.created == []


async def test_defaults_the_name_to_the_leaf_directory():
    """The dive's name feeds the per-dive Label Studio project title, so an
    unnamed dive gets a title of just its id."""
    catalog = RecordingCatalog()

    await _create(_request(), _preflight(), catalog)

    assert catalog.created[0]["name"] == "082929_FishModels_FSL07"


async def test_an_explicit_name_wins():
    catalog = RecordingCatalog()

    await _create(_request(dive_name="Reef dive 3"), _preflight(), catalog)

    assert catalog.created[0]["name"] == "Reef dive 3"


async def test_carries_the_camera_and_calibration_intent():
    """v2: the ids preflight resolved -- device from the serial, slate template
    from its name, calibration source from its path."""
    catalog = RecordingCatalog()
    slate, source = uuid.uuid4(), uuid.uuid4()

    await _create(
        _request(self_calibrates=False, calibration_source_path="x/y",
                 slate_template="H-Slate", flip_dive_slate=True),
        _preflight(resolved_slate_template_id=slate,
                   resolved_calibration_source_dive_id=source),
        catalog,
    )  # fmt: skip

    created = catalog.created[0]
    assert created["device_id"] == DEVICE
    assert created["calibration_source_dive_id"] == source
    assert created["slate_template_id"] == slate
    assert created["flip_dive_slate"] is True
    assert created["source_path"] == FOLDER


async def test_a_self_calibrating_dive_has_no_calibration_link():
    """NULL means "self-calibrate". Writing a link here would make the dive
    borrow a calibration it does not need -- own-wins resolution would ignore
    it, but the row would be lying."""
    catalog = RecordingCatalog()

    await _create(_request(self_calibrates=True), _preflight(), catalog)

    assert catalog.created[0]["calibration_source_dive_id"] is None


# -- finalize: priority is the commit flag -----------------------------------


async def test_promotes_to_the_requested_priority_when_everything_landed():
    catalog = RecordingCatalog()

    report = await _finalize(DIVE, _request(priority="high"), _totals(), catalog)

    assert catalog.finalized == [(TENANT, DIVE, {"priority": "high", "dived_at": T1})]
    assert report.committed is True
    assert report.dive_id == DIVE


async def test_finalize_writes_only_the_commit_flag_and_the_datetime():
    """v1 needed a long test here: its promote went through a whole-row upsert
    that could null the camera and leave the dive at low forever. v2's finalize
    writes exactly two columns, so the pin is that it asks for no more."""
    catalog = RecordingCatalog()

    await _finalize(DIVE, _request(priority="low"), _totals(), catalog)

    ((_, _, fields),) = catalog.finalized
    assert set(fields) == {"priority", "dived_at"}


async def test_refuses_when_a_frame_was_rejected():
    """A partially-ingested dive must never enter the pipeline. Non-retryable:
    a rejected frame is a data problem, and retrying re-reads the same bytes to
    the same conclusion."""
    catalog = RecordingCatalog()
    totals = _totals(
        registered=1, rejected=[RejectedImage(path="x", reason="no timestamp")]
    )

    with pytest.raises(ApplicationError) as excinfo:
        await _finalize(DIVE, _request(), totals, catalog)

    assert excinfo.value.non_retryable
    assert excinfo.value.type == INCOMPLETE_INGEST_TYPE
    assert catalog.finalized == []


async def test_refuses_when_the_counts_do_not_add_up():
    """registered + skipped must equal total. A gap means a frame was neither
    written nor recognised as already present -- silence rather than a
    rejection, which is worse."""
    catalog = RecordingCatalog()

    with pytest.raises(ApplicationError) as excinfo:
        await _finalize(DIVE, _request(), _totals(registered=1), catalog)

    assert excinfo.value.non_retryable
    assert catalog.finalized == []


async def test_a_dive_that_was_entirely_skipped_still_commits():
    """Re-running a completed ingest is a no-op that must still succeed --
    otherwise the only way to re-verify a dive is to make it fail."""
    catalog = RecordingCatalog()

    report = await _finalize(
        DIVE, _request(), _totals(registered=0, skipped_existing=2), catalog
    )

    assert report.committed is True
    assert report.skipped_existing == 2


# -- finalize: content overlap -----------------------------------------------


async def test_reports_content_overlap_with_an_existing_dive():
    """Layer 2 of duplicate detection, and the reason it runs HERE: it needs
    every frame's checksum, which only exists once the scan has written the
    rows. Containment is |new ∩ existing| / |new| over content hashes, so it is
    immune to filenames and ordering -- the property the legacy whole-dive MD5
    digest lacked.

    v2: the arithmetic (and excluding the dive itself) is the store's, tested
    there against Postgres; this pins that the report carries it."""
    catalog = RecordingCatalog(
        overlap=[ContentOverlap(OTHER_DIVE, "backup/082929_FishModels_FSL07", 1, 0.5)]
    )

    report = await _finalize(DIVE, _request(), _totals(), catalog)

    assert len(report.duplicate_overlap) == 1
    overlap = report.duplicate_overlap[0]
    assert overlap.dive_id == OTHER_DIVE
    assert overlap.dive_path == "backup/082929_FishModels_FSL07"
    assert overlap.shared_images == 1
    assert overlap.containment == pytest.approx(0.5)


async def test_a_dive_with_no_overlap_reports_none():
    report = await _finalize(
        DIVE, _request(), _totals(total=1, registered=1), RecordingCatalog()
    )

    assert report.duplicate_overlap == []
