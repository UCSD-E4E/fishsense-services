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
    AUTOMATIC_JPEG_RENDER_VERSION,
    AutomaticCalibrationResult,
    AutomaticFrameResult,
    AutomaticLength,
    MeasureAutomaticResult,
)
from fishsense_services_contracts.laser_region import LASER_REGION_POLYGON
from fishsense_services_contracts.object_store import (
    HEADTAIL_JPEG_FOLDER,
    ObjectRef,
)
from fishsense_services_contracts.renders import render_key
from fishsense_services_api.renders_store import RenderAt
from fishsense_services_contracts.species_prediction import SpeciesPredictionResult
from fishsense_services_orchestrator.automatic_results.activities import (
    AutomaticResultsActivities,
    AutomaticTarget,
)

TENANT_A, TENANT_B = uuid.uuid4(), uuid.uuid4()
MISSION = uuid.uuid4()
TARGET = AutomaticTarget(TENANT_A, MISSION)
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
    """Keys as the layout writes them. `existing` have a head/tail JPEG at v1's
    key (the image human labels were drawn on); `automatic` are the automatic
    renders in Garage, as ``(folder, checksum)``."""

    def __init__(self, existing=(), automatic=()):
        self.existing = set(existing)
        self.automatic = set(automatic)
        self.layout = SimpleNamespace(
            raw=lambda t, c: ObjectRef(
                bucket="scratch", key=f"tenants/{t}/raw/{c}.ORF"
            ),
            processed_jpeg=lambda t, f, c: ObjectRef(
                bucket="labels", key=f"tenants/{t}/{f}/{c}.JPG"
            ),
        )

    async def locate_processed_jpeg(self, tenant_id, folder, checksum, *, from_v1):
        if folder.startswith("automatic_jpeg_r"):
            assert not from_v1  # v1 never wrote one
            if (folder, checksum) in self.automatic:
                return self.layout.processed_jpeg(tenant_id, folder, checksum)
            return None
        assert folder == HEADTAIL_JPEG_FOLDER
        if checksum in self.existing:
            return ObjectRef(
                bucket="labels", key=f"fishsense-lite/{folder}/{checksum}.JPG"
            )
        return None


CAMERA = uuid.uuid4()


class _Renders:
    """The captures' renders by version and camera (`renders_store.renders_at`),
    and presigned GETs of them in the tenant's bucket."""

    def __init__(self, *rows):
        self.rows = rows
        self.presigners = 0

    async def at(self, tenant_id, capture_ids, *, render_version,
                 camera_calibration_id):  # fmt: skip
        return {r.capture_id: r for r in self.rows
                if r.capture_id in capture_ids
                and (r.render_version, r.camera_calibration_id)
                == (render_version, camera_calibration_id)}  # fmt: skip

    async def presigner(self, tenant_id):
        self.presigners += 1
        return lambda key: f"https://garage/fishsense-tenant-lab/{key}?sig"

    #: The tenant's bucket (`renders.urls.TenantRenderUrls.objects`), for
    #: stored masks; None, no bucket.
    bucket = None

    async def objects(self, tenant_id):
        return self.bucket


def _render(capture_id, checksum, *, camera=CAMERA, version=AUTOMATIC_JPEG_RENDER_VERSION,
            status="rendered", settled=True):  # fmt: skip
    key = render_key(version, camera, checksum) if status == "rendered" else None
    bucket = "fishsense-tenant-lab" if key else None
    return RenderAt(capture_id, status, settled, bucket, key, version, camera)


def _url(checksum, *, camera=CAMERA, version=AUTOMATIC_JPEG_RENDER_VERSION):
    return f"https://garage/fishsense-tenant-lab/{render_key(version, camera, checksum)}?sig"


def _acts(catalog, store=None, renders=None, **settings):
    return AutomaticResultsActivities(
        catalog=catalog,
        store=store or _Store(),
        renders=renders or _Renders(),
        **settings,
    )


async def _run(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


# -- select -------------------------------------------------------------------------


async def test_the_oldest_backlog_mission_across_tenants():
    newer, older = uuid.uuid4(), uuid.uuid4()
    catalog = _Catalog(
        next_mission_for_automatic_results=lambda tenant, **v: AutomaticCandidate(
            newer if tenant == TENANT_A else older,
            T0.replace(day=2) if tenant == TENANT_A else T0,
        )
    )
    got = await _run(_acts(catalog).select_next_mission_for_automatic_results)
    assert got == AutomaticTarget(TENANT_B, older)


async def test_species_on_by_default_is_species_work_in_the_cohort():
    catalog = _Catalog(next_mission_for_automatic_results=None)
    await _run(_acts(catalog).select_next_mission_for_automatic_results)
    assert all(kw["species"] is True for _, _, kw in catalog.calls)


async def test_with_species_off_the_cohort_has_no_species_work():
    """Its own switch (2026-10-15): off, species never holds a dive."""
    catalog = _Catalog(next_mission_for_automatic_results=None)
    await _run(
        _acts(catalog, species_enabled=False).select_next_mission_for_automatic_results
    )
    assert [kw["species"] for _, _, kw in catalog.calls] == [False, False]


async def test_with_species_off_there_are_no_species_images():
    catalog = _Catalog(automatic_species_captures=[
        AutomaticSpeciesCapture(uuid.uuid4(), "cc" * 16, True, uuid.uuid4(),
                                [1, 2, 30, 40], False, 1, CAMERA),
    ])  # fmt: skip
    got = await _run(
        _acts(catalog, _Store(["cc" * 16]), species_enabled=False)
        .resolve_automatic_species_inputs, TARGET,
    )  # fmt: skip
    assert got.images == []
    assert not catalog.calls


async def test_no_backlog_is_none():
    catalog = _Catalog(next_mission_for_automatic_results=None)
    assert await _run(_acts(catalog).select_next_mission_for_automatic_results) is None


# -- frames -------------------------------------------------------------------------


async def test_frames_read_their_captures_render_by_url():
    """SAM segments the capture's render -- rectified from the raw by the
    render stage, in the tenant's bucket -- given by presigned GET: the one at
    this version under the mission's current camera, not the newest render
    whatever its camera (review of #144). A frame with none, or one owed again
    (a raw missing from scratch, not yet settled), gets no URL; the workflow
    renders it first. The frames stage writes nothing."""
    have, old, new, retry = (uuid.uuid4() for _ in range(4))
    frames = [
        AutomaticFrame(have, "aa" * 16, True, 0.9),
        AutomaticFrame(old, "bb" * 16, False, None),
        AutomaticFrame(new, "cc" * 16, False, None),
        AutomaticFrame(retry, "dd" * 16, False, None),
    ]
    catalog = _Catalog(
        automatic_frames_inputs=AutomaticFramesInputs(K, [0.1] * 5, frames, CAMERA)
    )
    renders = _Renders(
        _render(have, "aa" * 16), _render(old, "bb" * 16, camera=uuid.uuid4()),
        _render(retry, "dd" * 16, status="raw_unavailable", settled=False),
    )  # fmt: skip

    got = await _run(_acts(catalog, renders=renders).resolve_automatic_frames_inputs,
                     TARGET)  # fmt: skip

    a, b, c, d = got.frames
    assert a.render_url == _url("aa" * 16)
    assert b.render_url is None and c.render_url is None and d.render_url is None
    for f in got.frames:
        assert f.jpeg is None and not f.write_jpeg
        assert f.jpeg_render_version == AUTOMATIC_JPEG_RENDER_VERSION
    assert a.is_slate and a.slate_probability == 0.9
    assert got.laser_region == [list(v) for v in LASER_REGION_POLYGON]
    assert got.camera_matrix == K and got.camera_calibration_id == CAMERA
    assert renders.presigners == 1  # one bucket lookup, not one per frame


async def test_a_large_mission_heartbeats_a_bounded_number_of_times():
    """Temporal's worker queues heartbeats (1,000 at most), and a loop that
    heartbeats per frame without yielding fills it: mission 440 (1,122 frames)
    failed resolving its frames with asyncio's QueueFull (2026-10-20, after
    the bucket lookup stopped awaiting per frame). Once per page of frames."""
    n = 2500
    frames = [AutomaticFrame(uuid.uuid4(), f"{i:032x}", False, None) for i in range(n)]
    catalog = _Catalog(
        automatic_frames_inputs=AutomaticFramesInputs(K, [0.1] * 5, frames, CAMERA)
    )
    beats = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)

    got = await env.run(_acts(catalog).resolve_automatic_frames_inputs, TARGET)

    assert len(got.frames) == n
    assert len(beats) <= n // 250 + 1


async def test_a_frame_whose_render_settled_as_an_abstention_is_one():
    """A raw that would not decode -- or was missing from scratch every time --
    has no render and never will: its frame is recorded as the same
    abstention, so it stops holding its mission (review of #144: it held
    calibration and lengths for good)."""
    bad, gone, fine = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    frames = [AutomaticFrame(c, f"{i:032x}", False, None)
              for i, c in enumerate((bad, gone, fine))]  # fmt: skip
    catalog = _Catalog(
        automatic_frames_inputs=AutomaticFramesInputs(K, [0.1] * 5, frames, CAMERA),
        persist_automatic_head_tails=lambda t, d, rows, *masks: len(rows),
    )
    renders = _Renders(
        _render(bad, f"{0:032x}", status="decode_failed"),
        _render(gone, f"{1:032x}", status="raw_unavailable", settled=True),
        _render(fine, f"{2:032x}"),
    )  # fmt: skip

    got = await _run(_acts(catalog, renders=renders).resolve_automatic_frames_inputs,
                     TARGET)  # fmt: skip

    assert [f.capture_id for f in got.frames] == [fine]
    ((_, (_, _, rows), _),) = [c for c in catalog.calls
                               if c[0] == "persist_automatic_head_tails"]  # fmt: skip
    assert {(r.capture_id, r.status) for r in rows} == {
        (bad, "decode_failed"),
        (gone, "raw_unavailable"),
    }
    assert all(
        r.predictor_version == AUTOMATIC_HEADTAIL_PREDICTOR_VERSION for r in rows
    )


def _frame_result(status="predicted", **extra):
    fields = dict(capture_id=uuid.uuid4(), status=status,
                  predictor_version=AUTOMATIC_HEADTAIL_PREDICTOR_VERSION,
                  jpeg_render_version=AUTOMATIC_JPEG_RENDER_VERSION,
                  jpeg_camera_calibration_id=CAMERA)  # fmt: skip
    fields.update(extra)
    return AutomaticFrameResult(**fields)


async def test_frame_results_are_written_as_automatic_rows():
    catalog = _Catalog(
        persist_automatic_head_tails=lambda t, d, rows, *masks: len(rows)
    )
    r = _frame_result("slate_frame", laser_x=1.0, laser_y=2.0, slate_probability=0.8)

    assert await _run(_acts(catalog).persist_automatic_frames, TARGET, [r]) == 1
    ((_, (tenant, mission, rows, masks), _),) = catalog.calls
    assert masks == []
    assert (tenant, mission) == (TENANT_A, MISSION)
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


async def test_a_frame_found_in_another_render_is_refused():
    """Its pixels belong to a render this orchestrator did not ask for: a
    processor at another render version."""
    catalog = _Catalog(persist_automatic_head_tails=1)
    bad = _frame_result(jpeg_render_version=AUTOMATIC_JPEG_RENDER_VERSION + 1)
    with pytest.raises(ApplicationError) as error:
        await _run(_acts(catalog).persist_automatic_frames, TARGET, [bad])
    assert error.value.non_retryable
    assert catalog.calls == []


async def test_a_rendered_frame_with_no_render_named_is_left_owed():
    """A frame resolved before the deploy (contract 7) comes back rendered but
    with no render named: it is not written -- its pixels are in no automatic
    render -- and not a failure of the dive; it stays owed and the next run
    renders it (review of #136). The rest of the batch is written."""
    written = []
    catalog = _Catalog(
        persist_automatic_head_tails=lambda t, d, rows, *masks: written.extend(rows)
        or len(rows)
    )
    good = _frame_result()
    legacy = [
        _frame_result(jpeg_render_version=None, jpeg_camera_calibration_id=None),
        _frame_result("slate_frame", laser_x=1.0, laser_y=2.0,
                      jpeg_render_version=None, jpeg_camera_calibration_id=None),
        _frame_result("no_laser_dot", jpeg_render_version=None,
                      jpeg_camera_calibration_id=None),
    ]  # fmt: skip

    n = await _run(_acts(catalog).persist_automatic_frames, TARGET, [good, *legacy])

    assert n == 1 and [r.capture_id for r in written] == [good.capture_id]


@pytest.mark.parametrize("status", ["raw_unavailable", "decode_failed"])
async def test_a_frame_with_no_render_needs_no_render_version(status):
    catalog = _Catalog(
        persist_automatic_head_tails=lambda t, d, rows, *masks: len(rows)
    )
    r = _frame_result(status, jpeg_render_version=None, jpeg_camera_calibration_id=None)
    assert await _run(_acts(catalog).persist_automatic_frames, TARGET, [r]) == 1


async def test_a_store_refusal_is_final():
    catalog = _Catalog(persist_automatic_head_tails=ForeignCapture("not mine"))
    with pytest.raises(ApplicationError) as error:
        await _run(_acts(catalog).persist_automatic_frames, TARGET, [_frame_result()])
    assert error.value.non_retryable


# -- species ------------------------------------------------------------------------


async def test_species_are_cropped_by_the_automatic_mask_from_its_render():
    """The mask box is in the pixels of the render it was found in -- the
    version and camera its row records, not today's -- read by URL."""
    ht = uuid.uuid4()
    a, b = uuid.uuid4(), uuid.uuid4()
    old_camera = uuid.uuid4()
    catalog = _Catalog(automatic_species_captures=[
        AutomaticSpeciesCapture(a, "cc" * 16, True, ht, [1, 2, 30, 40], False,
                                AUTOMATIC_JPEG_RENDER_VERSION, CAMERA),
        AutomaticSpeciesCapture(b, "ee" * 16, True, uuid.uuid4(), [1, 2, 3, 4],
                                False, 7, old_camera),
    ])  # fmt: skip
    renders = _Renders(_render(a, "cc" * 16),
                       _render(b, "ee" * 16, camera=old_camera, version=7))  # fmt: skip

    got = await _run(_acts(catalog, renders=renders).resolve_automatic_species_inputs,
                     TARGET)  # fmt: skip

    x, y = got.images
    assert x.headtail_prediction_id == ht and x.mask_bbox == [1, 2, 30, 40]
    assert (x.jpeg, x.jpeg_url) == (None, _url("cc" * 16))
    assert y.jpeg_url == _url("ee" * 16, camera=old_camera, version=7)
    assert got.candidates  # the species labeling config's target species


@pytest.mark.parametrize("recorded", ["none", "another-camera"])
async def test_a_capture_whose_named_render_is_not_recorded_fails_the_species_step(
    recorded,
):
    """Not deferred: its row names a render nobody recorded -- none at all, or
    only one under another camera (review of #144: that presigned a key that
    did not exist) -- which would hold its mission in the cohort for good.
    Failed, it is a strike, and visible."""
    c = uuid.uuid4()
    catalog = _Catalog(automatic_species_captures=[
        AutomaticSpeciesCapture(c, "cc" * 16, True, uuid.uuid4(), [1, 2, 30, 40],
                                False, 1, CAMERA),
    ])  # fmt: skip
    renders = _Renders(*(
        [] if recorded == "none" else [_render(c, "cc" * 16, camera=uuid.uuid4())]
    ))  # fmt: skip
    with pytest.raises(ApplicationError) as error:
        await _run(_acts(catalog, renders=renders).resolve_automatic_species_inputs,
                   TARGET)  # fmt: skip
    assert error.value.type == "AutomaticRenderMissing" and error.value.non_retryable


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


async def test_calibration_frames_are_slate_frames_rendered_with_the_fits_camera(
    caplog,
):
    """Features are taken from the render the dot was found against, read by
    URL, and the fit reads them through the mission's current camera: a frame
    with no render, or rendered under another camera, is left out, and the log
    says how many (review of #136)."""
    a = AutomaticCalibrationCandidate(
        uuid.uuid4(), "ee" * 16, True, 10.0, 20.0, AUTOMATIC_JPEG_RENDER_VERSION, CAMERA
    )
    b = AutomaticCalibrationCandidate(uuid.uuid4(), "ff" * 16, True, 11.0, 21.0)
    c = AutomaticCalibrationCandidate(
        uuid.uuid4(),
        "dd" * 16,
        True,
        12.0,
        22.0,
        AUTOMATIC_JPEG_RENDER_VERSION,
        uuid.uuid4(),
    )
    catalog = _Catalog(
        automatic_calibration_inputs=AutomaticCalibrationInputs(
            CAMERA, K, [a, b, c], [[10.0, 20.0], [11.0, 21.0], [500.0, 600.0]]
        )
    )
    renders = _Renders(_render(a.capture_id, "ee" * 16))  # fmt: skip

    with caplog.at_level("WARNING"):
        got = await _run(_acts(catalog, renders=renders)
                         .resolve_automatic_calibration_inputs, TARGET)  # fmt: skip

    frames = got.payload.frames
    assert [(f.capture_id, f.x, f.y) for f in frames] == [(a.capture_id, 10.0, 20.0)]
    assert (frames[0].jpeg, frames[0].jpeg_url) == (None, _url("ee" * 16))
    assert len(got.payload.line_dots) == 3
    assert got.camera_calibration_id == CAMERA
    assert "1 with no render" in caplog.text and "1 rendered under" in caplog.text


async def test_a_capture_with_no_render_recorded_fails_the_calibration_step():
    a = AutomaticCalibrationCandidate(
        uuid.uuid4(), "ee" * 16, True, 10.0, 20.0, AUTOMATIC_JPEG_RENDER_VERSION, CAMERA
    )
    catalog = _Catalog(automatic_calibration_inputs=AutomaticCalibrationInputs(
        CAMERA, K, [a], [[10.0, 20.0]]))  # fmt: skip
    with pytest.raises(ApplicationError) as error:
        await _run(_acts(catalog).resolve_automatic_calibration_inputs, TARGET)
    assert error.value.type == "AutomaticRenderMissing" and error.value.non_retryable


async def test_a_calibration_is_written_with_its_camera():
    camera = uuid.uuid4()
    catalog = _Catalog(
        automatic_calibration_inputs=AutomaticCalibrationInputs(camera, K, [], []),
        persist_automatic_calibration=lambda t, d, row: uuid.uuid4(),
    )
    result = AutomaticCalibrationResult(
        mission_id=MISSION, outcome="refused", refusal_reason="no_candidates",
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
        mission_id=MISSION, algorithm="laser_depth_fronto_parallel", algorithm_version="1",
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


async def test_the_missions_a_run_took_are_excluded_in_every_tenant():
    taken = [MISSION, uuid.uuid4()]
    catalog = _Catalog(next_mission_for_automatic_results=None)
    await _run(_acts(catalog).select_next_mission_for_automatic_results, taken)
    assert [kw["exclude"] for _, _, kw in catalog.calls] == [taken, taken]


async def test_a_failed_step_is_recorded_against_its_mission():
    catalog = _Catalog(record_automatic_results_failure=None)
    await _run(
        _acts(catalog).record_automatic_results_failure, TARGET, "frames", "boom"
    )
    assert catalog.calls == [
        ("record_automatic_results_failure", (TENANT_A, MISSION),
         {"step": "frames", "error": "boom"}),
    ]  # fmt: skip


async def test_frames_left_out_are_recorded_against_each_frame():
    """Review of #85: never as one of the dive's strikes. The store answers
    which frames it has given up on (their terminal `frame_failed` rows)."""
    bad = uuid.uuid4()
    catalog = _Catalog(record_automatic_frame_failures=[bad])
    given_up = await _run(
        _acts(catalog).record_automatic_frame_failures, TARGET, [bad], "left out"
    )
    assert given_up == [bad]
    assert catalog.calls == [
        ("record_automatic_frame_failures", (TENANT_A, MISSION, [(bad, "left out")]),
         {"headtail_version": AUTOMATIC_HEADTAIL_PREDICTOR_VERSION}),
    ]  # fmt: skip


async def test_a_run_is_planned_for_the_named_tenant():
    from fishsense_services_api.automatic_results_store import (
        AutomaticRunPlan,
        AutomaticRunPlanMission,
    )

    mission = AutomaticRunPlanMission(
        number=62, mission_id=MISSION, folder="/d/62", calibration_session=True,
        excluded=False, human_measured=False, rectifiable=True, frames=9,
        frames_owed=9,
    )  # fmt: skip
    catalog = _Catalog(
        tenant_by_slug=TENANT_A,
        automatic_run_plan=AutomaticRunPlan(missions=[mission], missing=[1]),
    )
    plan = await _run(_acts(catalog).plan_automatic_run, "lab", [62, 1])

    assert plan["tenant_id"] == str(TENANT_A)
    assert plan["dives"][0]["dive_id"] == str(MISSION)
    assert plan["dives"][0]["calibration_session"] is True
    assert plan["missing"] == [1]
    assert catalog.calls[1] == (
        "automatic_run_plan", (TENANT_A, [62, 1]),
        {"headtail_version": AUTOMATIC_HEADTAIL_PREDICTOR_VERSION},
    )  # fmt: skip


async def test_a_run_for_a_tenant_the_orchestrator_is_not_in_is_refused():
    catalog = _Catalog(tenant_by_slug=None)
    with pytest.raises(ApplicationError) as error:
        await _run(_acts(catalog).plan_automatic_run, "nobody", [62])
    assert error.value.non_retryable


# -- stored masks (contract 12, 2026-10-21) ------------------------------------------


class _MaskBucket:
    bucket = "fishsense-tenant-lab"

    def __init__(self, stored=None):
        self.stored = stored or {}
        self.deleted = []

    def delete(self, key):
        self.deleted.append(key)

    def presign_mask_put(self, key):
        return SimpleNamespace(url=f"https://g/{key}?sig")

    def get(self, key):
        return self.stored.get(key)


async def test_each_frame_of_a_chunk_gets_a_mask_upload():
    renders = _Renders()
    renders.bucket = _MaskBucket()
    captures = [uuid.uuid4(), uuid.uuid4()]

    uploads = await _run(_acts(_Catalog(), renders=renders).presign_automatic_masks,
                         TARGET, captures)  # fmt: skip

    assert [u.key.split("/")[2] for u in uploads] == [str(c) for c in captures]
    assert all(u.put_url.startswith("https://g/masks/automatic/") for u in uploads)


async def test_a_tenant_with_no_bucket_gets_no_mask_uploads():
    uploads = await _run(_acts(_Catalog()).presign_automatic_masks, TARGET,
                         [uuid.uuid4()])  # fmt: skip
    assert uploads == [None]


async def test_a_checked_mask_is_written_with_its_prediction_and_left_off_the_row():
    import hashlib

    import cv2
    import numpy as np

    from fishsense_services_contracts.masks import StoredMask, mask_key

    data = cv2.imencode(".png", np.zeros((3, 4), np.uint8))[1].tobytes()
    capture = uuid.uuid4()
    key = mask_key("automatic", capture, uuid.uuid4())
    r = _frame_result(capture_id=capture, width=4, height=3, mask_bbox=[0, 0, 1, 1],
                      mask=StoredMask(key=key, size=len(data),
                                      sha256=hashlib.sha256(data).hexdigest(),
                                      width=4, height=3))  # fmt: skip
    renders = _Renders()
    renders.bucket = _MaskBucket({key: data})
    catalog = _Catalog(persist_automatic_head_tails=lambda t, d, rows, masks: len(rows))

    assert await _run(_acts(catalog, renders=renders).persist_automatic_frames,
                      TARGET, [r]) == 1  # fmt: skip
    ((_, (_, _, rows, masks), _),) = catalog.calls
    assert not hasattr(rows[0], "mask")
    assert [(m.capture_id, m.key) for m in masks] == [(capture, key)]
    assert renders.bucket.deleted == []


async def test_a_mask_that_fails_its_check_costs_only_itself():
    from fishsense_services_contracts.masks import StoredMask, mask_key

    capture = uuid.uuid4()
    key = mask_key("automatic", capture, uuid.uuid4())
    r = _frame_result(capture_id=capture, width=4, height=3, mask_bbox=[0, 0, 1, 1],
                      mask=StoredMask(key=key, size=9, sha256="a" * 64, width=4,
                                      height=3))  # fmt: skip
    renders = _Renders()
    renders.bucket = _MaskBucket({key: b"other bytes"})
    catalog = _Catalog(
        persist_automatic_head_tails=lambda t, d, rows, *masks: len(rows)
    )

    assert await _run(_acts(catalog, renders=renders).persist_automatic_frames,
                      TARGET, [r]) == 1  # fmt: skip
    ((_, (_, _, _, masks), _),) = catalog.calls
    assert masks == [] and renders.bucket.deleted == [key]


async def test_a_mask_of_a_frame_left_owed_is_deleted():
    """A frame that names no render is not written (left owed): its mask
    would name nothing, so it is deleted (review of #232)."""
    from fishsense_services_contracts.masks import StoredMask, mask_key

    capture = uuid.uuid4()
    key = mask_key("automatic", capture, uuid.uuid4())
    r = _frame_result(capture_id=capture, width=4, height=3, mask_bbox=[0, 0, 1, 1],
                      jpeg_render_version=None, jpeg_camera_calibration_id=None,
                      mask=StoredMask(key=key, size=9, sha256="a" * 64, width=4,
                                      height=3))  # fmt: skip
    renders = _Renders()
    renders.bucket = _MaskBucket({})
    catalog = _Catalog(
        persist_automatic_head_tails=lambda t, d, rows, *masks: len(rows)
    )

    await _run(_acts(catalog, renders=renders).persist_automatic_frames, TARGET, [r])
    assert renders.bucket.deleted == [key]


def test_the_api_repeats_the_window_and_the_cut_mask_version():
    """The API image has no contracts: its copies of the window and of the
    version that grows a cut mask's window are pinned here (2026-10-22)."""
    from fishsense_services_api import automatic_results_store as api
    from fishsense_services_contracts.headtail import (
        HEADTAIL_CROP_HEIGHT,
        HEADTAIL_CROP_WIDTH,
    )

    assert (api.HEADTAIL_CROP_WIDTH, api.HEADTAIL_CROP_HEIGHT) == (
        HEADTAIL_CROP_WIDTH,
        HEADTAIL_CROP_HEIGHT,
    )
    assert api.CUT_MASK_FIX_VERSION == AUTOMATIC_HEADTAIL_PREDICTOR_VERSION == 4


async def test_the_dots_colour_reaches_the_row():
    """Contract v21 (2026-10-24): the processor sends the colour it read off
    the dot, and the row keeps it."""
    written = []
    catalog = _Catalog(
        persist_automatic_head_tails=lambda t, d, rows, *masks: written.extend(rows)
        or len(rows)
    )
    result = _frame_result(laser_color="red", laser_color_margin=0.4)

    await _run(_acts(catalog).persist_automatic_frames, TARGET, [result])

    assert [(r.laser_color, r.laser_color_margin) for r in written] == [("red", 0.4)]
