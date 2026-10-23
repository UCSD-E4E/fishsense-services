"""Calibration v3's read-only replay: per mission, the automatic chain's own
inputs then v3 on the per-image queue (no frames: refused, nothing woken);
the run replays every planned mission, draws only for the control missions,
goes on past one that fails, and returns where each report is."""

from __future__ import annotations

import uuid
from typing import List, Optional

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import PROCESSOR_GPU_TASK_QUEUE, PROCESSOR_TASK_QUEUE
from fishsense_services_contracts.automatic_results import (
    AutomaticCalibrationFrame,
    FitAutomaticCalibrationInput,
)
from fishsense_services_contracts.calibration_v3 import (
    CalibrationV3Report,
    ReplayCalibrationInput,
)
from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_orchestrator.automatic_results.activities import (
    AutomaticCalibrationPlan,
    AutomaticTarget,
)
from fishsense_services_orchestrator.calibration_v3.workflow import (
    ReplayCalibrationV3ForMissionWorkflow,
    ReplayCalibrationV3Workflow,
    ReplayRequest,
    ReplayTarget,
)

TENANT = uuid.uuid4()
QUEUE = "test-calibration-v3"
EVENTS: list = []
IDS = {n: uuid.uuid5(uuid.NAMESPACE_OID, f"m{n}") for n in range(1, 6)}


def _stubs(*, frames=2, broken=(), mode="gpu", template=True):
    @activity.defn(name="plan_calibration_v3_replay")
    async def plan(tenant: str, numbers: Optional[List[int]] = None) -> dict:
        ns = numbers or [1, 2, 3]
        return {"tenant_id": str(TENANT),
                "missions": [{"mission_id": str(IDS[n]), "number": n} for n in ns]}  # fmt: skip

    @activity.defn(name="resolve_automatic_calibration_inputs")
    async def resolve(t: AutomaticTarget) -> AutomaticCalibrationPlan:
        if t.mission_id in {IDS[n] for n in broken}:
            raise ApplicationError("boom", type="Boom", non_retryable=True)
        EVENTS.append(("resolve", str(t.mission_id)))
        return AutomaticCalibrationPlan(
            payload=FitAutomaticCalibrationInput(
                tenant_id=t.tenant_id, mission_id=t.mission_id, camera_matrix=[[1.0]],
                frames=[AutomaticCalibrationFrame(capture_id=uuid.uuid4(), jpeg_url="u",
                                                  x=1.0, y=2.0) for _ in range(frames)]),
            camera_calibration_id=uuid.uuid4())  # fmt: skip

    @activity.defn(name="ensure_per_image_processor_running")
    async def wake() -> None:
        EVENTS.append(("wake",))

    @activity.defn(name="ensure_gpu_processor_running")
    async def wake_gpu() -> str:
        EVENTS.append(("wake gpu",))
        return mode

    @activity.defn(name="resolve_mission_slate_template")
    async def slate(t: AutomaticTarget) -> Optional[dict]:
        EVENTS.append(("template",))
        if not template:
            return None
        return {"slate_template_id": str(IDS[5]), "dpi": 200,
                "reference_points": [[1.0, 2.0], [3.0, 4.0]],
                "slate_pdf": {"bucket": "scratch", "key": f"slate_pdf/{IDS[5]}.pdf"}}  # fmt: skip

    @activity.defn(name="stage_slate_pdf")
    async def stage_pdf(target) -> bool:
        EVENTS.append(("stage pdf", str(target["slate_template_id"])))
        return True

    return [plan, resolve, wake, wake_gpu, slate, stage_pdf]


@workflow.defn(name="ReplayAutomaticCalibrationV3Workflow")
class _Replay:  # pylint: disable=too-few-public-methods
    """The processor's replay workflow, on the per-image queue."""

    @workflow.run
    async def run(self, r: dict) -> dict:
        await workflow.execute_activity(
            "_record", ["replay", r["payload"]["dive_id"], r["draw_sizes"],
                        r["draws_per_size"], r.get("size_source"),
                        sorted(v for v in r.get("sam_sizes", {}).values() if v)],
            start_to_close_timeout=timedelta(seconds=10),
        )  # fmt: skip
        await workflow.execute_activity(
            "_record", ["points", r.get("pose_points", {})],
            start_to_close_timeout=timedelta(seconds=10))  # fmt: skip
        return CalibrationV3Report(
            mission_id=r["payload"]["dive_id"], outcome="accepted"
        ).model_dump(mode="json", by_alias=True)


@activity.defn(name="_record")
async def _record(event: list) -> None:
    if event[0] == "points":
        POINTS.append(event[1])
    elif event[0] == "replay":
        EVENTS.append((event[0], event[1], tuple(event[2]), event[3]))
        SAM.append((event[4], event[5]))
    else:
        EVENTS.append(tuple(event))


SAM: list = []
POINTS: list = []


@workflow.defn(name="SlatePoseFramesWorkflow")
class _Pose:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, req: dict) -> list:
        await workflow.execute_activity(
            "_record", ["pose", str(len(req["frames"])), req["slate_pdf"]["key"],
                        str(req["slate_dpi"])],
            start_to_close_timeout=timedelta(seconds=10))  # fmt: skip
        return [{"capture_id": f["capture_id"],
                 "status": "weak_alignment" if i == 0 else "posed",
                 "point": [0.1, 0.0, 1.0 + i]} for i, f in enumerate(req["frames"])]  # fmt: skip


@workflow.defn(name="SegmentSlateFramesWorkflow")
class _Segment:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: dict) -> list:
        await workflow.execute_activity(
            "_record", ["segment", str(len(payload["frames"]))],
            start_to_close_timeout=timedelta(seconds=10))  # fmt: skip
        return [{"capture_id": f["capture_id"], "status": "segmented",
                 "dots": [{"size": 10.0 + i}]} for i, f in enumerate(payload["frames"])]  # fmt: skip


async def _run(fn, arg, **stubs):
    EVENTS.clear()
    SAM.clear()
    POINTS.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue=QUEUE, activities=_stubs(**stubs),
                          workflows=[ReplayCalibrationV3Workflow,
                                     ReplayCalibrationV3ForMissionWorkflow]), \
                   Worker(env.client, task_queue=PROCESSOR_TASK_QUEUE, activities=[_record],
                          workflows=[_Replay]), \
                   Worker(env.client, task_queue=PROCESSOR_GPU_TASK_QUEUE,
                          activities=[_record], workflows=[_Segment, _Pose]):  # fmt: skip
            result = await env.client.execute_workflow(
                fn, arg, id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE
            )
            return result, env


async def test_a_mission_is_resolved_then_replayed_on_the_per_image_queue():
    target = ReplayTarget(TENANT, IDS[1], [3, 4], 5)
    report, _ = await _run(ReplayCalibrationV3ForMissionWorkflow.run, target)
    assert report.outcome == "accepted"
    assert EVENTS == [
        ("resolve", str(IDS[1])),
        ("wake",),
        ("replay", str(IDS[1]), (3, 4), 5),
    ]


async def test_a_mission_with_no_slate_frame_is_refused_and_wakes_nothing():
    report, _ = await _run(ReplayCalibrationV3ForMissionWorkflow.run,
                           ReplayTarget(TENANT, IDS[1]), frames=0)  # fmt: skip
    assert (report.outcome, report.refusal_reason) == ("refused", "no_candidates")
    assert EVENTS == [("resolve", str(IDS[1]))]


async def test_the_run_replays_every_mission_and_draws_only_the_controls():
    request = ReplayRequest(tenant="lab", numbers=[1, 2, 3], draw_numbers=[2],
                            draw_sizes=[3, 4], draws_per_size=5)  # fmt: skip
    result, _ = await _run(ReplayCalibrationV3Workflow.run, request, broken=(3,))
    rows = {r["number"]: r for r in result["missions"]}
    assert set(rows) == {1, 2, 3} and "error" in rows[3] and "error" not in rows[1]
    replays = {e[1]: e[2:] for e in EVENTS if e[0] == "replay"}
    assert replays == {str(IDS[1]): ((), 0), str(IDS[2]): ((3, 4), 5)}
    assert all(r["child"].endswith(r["mission_id"]) for r in result["missions"])


async def test_sam_sizes_are_segmented_on_a_real_gpu_then_fitted():
    """Strategy 2: each slate frame's SAM mask size, measured on the GPU, is
    what the replay fits on."""
    target = ReplayTarget(TENANT, IDS[1], size_source="sam")
    await _run(ReplayCalibrationV3ForMissionWorkflow.run, target, frames=3)
    assert ("wake gpu",) in EVENTS and ("segment", "3") in EVENTS
    assert SAM == [("sam", [10.0, 11.0, 12.0])]


async def test_sam_sizes_without_a_gpu_are_refused_unsegmented():
    target = ReplayTarget(TENANT, IDS[1], size_source="sam")
    report, _ = await _run(ReplayCalibrationV3ForMissionWorkflow.run, target,
                           mode="cpu_fallback")  # fmt: skip
    assert (report.outcome, report.refusal_reason) == ("refused", "no_gpu")
    assert not any(e[0] in ("segment", "replay") for e in EVENTS)


async def test_pose_points_come_from_the_gpu_and_only_strong_poses_are_fitted():
    """Calibration v4: the mission's template staged, each slate frame posed
    on a real GPU, the points of strong poses fitted; every pose is in the
    report, weak ones too, for the threshold to be set from them."""
    target = ReplayTarget(TENANT, IDS[1], size_source="pose")
    report, _ = await _run(ReplayCalibrationV3ForMissionWorkflow.run, target, frames=3)
    assert ("stage pdf", str(IDS[5])) in EVENTS and ("wake gpu",) in EVENTS
    assert ("pose", "3", f"slate_pdf/{IDS[5]}.pdf", "200") in EVENTS
    (points,) = POINTS
    assert sorted(v[2] for v in points.values() if v) == [2.0, 3.0]
    assert len(points) == 3 and sum(v is None for v in points.values()) == 1
    assert [p.status for p in report.poses] == ["weak_alignment", "posed", "posed"]


async def test_a_mission_without_a_usable_template_is_refused_and_wakes_nothing():
    target = ReplayTarget(TENANT, IDS[1], size_source="pose")
    report, _ = await _run(ReplayCalibrationV3ForMissionWorkflow.run, target,
                           template=False)  # fmt: skip
    assert (report.outcome, report.refusal_reason) == ("refused", "no_slate_template")
    assert not any(e[0] in ("wake gpu", "wake", "pose", "stage pdf") for e in EVENTS)


async def test_pose_points_without_a_gpu_are_refused_unposed():
    target = ReplayTarget(TENANT, IDS[1], size_source="pose")
    report, _ = await _run(ReplayCalibrationV3ForMissionWorkflow.run, target,
                           mode="cpu_fallback")  # fmt: skip
    assert (report.outcome, report.refusal_reason) == ("refused", "no_gpu")
    assert not any(e[0] in ("pose", "replay") for e in EVENTS)


async def test_the_template_resolves_with_where_its_pdf_is_staged():
    from temporalio.testing import (
        ActivityEnvironment,
    )  # pylint: disable=import-outside-toplevel

    from fishsense_services_api.slate_store import SlateTemplate
    from fishsense_services_contracts.object_store import ObjectStoreConnection
    from fishsense_services_orchestrator.calibration_v3.activities import (
        CalibrationV3Activities,
    )
    from fishsense_services_orchestrator.object_store.layout import ObjectLayout

    template = SlateTemplate(id=IDS[5], name="t", dpi=200,
                             reference_points=[(1.0, 2.0)], source_path=None, v1_id=None)  # fmt: skip

    class _Slates:
        async def mission_slate_template(self, tenant_id, mission_id):
            return template if mission_id == IDS[1] else None

    layout = ObjectLayout(ObjectStoreConnection(
        endpoint_url="https://s3.example.test", region="garage", access_key_id="k",
        secret_access_key="s", bucket="scratch", labels_bucket="labels",
        legacy_labels_prefix="x"))  # fmt: skip
    acts = CalibrationV3Activities(catalog=None, slates=_Slates(), layout=layout)
    env = ActivityEnvironment()
    got = await env.run(acts.resolve_mission_slate_template,
                        AutomaticTarget(TENANT, IDS[1]))  # fmt: skip
    assert got["dpi"] == 200 and got["reference_points"] == [[1.0, 2.0]]
    assert got["slate_pdf"] == layout.slate_pdf(TENANT, IDS[5]).model_dump(mode="json")
    assert await env.run(acts.resolve_mission_slate_template,
                         AutomaticTarget(TENANT, IDS[2])) is None  # fmt: skip
