"""The phone measurement's workflows.

Per mission: pages until none is left; the per-image processor is woken once
there is work; each page measured on the per-image queue and its results
persisted; a page with no bucket wakes nothing. The run: a dry run plans and
digests and measures nothing; apply refuses a changed plan, runs the plan's
missions, and goes on past one that fails.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import PROCESSOR_TASK_QUEUE
from fishsense_services_contracts.phone_measurement import (
    PhoneFrame,
    PhoneMeasured,
    PhoneMeasureInput,
)
from fishsense_services_contracts.temporal import DATA_CONVERTER

# This module defines a workflow, so the sandbox imports it: the activities'
# module reaches boto3, which is passed through as the workflow module does.
with workflow.unsafe.imports_passed_through():
    from fishsense_services_orchestrator.phone_measurement.activities import (
        PhoneMeasurementPage,
        PhoneMeasurementTarget,
    )
    from fishsense_services_orchestrator.phone_measurement.workflow import (
        PhoneMeasurementForMissionWorkflow,
        PhoneMeasurementRequest,
        PhoneMeasurementRunWorkflow,
        plan_digest,
    )

TENANT = uuid.uuid4()
QUEUE = "test-phone-measurement"
EVENTS: list = []
K = [1604.2147, 0.0, 956.5816, 0.0, 1604.2147, 717.7617, 0.0, 0.0, 1.0]


def _frames(n):
    return [PhoneFrame(capture_id=uuid.uuid4(), rgb_url="r", depth_url="d", depth_width=256,
                       depth_height=192, depth_row_bytes=1024, intrinsics=K)
            for _ in range(n)]  # fmt: skip


def _stubs(*, pages=((2, False),), missions=None, broken=()):
    """`pages`: per page, (captures, no bucket); the page after the last is
    the end. `missions`: the plan's (number, captures)."""
    missions = missions or [(1, 2)]
    ids = {n: str(uuid.uuid5(uuid.NAMESPACE_OID, str(n))) for n, _ in missions}

    @activity.defn(name="plan_phone_measurement")
    async def plan(tenant: str, numbers: Optional[List[int]] = None) -> dict:
        return {"tenant": tenant, "tenant_id": str(TENANT),
                "missions": [{"mission_id": ids[n], "number": n, "captures": c}
                             for n, c in missions]}  # fmt: skip

    @activity.defn(name="resolve_phone_measurement_page")
    async def resolve(
        t: PhoneMeasurementTarget, after: Optional[str] = None
    ) -> PhoneMeasurementPage:
        if str(t.mission_id) in {ids[n] for n in broken}:
            raise ApplicationError("boom", type="Boom", non_retryable=True)
        i = 0 if after is None else int(after)
        EVENTS.append(("resolve", i))
        if i >= len(pages):
            return PhoneMeasurementPage(payload=None)
        n, no_bucket = pages[i]
        if no_bucket:
            return PhoneMeasurementPage(payload=None, last=str(i + 1), unavailable=n,
                                        no_bucket=True)  # fmt: skip
        return PhoneMeasurementPage(
            payload=PhoneMeasureInput(tenant_id=t.tenant_id, frames=_frames(n)),
            last=str(i + 1),
        )

    @activity.defn(name="persist_phone_measurements")
    async def persist(t: PhoneMeasurementTarget, r: List[PhoneMeasured]) -> int:
        EVENTS.append(("persist", len(r)))
        return len(r)

    @activity.defn(name="ensure_per_image_processor_running")
    async def wake() -> None:
        EVENTS.append(("wake",))

    return [plan, resolve, persist, wake]


@workflow.defn(name="MeasurePhoneCapturesWorkflow")
class _PerImage:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: dict) -> list:
        return [PhoneMeasured(capture_id=f["capture_id"],
                              status="no_fish" if i % 2 else "image_unavailable"
                              ).model_dump(mode="json")
                for i, f in enumerate(payload["frames"])]  # fmt: skip


async def _run(fn, arg, **stubs):
    EVENTS.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue=QUEUE, activities=_stubs(**stubs),
                          workflows=[PhoneMeasurementForMissionWorkflow,
                                     PhoneMeasurementRunWorkflow]), \
                   Worker(env.client, task_queue=PROCESSOR_TASK_QUEUE,
                          workflows=[_PerImage]):  # fmt: skip
            return await env.client.execute_workflow(
                fn, arg, id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE
            )


TARGET = PhoneMeasurementTarget(TENANT, uuid.uuid4())


async def test_a_mission_pages_until_done_and_counts_its_outcomes():
    counts = await _run(
        PhoneMeasurementForMissionWorkflow.run, TARGET, pages=((2, False), (1, False))
    )
    assert counts == {"image_unavailable": 2, "no_fish": 1, "recorded": 3}
    assert EVENTS == [("resolve", 0), ("wake",), ("persist", 2), ("resolve", 1),
                      ("persist", 1), ("resolve", 2)]  # fmt: skip


async def test_a_page_with_no_bucket_wakes_nothing():
    counts = await _run(
        PhoneMeasurementForMissionWorkflow.run, TARGET, pages=((3, True),)
    )
    assert counts == {"no_bucket": 3}
    assert ("wake",) not in EVENTS


def test_the_digest_is_the_missions_not_the_counts():
    a = {
        "tenant_id": "t",
        "missions": [{"mission_id": "x", "number": 1, "captures": 5}],
    }
    b = {
        "tenant_id": "t",
        "missions": [{"mission_id": "x", "number": 1, "captures": 3}],
    }
    c = {
        "tenant_id": "t",
        "missions": [{"mission_id": "y", "number": 2, "captures": 5}],
    }
    assert plan_digest(a) == plan_digest(b) != plan_digest(c)


async def test_a_dry_run_plans_and_measures_nothing():
    report = await _run(PhoneMeasurementRunWorkflow.run, PhoneMeasurementRequest(tenant="ccfrp-ucsd"),
                        missions=[(1, 10), (2, 5)])  # fmt: skip
    assert report["mode"] == "dry_run" and report["missions"] == []
    assert report["captures"] == 15 and report["plan_sha256"]
    assert EVENTS == []


async def test_apply_refuses_a_changed_plan():
    with pytest.raises(WorkflowFailureError):
        await _run(PhoneMeasurementRunWorkflow.run,
                   PhoneMeasurementRequest(tenant="ccfrp-ucsd", apply=True,
                                           expected_plan_sha256="stale"))  # fmt: skip
    assert EVENTS == []


async def _apply(**stubs):
    dry = await _run(PhoneMeasurementRunWorkflow.run,
                     PhoneMeasurementRequest(tenant="ccfrp-ucsd"), **stubs)  # fmt: skip
    return await _run(PhoneMeasurementRunWorkflow.run,
                      PhoneMeasurementRequest(tenant="ccfrp-ucsd", apply=True,
                                              expected_plan_sha256=dry["plan_sha256"]),
                      **stubs)  # fmt: skip


async def test_apply_runs_every_mission_and_goes_on_past_a_failing_one():
    report = await _apply(missions=[(1, 2), (2, 2)], broken=(1,))
    first, second = report["missions"]
    assert "error" in first and second["counts"]["recorded"] == 2
