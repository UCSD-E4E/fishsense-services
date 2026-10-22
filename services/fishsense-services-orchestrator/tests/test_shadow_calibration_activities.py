"""The shadow calibration's orchestrator activities, against fakes.

- selection walks the member tenants, asking at this algorithm version and
  passing the run's exclusions;
- resolve presigns each rendered frame's render and keeps both dots, leaving
  out (and counting) a frame with no render and a frame with no dot;
- assemble puts SAM's answer under the right dot of the right source, and a
  frame SAM never answered for keeps no size;
- persist refuses another mission's result.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from fishsense_services_api.shadow_calibration_store import (
    ShadowCalibrationInputs,
    ShadowFrame,
)
from fishsense_services_contracts.shadow_calibration import (
    SHADOW_ALGORITHM_VERSION,
    DotSegment,
    ShadowCalibrationOut,
    ShadowFitResult,
    ShadowFrameOut,
    SlateSegment,
)
from fishsense_services_orchestrator.shadow_calibration.activities import (
    ShadowCalibrationActivities,
    ShadowTarget,
)

TENANT, MISSION, CAMERA = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
TARGET = ShadowTarget(TENANT, MISSION)


def _frame(*, label=True, detector=True):
    return ShadowFrame(
        capture_id=uuid.uuid4(), capture_number=3,
        captured_at=datetime(2024, 1, 1, tzinfo=timezone.utc), checksum="c",
        frame_source="both", slate_probability=None,
        label_dot=(10.0, 20.0) if label else None,
        label_id=uuid.uuid4() if label else None,
        label_source="label_studio" if label else None,
        detector_dot=(11.0, 21.0) if detector else None,
        detector_confidence=0.9 if detector else None,
        prediction_id=uuid.uuid4() if detector else None,
    )  # fmt: skip


class _Catalog:
    def __init__(self, frames=(), answers=None):
        self.frames = list(frames)
        self.answers = answers or {}
        self.asked = []
        self.persisted = []

    async def member_tenants(self):
        return list(self.answers)

    async def next_mission_for_shadow_calibration(self, tenant_id, *, algorithm_version,
                                                  exclude=()):  # fmt: skip
        self.asked.append((tenant_id, algorithm_version, list(exclude)))
        return self.answers[tenant_id]

    async def shadow_calibration_inputs(
        self, tenant_id, mission_id, *, headtail_version
    ):
        return ShadowCalibrationInputs(
            mission_number=7, camera_calibration_id=CAMERA, camera_matrix=[[1.0]],
            mission_line=(1.0, 0.0, -5.0), reference_laser_calibration_id=None,
            reference_borrowed=None, reference_position=None, reference_axis=None,
            frames=self.frames, mission_detector_dots=[(1.0, 2.0, 0.9)],
        )  # fmt: skip

    async def persist_shadow_calibrations(self, tenant_id, mission_id, rows):
        self.persisted += rows
        return [uuid.uuid4() for _ in rows]


class _Renders:
    def __init__(self, rendered, failed=()):
        self.rendered = rendered
        self.failed = set(failed)

    async def at(
        self, tenant_id, capture_ids, *, render_version, camera_calibration_id
    ):
        assert camera_calibration_id == CAMERA
        return {c: SimpleNamespace(status="failed" if c in self.failed else "rendered",
                                   key=None if c in self.failed else f"renders/{c}.JPG")
                for c in capture_ids if c in self.rendered | self.failed}  # fmt: skip

    async def presigner(self, tenant_id):
        return lambda key: f"https://g/{key}?sig"


def _activities(frames=(), rendered=None, answers=None, failed=()):
    rendered = {f.capture_id for f in frames} if rendered is None else rendered
    catalog = _Catalog(frames, answers)
    renders = _Renders(rendered, failed)
    return ShadowCalibrationActivities(catalog=catalog, renders=renders), catalog


async def test_select_walks_tenants_at_this_version():
    other, mission = uuid.uuid4(), uuid.uuid4()
    acts, catalog = _activities(answers={TENANT: None, other: mission})
    excluded = [uuid.uuid4()]
    assert await acts.select_next_mission_for_shadow_calibration(excluded) == ShadowTarget(
        other, mission)  # fmt: skip
    assert catalog.asked == [(TENANT, SHADOW_ALGORITHM_VERSION, excluded),
                             (other, SHADOW_ALGORITHM_VERSION, excluded)]  # fmt: skip


async def test_select_none_left():
    acts, _ = _activities(answers={TENANT: None})
    assert await acts.select_next_mission_for_shadow_calibration() is None


async def test_resolve_keeps_both_dots_and_leaves_out_the_unrendered():
    both, label_only, unrendered, dotless = (
        _frame(),
        _frame(detector=False),
        _frame(),
        _frame(label=False, detector=False),
    )
    acts, _ = _activities([both, label_only, unrendered, dotless],
                          rendered={both.capture_id, label_only.capture_id})  # fmt: skip
    plan = await acts.resolve_shadow_calibration_inputs(TARGET)

    seg = {f.capture_id: f for f in plan.segment.frames}
    assert set(seg) == {both.capture_id, label_only.capture_id}
    assert seg[both.capture_id].dots == [[10.0, 20.0], [11.0, 21.0]]
    assert (
        seg[both.capture_id].render_url
        == f"https://g/renders/{both.capture_id}.JPG?sig"
    )
    assert seg[label_only.capture_id].dots == [[10.0, 20.0]]
    assert plan.dot_index[str(both.capture_id)] == {"labels": 0, "detector": 1}
    assert plan.unrendered == 1
    assert [f.capture_id for f in plan.fit.frames["labels"]] == [both.capture_id,
                                                                  label_only.capture_id]  # fmt: skip
    (det,) = plan.fit.frames["detector"]
    assert (det.dot_x, det.dot_y, det.dot_confidence) == (11.0, 21.0, 0.9)
    assert det.dot_prediction_id == both.prediction_id
    assert plan.fit.mission_number == 7
    assert plan.fit.mission_line == [1.0, 0.0, -5.0]
    assert plan.fit.mission_detector_dots == [[1.0, 2.0, 0.9]]


async def test_resolve_leaves_out_a_failed_render():
    ok, failed = _frame(), _frame()
    acts, _ = _activities([ok, failed], rendered={ok.capture_id},
                          failed={failed.capture_id})  # fmt: skip
    plan = await acts.resolve_shadow_calibration_inputs(TARGET)
    assert [f.capture_id for f in plan.segment.frames] == [ok.capture_id]
    assert plan.unrendered == 1


async def test_resolve_with_no_frames_asks_for_no_renders():
    acts, _ = _activities([])
    plan = await acts.resolve_shadow_calibration_inputs(TARGET)
    assert plan.segment.frames == [] and plan.unrendered == 0


async def test_assemble_puts_each_answer_under_its_own_dot():
    both, missing = _frame(), _frame()
    acts, _ = _activities([both, missing])
    lost = SlateSegment(capture_id=missing.capture_id, status="decode_failed")
    plan = await acts.resolve_shadow_calibration_inputs(TARGET)
    segments = [SlateSegment(
        capture_id=both.capture_id, status="segmented", render_scale=2.5,
        sam_model="sam3.1",
        dots=[DotSegment(prompt="white board", score=0.8, mask_area_px=100.0, size=10.0),
              DotSegment(prompt="slate", score=0.6, mask_area_px=400.0, size=20.0)],
    ), lost]  # fmt: skip
    fit = await acts.assemble_shadow_fit(plan, segments)

    label = {f.capture_id: f for f in fit.frames["labels"]}
    det = {f.capture_id: f for f in fit.frames["detector"]}
    assert (label[both.capture_id].size, label[both.capture_id].sam_prompt) == (10.0,
                                                                                "white board")  # fmt: skip
    assert (det[both.capture_id].size, det[both.capture_id].sam_score) == (20.0, 0.6)
    assert (
        label[missing.capture_id].size is None and det[missing.capture_id].size is None
    )
    assert fit.render_scale == 2.5 and fit.sam_model == "sam3.1"


async def test_persist_refuses_another_missions_result():
    acts, catalog = _activities()
    with pytest.raises(ValueError):
        await acts.persist_shadow_calibration(
            TARGET, ShadowFitResult(mission_id=uuid.uuid4(), calibrations=[])
        )
    assert catalog.persisted == []


async def test_persist_stores_every_result_with_its_frames():
    acts, catalog = _activities()
    out = ShadowCalibrationOut(
        dot_source="labels", variant="port", outcome="refused", o_mag_m=0.104,
        frames_used=0, sam_model="m", sam_prompts=["slate"], sam_threshold=0.3,
        render_long_side=1600, algorithm_version="1", code_version="x",
        refusal_reason="too_few_frames",
        frames=[ShadowFrameOut(capture_id=uuid.uuid4(), frame_source="both",
                               used=False, rejected_reason="no_mask")],
    )  # fmt: skip
    assert (
        await acts.persist_shadow_calibration(
            TARGET, ShadowFitResult(mission_id=MISSION, calibrations=[out])
        )
        == 1
    )
    (row,) = catalog.persisted
    assert row.refusal_reason == "too_few_frames" and len(row.frames) == 1


async def test_assemble_refuses_a_frame_sam_never_answered_for():
    """Review of #261: a frame SAM never answered for (its render gone, every
    try failed) is an outage, not a frame with no mask: the mission fails and
    is tried again, rather than stored short."""
    from temporalio.exceptions import ApplicationError

    answered, lost = _frame(), _frame()
    acts, _ = _activities([answered, lost])
    plan = await acts.resolve_shadow_calibration_inputs(TARGET)
    seg = SlateSegment(capture_id=answered.capture_id, status="segmented",
                       render_scale=2.5, sam_model="m",
                       dots=[DotSegment(), DotSegment()])  # fmt: skip
    gone = SlateSegment(capture_id=lost.capture_id, status="render_unavailable")
    for segments in ([seg], [seg, gone]):
        with pytest.raises(ApplicationError) as e:
            await acts.assemble_shadow_fit(plan, segments)
        assert e.value.type == "SegmentationIncomplete"


async def test_assemble_marks_a_render_sam_could_not_decode():
    ok, bad = _frame(), _frame()
    acts, _ = _activities([ok, bad])
    plan = await acts.resolve_shadow_calibration_inputs(TARGET)
    fit = await acts.assemble_shadow_fit(plan, [
        SlateSegment(capture_id=ok.capture_id, status="segmented", render_scale=2.5,
                     sam_model="m", dots=[DotSegment(), DotSegment()]),
        SlateSegment(capture_id=bad.capture_id, status="decode_failed"),
    ])  # fmt: skip
    status = {f.capture_id: f.segment_status for f in fit.frames["labels"]}
    assert status == {ok.capture_id: None, bad.capture_id: "decode_failed"}
