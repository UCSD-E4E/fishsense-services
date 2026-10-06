"""The automatic-results track, the orchestrator's activities.

New in v2: select the backlog's oldest dive across tenants, hand each stage
its inputs (the orchestrator issues every key), and check and write back what
the processor answered. Built as head/tail prediction's and species
prediction's are (test_headtail_activities.py,
test_species_prediction_activities.py), and pinned here:

* the frames step gets each frame's staged raw and the head/tail stage's JPEG
  key -- where it already is (left alone), else the tenant's (written) -- and
  the laser stage's expected-laser region;
* species reuse the species stage's processor contract, cropped by the
  **automatic** mask and naming the automatic head/tail; a JPEG not yet in
  Garage defers the fish;
* the calibration step gets the dive's slate frames whose JPEG exists, and
  every automatic dot;
* a length is written with the calibration it was computed under (resolved
  once, carried to the write), never the one current at write time;
* a refusal of the processor's output is final (non-retryable).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.automatic_results_store import (
    AutomaticCalibrationCandidate,
    AutomaticCalibrationInputs,
    AutomaticCandidate,
    AutomaticFrame,
    AutomaticFramesInputs,
    AutomaticMeasureCapture,
    AutomaticMeasureInputs,
    AutomaticSpeciesCapture,
    ForeignCapture,
    MeasurementCalibration,
)
from fishsense_services_contracts.automatic_results import (
    AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
    AutomaticCalibrationResult,
    AutomaticFrameResult,
    AutomaticLength,
    MeasureAutomaticResult,
)
from fishsense_services_contracts.laser_region import LASER_REGION_POLYGON
from fishsense_services_contracts.object_store import HEADTAIL_JPEG_FOLDER, ObjectRef
from fishsense_services_contracts.species_prediction import SpeciesPredictionResult
from fishsense_services_orchestrator.automatic_results.activities import (
    AutomaticResultsActivities,
    AutomaticTarget,
)

TENANT_A, TENANT_B = uuid.uuid4(), uuid.uuid4()
DIVE = uuid.uuid4()
TARGET = AutomaticTarget(TENANT_A, DIVE)
K = [[3000.0, 0.0, 2048.0], [0.0, 3000.0, 1536.0], [0.0, 0.0, 1.0]]
T0 = datetime(2026, 1, 1, tzinfo=UTC)


class _Catalog:
    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    async def member_tenants(self):
        return [TENANT_A, TENANT_B]

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            answer = self.answers.get(name)
            if isinstance(answer, Exception):
                raise answer
            return answer(*args, **kwargs) if callable(answer) else answer

        return call


class _Store:
    """Keys as the layout writes them; `existing` are already in Garage."""

    def __init__(self, existing=()):
        self.existing = set(existing)
        self.layout = SimpleNamespace(
            raw=lambda t, c: ObjectRef(
                bucket="scratch", key=f"tenants/{t}/raw/{c}.ORF"
            ),
            processed_jpeg=lambda t, f, c: ObjectRef(
                bucket="labels", key=f"tenants/{t}/{f}/{c}.JPG"
            ),
        )

    async def locate_processed_jpeg(self, tenant_id, folder, checksum, *, from_v1):
        assert folder == HEADTAIL_JPEG_FOLDER
        if checksum in self.existing:
            return ObjectRef(
                bucket="labels", key=f"fishsense-lite/{folder}/{checksum}.JPG"
            )
        return None


def _acts(catalog, store=None):
    return AutomaticResultsActivities(catalog=catalog, store=store or _Store())


async def _run(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


# -- select -------------------------------------------------------------------------


async def test_the_oldest_backlog_dive_across_tenants():
    newer, older = uuid.uuid4(), uuid.uuid4()
    catalog = _Catalog(
        next_dive_for_automatic_results=lambda tenant, **v: AutomaticCandidate(
            newer if tenant == TENANT_A else older,
            T0.replace(day=2) if tenant == TENANT_A else T0,
        )
    )
    got = await _run(_acts(catalog).select_next_dive_for_automatic_results)
    assert got == AutomaticTarget(TENANT_B, older)


async def test_no_backlog_is_none():
    catalog = _Catalog(next_dive_for_automatic_results=None)
    assert await _run(_acts(catalog).select_next_dive_for_automatic_results) is None


# -- frames -------------------------------------------------------------------------


async def test_frames_carry_their_raw_their_jpeg_key_and_the_slate_score():
    have, new = "aa" * 16, "bb" * 16
    frames = [
        AutomaticFrame(uuid.uuid4(), have, True, 0.9),
        AutomaticFrame(uuid.uuid4(), new, False, None),
    ]
    catalog = _Catalog(
        automatic_frames_inputs=AutomaticFramesInputs(K, [0.1] * 5, frames)
    )

    got = await _run(
        _acts(catalog, _Store([have])).resolve_automatic_frames_inputs, TARGET
    )

    a, b = got.frames
    assert a.raw.key.endswith(f"{have}.ORF") and not a.write_jpeg
    assert a.jpeg.key == f"fishsense-lite/{HEADTAIL_JPEG_FOLDER}/{have}.JPG"
    assert a.is_slate and a.slate_probability == 0.9
    assert (
        b.write_jpeg
        and b.jpeg.key == f"tenants/{TENANT_A}/{HEADTAIL_JPEG_FOLDER}/{new}.JPG"
    )
    assert not b.is_slate
    assert got.laser_region == [list(v) for v in LASER_REGION_POLYGON]
    assert got.camera_matrix == K


def _frame_result(status="predicted", **extra):
    fields = dict(capture_id=uuid.uuid4(), status=status,
                  predictor_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION)  # fmt: skip
    fields.update(extra)
    return AutomaticFrameResult(**fields)


async def test_frame_results_are_written_as_automatic_rows():
    catalog = _Catalog(persist_automatic_head_tails=lambda t, d, rows: len(rows))
    r = _frame_result("slate_frame", laser_x=1.0, laser_y=2.0, slate_probability=0.8)

    assert await _run(_acts(catalog).persist_automatic_frames, TARGET, [r]) == 1
    ((_, (tenant, dive, rows), _),) = catalog.calls
    assert (tenant, dive) == (TENANT_A, DIVE)
    assert rows[0].status == "slate_frame" and rows[0].slate_probability == 0.8


@pytest.mark.parametrize(
    "bad",
    [
        _frame_result("skipped_no_upgrade_available"),
        _frame_result(predictor_version=-1),
    ],
)
async def test_a_result_that_is_no_automatic_row_is_refused(bad):
    catalog = _Catalog(persist_automatic_head_tails=1)
    with pytest.raises(ApplicationError) as error:
        await _run(_acts(catalog).persist_automatic_frames, TARGET, [bad])
    assert error.value.non_retryable and error.value.type == "InvalidPredictions"
    assert catalog.calls == []


async def test_a_store_refusal_is_final():
    catalog = _Catalog(persist_automatic_head_tails=ForeignCapture("not mine"))
    with pytest.raises(ApplicationError) as error:
        await _run(_acts(catalog).persist_automatic_frames, TARGET, [_frame_result()])
    assert error.value.non_retryable


# -- species ------------------------------------------------------------------------


async def test_species_are_cropped_by_the_automatic_mask():
    ht = uuid.uuid4()
    have, missing = "cc" * 16, "dd" * 16
    catalog = _Catalog(automatic_species_captures=[
        AutomaticSpeciesCapture(uuid.uuid4(), have, True, ht, [1, 2, 30, 40], False),
        AutomaticSpeciesCapture(uuid.uuid4(), missing, False, uuid.uuid4(), [1, 2, 3, 4],
                                False),
    ])  # fmt: skip

    got = await _run(
        _acts(catalog, _Store([have])).resolve_automatic_species_inputs, TARGET
    )

    (image,) = got.images
    assert image.headtail_prediction_id == ht and image.mask_bbox == [1, 2, 30, 40]
    assert got.candidates  # the species labeling config's target species


async def test_a_species_outside_the_candidates_is_refused():
    catalog = _Catalog(persist_automatic_species=1)
    bad = SpeciesPredictionResult(
        capture_id=uuid.uuid4(), headtail_prediction_id=uuid.uuid4(),
        status="predicted", predicted_choice="Fish, Nemo (Amphiprion nemo)",
        top1_probability=0.9, margin=0.5, predictor_version=1, model_id="m",
    )  # fmt: skip
    with pytest.raises(ApplicationError):
        await _run(_acts(catalog).persist_automatic_species, TARGET, [bad])
    assert catalog.calls == []


# -- calibration and lengths ------------------------------------------------------


async def test_calibration_frames_are_slate_frames_whose_jpeg_exists():
    have, missing = "ee" * 16, "ff" * 16
    a = AutomaticCalibrationCandidate(uuid.uuid4(), have, True, 10.0, 20.0)
    b = AutomaticCalibrationCandidate(uuid.uuid4(), missing, False, 11.0, 21.0)
    camera = uuid.uuid4()
    catalog = _Catalog(automatic_calibration_inputs=AutomaticCalibrationInputs(
        camera, K, [a, b], [[10.0, 20.0], [11.0, 21.0], [500.0, 600.0]]))  # fmt: skip

    got = await _run(
        _acts(catalog, _Store([have])).resolve_automatic_calibration_inputs, TARGET
    )

    frames = got.payload.frames
    assert [(f.capture_id, f.x, f.y) for f in frames] == [(a.capture_id, 10.0, 20.0)]
    assert len(got.payload.line_dots) == 3
    assert got.camera_calibration_id == camera


async def test_a_calibration_is_written_with_its_camera():
    camera = uuid.uuid4()
    catalog = _Catalog(
        automatic_calibration_inputs=AutomaticCalibrationInputs(camera, K, [], []),
        persist_automatic_calibration=lambda t, d, row: uuid.uuid4(),
    )
    result = AutomaticCalibrationResult(
        dive_id=DIVE, outcome="refused", refusal_reason="no_candidates",
        algorithm_version="1",
    )  # fmt: skip

    acts = _acts(catalog)
    plan = await _run(acts.resolve_automatic_calibration_inputs, TARGET)
    await _run(acts.persist_automatic_calibration, TARGET, plan, result)

    row = catalog.calls[-1][1][2]
    assert (row.outcome, row.refusal_reason, row.camera_calibration_id) == (
        "refused", "no_candidates", camera,
    )  # fmt: skip


async def test_lengths_are_written_under_the_calibration_they_were_computed_with():
    ht, capture, stored, camera = (uuid.uuid4() for _ in range(4))
    catalog = _Catalog(
        automatic_measure_inputs=AutomaticMeasureInputs(
            MeasurementCalibration("stored", None, stored, [0.1, 0, 0], [0, 0, 1]),
            camera, K,
            [AutomaticMeasureCapture(capture, ht, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0)],
        ),
        persist_automatic_measurements=lambda t, d, rows: len(rows),
    )  # fmt: skip
    acts = _acts(catalog)

    plan = await _run(acts.resolve_automatic_measure_inputs, TARGET)
    assert plan.payload.laser_position == [0.1, 0, 0]
    result = MeasureAutomaticResult(
        dive_id=DIVE, algorithm="laser_depth_fronto_parallel", algorithm_version="1",
        core_version="4.1.0",
        lengths=[AutomaticLength(capture_id=capture,
                                 automatic_head_tail_prediction_id=ht,
                                 length_m=0.4, depth_m=2.0)],
    )  # fmt: skip
    assert await _run(acts.persist_automatic_measurements, TARGET, plan, result) == 1

    (row,) = catalog.calls[-1][1][2]
    assert (row.calibration_source, row.laser_calibration_id) == ("stored", stored)
    assert row.automatic_laser_calibration_id is None
    assert row.camera_calibration_id == camera and row.length_m == 0.4


async def test_no_calibration_is_no_plan():
    catalog = _Catalog(
        automatic_measure_inputs=AutomaticMeasureInputs(None, None, None, [])
    )
    assert await _run(_acts(catalog).resolve_automatic_measure_inputs, TARGET) is None
