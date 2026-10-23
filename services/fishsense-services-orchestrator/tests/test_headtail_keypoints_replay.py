"""The SimCC keypoint replay, read-only: a page of a mission's frames, each
with the render its prediction was made on (by version and camera) and its
mask box; one whose render is gone is counted and left out. A mission pages
to its end on a real GPU only and returns every result; the run replays
every planned mission, goes on past one that fails, and says where each
mission's results are. Nothing is written anywhere."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import List, Optional

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import PROCESSOR_GPU_TASK_QUEUE
from fishsense_services_contracts.headtail_keypoints import (
    KeypointFrame,
    KeypointFramesInput,
    KeypointResult,
)
from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_orchestrator.headtail_keypoints.activities import (
    KeypointPage,
    KeypointReplayActivities,
    KeypointTarget,
)
from fishsense_services_orchestrator.headtail_keypoints.workflow import (
    KeypointMissionResult,
    KeypointReplayRequest,
    ReplayHeadtailKeypointsForMissionWorkflow,
    ReplayHeadtailKeypointsWorkflow,
    keypoint_child_id,
)

TENANT = uuid.uuid4()


# -- the activities ---------------------------------------------------------------------


class _Catalog:
    def __init__(self, rows):
        self.rows = rows

    async def member_tenants(self):
        return [TENANT]

    async def tenant_slug(self, t):
        return "lab"

    async def keypoint_replay_missions(self, t):
        return [
            SimpleNamespace(mission_id=uuid.uuid4(), number=7, frames=len(self.rows))
        ]

    async def keypoint_replay_frames(self, t, m, *, after_seq=0, limit=200):
        return [r for r in self.rows if r.seq > after_seq][:limit]


class _Renders:
    def __init__(self, rendered, bucket=True):
        self.rendered, self.asked, self.bucket = rendered, [], bucket

    async def objects(self, t):
        return object() if self.bucket else None

    async def at(self, t, ids, *, render_version, camera_calibration_id):
        self.asked.append((render_version, camera_calibration_id))
        return {c: SimpleNamespace(status="rendered", key=f"renders/{c}.JPG")
                for c in ids if c in self.rendered}  # fmt: skip

    async def presigner(self, t):
        return lambda key: f"https://g/get/{key}"


def _row(seq, version=1, camera=None):
    return SimpleNamespace(prediction_id=uuid.uuid4(), seq=seq, capture_id=uuid.uuid4(),
                           mask_bbox=[1, 2, 30, 40], render_version=version,
                           camera_calibration_id=camera or uuid.UUID(int=9))  # fmt: skip


async def test_a_page_carries_each_frames_own_render_and_box():
    cam2 = uuid.uuid4()
    a, b, gone = _row(1), _row(2, version=2, camera=cam2), _row(3)
    renders = _Renders({a.capture_id, b.capture_id})
    acts = KeypointReplayActivities(catalog=_Catalog([a, b, gone]), renders=renders)
    page = await ActivityEnvironment().run(acts.resolve_keypoint_page,
                                           KeypointTarget(TENANT, uuid.uuid4()), 0)  # fmt: skip
    assert (page.last_seq, page.unavailable) == (3, 1)
    by = {f.prediction_id: f for f in page.payload.frames}
    assert set(by) == {a.prediction_id, b.prediction_id}
    assert by[b.prediction_id].render_url == f"https://g/get/renders/{b.capture_id}.JPG"
    assert by[a.prediction_id].mask_bbox == [1, 2, 30, 40]
    assert sorted(renders.asked, key=str) == sorted(
        [(1, uuid.UUID(int=9)), (2, cam2)], key=str
    )


async def test_the_end_of_a_mission_and_a_tenant_without_a_bucket():
    acts = KeypointReplayActivities(
        catalog=_Catalog([_row(1)]), renders=_Renders(set())
    )
    end = await ActivityEnvironment().run(acts.resolve_keypoint_page,
                                          KeypointTarget(TENANT, uuid.uuid4()), 1)  # fmt: skip
    assert (end.payload, end.last_seq) == (None, None)
    acts = KeypointReplayActivities(catalog=_Catalog([_row(1)]),
                                    renders=_Renders(set(), bucket=False))  # fmt: skip
    page = await ActivityEnvironment().run(acts.resolve_keypoint_page,
                                           KeypointTarget(TENANT, uuid.uuid4()), 0)  # fmt: skip
    assert (page.payload, page.last_seq, page.unavailable) == (None, 1, 1)


async def test_the_plan_is_the_tenants_missions_with_work():
    acts = KeypointReplayActivities(catalog=_Catalog([_row(1), _row(2)]), renders=None)
    plan = await ActivityEnvironment().run(acts.plan_keypoint_replay, "lab", None)
    assert plan["tenant_id"] == str(TENANT)
    assert [(m["number"], m["frames"]) for m in plan["missions"]] == [(7, 2)]


# -- the workflows ----------------------------------------------------------------------

QUEUE = "test-keypoints"
EVENTS: list = []
IDS = {n: uuid.uuid5(uuid.NAMESPACE_OID, f"k{n}") for n in range(1, 5)}


def _stubs(*, pages=((2, 0),), mode="gpu", broken=()):
    @activity.defn(name="plan_keypoint_replay")
    async def plan(tenant: str, numbers: Optional[List[int]] = None) -> dict:
        return {"tenant_id": str(TENANT),
                "missions": [{"mission_id": str(IDS[n]), "number": n, "frames": 2}
                             for n in (numbers or [1, 2, 3])]}  # fmt: skip

    @activity.defn(name="resolve_keypoint_page")
    async def resolve(t: KeypointTarget, after: int = 0) -> KeypointPage:
        if t.mission_id in {IDS[n] for n in broken}:
            raise ApplicationError("boom", type="Boom", non_retryable=True)
        EVENTS.append(("resolve", after))
        if after >= len(pages):
            return KeypointPage(payload=None)
        n, gone = pages[after]
        frames = [KeypointFrame(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                                render_url="u", mask_bbox=[1, 1, 5, 5]) for _ in range(n)]  # fmt: skip
        return KeypointPage(
            payload=KeypointFramesInput(tenant_id=t.tenant_id, mission_id=t.mission_id,
                                        frames=frames) if n else None,
            last_seq=after + 1, unavailable=gone)  # fmt: skip

    @activity.defn(name="ensure_gpu_processor_running")
    async def wake() -> str:
        EVENTS.append(("wake",))
        return mode

    return [plan, resolve, wake]


@workflow.defn(name="PredictHeadtailKeypointsWorkflow")
class _Gpu:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: dict) -> list:
        return [KeypointResult(prediction_id=f["prediction_id"], capture_id=f["capture_id"],
                               status="predicted").model_dump(mode="json")
                for f in payload["frames"]]  # fmt: skip


async def _run(fn, arg, **stubs):
    EVENTS.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue=QUEUE, activities=_stubs(**stubs),
                          workflows=[ReplayHeadtailKeypointsForMissionWorkflow,
                                     ReplayHeadtailKeypointsWorkflow]), \
                   Worker(env.client, task_queue=PROCESSOR_GPU_TASK_QUEUE,
                          workflows=[_Gpu]):  # fmt: skip
            return await env.client.execute_workflow(
                fn, arg, id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE
            )


TARGET = KeypointTarget(TENANT, IDS[1])


async def test_a_mission_pages_to_its_end_and_returns_every_result():
    r: KeypointMissionResult = await _run(
        ReplayHeadtailKeypointsForMissionWorkflow.run, TARGET, pages=((3, 1), (2, 0))
    )
    assert (len(r.results), r.unavailable, r.no_gpu) == (5, 1, False)
    assert EVENTS == [("resolve", 0), ("wake",), ("resolve", 1), ("resolve", 2)]


async def test_only_a_real_gpu_runs_the_model():
    for mode in ("cpu_fallback", "unavailable"):
        r = await _run(ReplayHeadtailKeypointsForMissionWorkflow.run, TARGET, mode=mode)
        assert (r.no_gpu, r.results) == (True, [])


async def test_the_run_replays_every_mission_past_one_that_fails():
    out = await _run(ReplayHeadtailKeypointsWorkflow.run,
                     KeypointReplayRequest(tenant="lab"), broken=(2,))  # fmt: skip
    rows = {r["number"]: r for r in out["missions"]}
    assert set(rows) == {1, 2, 3} and "error" in rows[2] and "error" not in rows[1]
    assert rows[1]["child"].endswith(str(IDS[1]))
    assert keypoint_child_id("headtail-keypoints-x", "m") == "headtail-keypoints-x-m"
