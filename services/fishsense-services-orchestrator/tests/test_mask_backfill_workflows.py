"""The mask backfill's workflows.

Per mission: pages until none is left; the GPU is woken once there is work
and only a real GPU regenerates; each page's outcomes counted and its
reproduced masks persisted; a page whose images are all gone wakes nothing.
The run: a dry run plans and digests; apply refuses a changed plan, runs its
part's missions, goes on past one that fails, and stops without a GPU.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import List, Optional

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import PROCESSOR_GPU_TASK_QUEUE
from fishsense_services_contracts.mask_backfill import (
    BackfilledMask,
    BackfillMaskFrame,
    BackfillMasksInput,
    MaskExpectation,
)
from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_orchestrator.mask_backfill.activities import (
    MaskBackfillPage,
    MaskBackfillTarget,
)
from fishsense_services_orchestrator.mask_backfill.workflow import (
    MaskBackfillForMissionWorkflow,
    MaskBackfillRequest,
    MaskBackfillRunWorkflow,
    plan_digest,
    split,
)

TENANT = uuid.uuid4()
QUEUE = "test-mask-backfill"
EVENTS: list = []
EXPECT = MaskExpectation(mask_area_px=100, crop_x=1, crop_y=2)


def _frames(n):
    return [BackfillMaskFrame(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                              dots=[[1.0, 2.0]], render_url="u", expect=EXPECT)
            for _ in range(n)]  # fmt: skip


def _stubs(*, pages=((2, 0),), mode="gpu", missions=None, broken=()):
    """`pages`: per page, (frames, unavailable); the page after the last is
    the end. `missions`: the plan's (number, predictions)."""
    missions = missions or [(1, 2)]
    ids = {n: str(uuid.uuid5(uuid.NAMESPACE_OID, str(n))) for n, _ in missions}

    @activity.defn(name="plan_mask_backfill")
    async def plan(
        tenant: str, source: str, numbers: Optional[List[int]] = None
    ) -> dict:
        return {"tenant": tenant, "tenant_id": str(TENANT), "source": source,
                "missions": [{"mission_id": ids[n], "number": n, "predictions": p}
                             for n, p in missions]}  # fmt: skip

    @activity.defn(name="resolve_mask_backfill_page")
    async def resolve(t: MaskBackfillTarget, after: int = 0) -> MaskBackfillPage:
        if str(t.mission_id) in {ids[n] for n in broken}:
            raise ApplicationError("boom", type="Boom", non_retryable=True)
        EVENTS.append(("resolve", after))
        if after >= len(pages):
            return MaskBackfillPage(payload=None)
        n, gone = pages[after]
        return MaskBackfillPage(
            payload=BackfillMasksInput(tenant_id=t.tenant_id, source=t.source,
                                       frames=_frames(n)) if n else None,
            last_seq=after + 1, unavailable=gone)  # fmt: skip

    @activity.defn(name="persist_mask_backfill")
    async def persist(t: MaskBackfillTarget, r: List[BackfilledMask]) -> int:
        EVENTS.append(("persist", len(r)))
        return sum(x.outcome == "reproduced" for x in r)

    @activity.defn(name="ensure_gpu_processor_running")
    async def wake() -> str:
        EVENTS.append(("wake",))
        return mode

    return [plan, resolve, persist, wake]


@workflow.defn(name="BackfillMasksWorkflow")
class _Gpu:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: dict) -> list:
        frames = payload["frames"]
        return [BackfilledMask(prediction_id=f["prediction_id"], capture_id=f["capture_id"],
                               outcome="reproduced" if i % 2 == 0 else "mismatch"
                               ).model_dump(mode="json")
                for i, f in enumerate(frames)]  # fmt: skip


async def _run(fn, arg, **stubs):
    EVENTS.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue=QUEUE, activities=_stubs(**stubs),
                          workflows=[MaskBackfillForMissionWorkflow,
                                     MaskBackfillRunWorkflow]), \
                   Worker(env.client, task_queue=PROCESSOR_GPU_TASK_QUEUE,
                          workflows=[_Gpu]):  # fmt: skip
            return await env.client.execute_workflow(
                fn, arg, id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE
            )


TARGET = MaskBackfillTarget(TENANT, uuid.uuid4(), "automatic")


async def test_a_mission_pages_until_done_and_counts_its_outcomes():
    counts = await _run(MaskBackfillForMissionWorkflow.run, TARGET,
                        pages=((3, 1), (2, 0)))  # fmt: skip
    assert counts == {
        "reproduced": 3,
        "mismatch": 2,
        "image_unavailable": 1,
        "named": 3,
    }
    assert EVENTS == [("resolve", 0), ("wake",), ("persist", 3), ("resolve", 1),
                      ("persist", 2), ("resolve", 2)]  # fmt: skip


async def test_only_a_real_gpu_regenerates():
    for mode in ("cpu_fallback", "unavailable"):
        counts = await _run(MaskBackfillForMissionWorkflow.run, TARGET, mode=mode)
        assert counts == {"no_gpu": 1} and ("persist", 2) not in EVENTS


async def test_a_page_of_gone_images_wakes_nothing():
    counts = await _run(MaskBackfillForMissionWorkflow.run, TARGET, pages=((0, 4),))
    assert counts == {"image_unavailable": 4} and ("wake",) not in EVENTS


def test_the_digest_is_the_missions_not_the_counts():
    plan = {"tenant_id": "t", "source": "automatic",
            "missions": [{"mission_id": "a", "number": 1, "predictions": 5}]}  # fmt: skip
    fewer = {**plan, "missions": [{**plan["missions"][0], "predictions": 2}]}
    other = {**plan, "missions": [{**plan["missions"][0], "mission_id": "b"}]}
    assert plan_digest(plan) == plan_digest(fewer) != plan_digest(other)


def test_parts_balance_predictions():
    missions = [{"mission_id": str(n), "number": n, "predictions": p}
                for n, p in [(1, 900), (2, 500), (3, 400), (4, 100)]]  # fmt: skip
    shares = split(missions, 2)
    assert sorted(sum(m["predictions"] for m in s) for s in shares) == [900, 1000]
    assert sorted(m["number"] for s in shares for m in s) == [1, 2, 3, 4]


async def test_a_dry_run_plans_and_runs_nothing():
    report = await _run(MaskBackfillRunWorkflow.run,
                        MaskBackfillRequest(tenant="lab", source="automatic", parts=2),
                        missions=[(1, 10), (2, 5)])  # fmt: skip
    assert report["mode"] == "dry_run" and report["missions"] == []
    assert sorted(report["parts"]) == [[1], [2]] and EVENTS == []


async def test_apply_refuses_a_changed_plan():
    with pytest.raises(WorkflowFailureError):
        await _run(MaskBackfillRunWorkflow.run,
                   MaskBackfillRequest(tenant="lab", source="automatic", apply=True,
                                       expected_plan_sha256="stale"))  # fmt: skip
    assert EVENTS == []


async def _apply(**stubs):
    dry = await _run(MaskBackfillRunWorkflow.run,
                     MaskBackfillRequest(tenant="lab", source="automatic"), **stubs)  # fmt: skip
    return await _run(MaskBackfillRunWorkflow.run,
                      MaskBackfillRequest(tenant="lab", source="automatic", apply=True,
                                          expected_plan_sha256=dry["plan_sha256"]),
                      **stubs)  # fmt: skip


async def test_apply_goes_on_past_a_failing_mission():
    report = await _apply(missions=[(1, 2), (2, 2)], broken=(1,))
    first, second = report["missions"]
    assert "error" in first and second["counts"]["named"] == 1


async def test_apply_stops_without_a_gpu():
    report = await _apply(missions=[(1, 2), (2, 2)], mode="unavailable")
    assert len(report["missions"]) == 1 and report["stopped"] == "no GPU"
