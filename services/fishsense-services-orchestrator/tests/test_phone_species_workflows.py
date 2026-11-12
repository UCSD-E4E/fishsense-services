"""The phone species' workflows.

Per mission: pages until none is left; the GPU queue is woken once there is
work, and nothing is sent when no worker can serve it; each page classified
on the GPU queue and persisted; unreadable masks and no bucket are counted.
The run: a dry run plans and digests and classifies nothing; apply refuses a
changed plan, runs the plan's missions, and goes on past one that fails.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import PROCESSOR_GPU_TASK_QUEUE
from fishsense_services_contracts.species_prediction import (
    PredictSpeciesImage,
    PredictSpeciesImagesInput,
    SpeciesCandidate,
    SpeciesPredictionResult,
)
from fishsense_services_contracts.temporal import DATA_CONVERTER

with workflow.unsafe.imports_passed_through():
    from fishsense_services_orchestrator.phone_species.activities import (
        PhoneSpeciesPage,
        PhoneSpeciesTarget,
    )
    from fishsense_services_orchestrator.phone_species.workflow import (
        PhoneSpeciesForMissionWorkflow,
        PhoneSpeciesRequest,
        PhoneSpeciesRunWorkflow,
        plan_digest,
    )

TENANT = uuid.uuid4()
SOCAL = "Southern California, USA"
QUEUE = "test-phone-species"
EVENTS: list = []
CANDIDATES = [SpeciesCandidate(choice="Fish, Kelp Bass (Paralabrax clathratus)",
                               scientific_name="Paralabrax clathratus")]  # fmt: skip


def _images(n):
    return [PredictSpeciesImage(capture_id=uuid.uuid4(), headtail_prediction_id=uuid.uuid4(),
                                jpeg_url="https://g/x", mask_bbox=[0, 0, 10, 10])
            for _ in range(n)]  # fmt: skip


def _stubs(*, pages=((2, 0, False),), missions=None, broken=(), gpu="gpu"):
    """`pages`: per page, (fish, unreadable masks, no bucket). `missions`: the
    plan's (number, fish)."""
    missions = missions or [(1, 2)]
    ids = {n: str(uuid.uuid5(uuid.NAMESPACE_OID, str(n))) for n, _ in missions}

    @activity.defn(name="plan_phone_species")
    async def plan(
        tenant: str, region: str, numbers: Optional[List[int]] = None
    ) -> dict:
        return {"tenant": tenant, "tenant_id": str(TENANT), "region": region,
                "species": 53,
                "missions": [{"mission_id": ids[n], "number": n, "captures": c}
                             for n, c in missions]}  # fmt: skip

    @activity.defn(name="resolve_phone_species_page")
    async def resolve(
        t: PhoneSpeciesTarget, after: Optional[str] = None
    ) -> PhoneSpeciesPage:
        if str(t.mission_id) in {ids[n] for n in broken}:
            raise RuntimeError("boom")
        i = 0 if after is None else int(after)
        EVENTS.append(("resolve", i, t.region))
        if i >= len(pages):
            return PhoneSpeciesPage(payload=None)
        n, unreadable, no_bucket = pages[i]
        if no_bucket:
            return PhoneSpeciesPage(payload=None, last=str(i + 1), unavailable=n,
                                    no_bucket=True)  # fmt: skip
        return PhoneSpeciesPage(
            payload=PredictSpeciesImagesInput(tenant_id=t.tenant_id, mission_id=t.mission_id,
                                              candidates=CANDIDATES, images=_images(n)),
            last=str(i + 1), unreadable=unreadable,
        )  # fmt: skip

    @activity.defn(name="persist_phone_species")
    async def persist(t: PhoneSpeciesTarget, r: List[SpeciesPredictionResult]) -> int:
        EVENTS.append(("persist", len(r)))
        return len(r)

    @activity.defn(name="ensure_gpu_processor_running")
    async def wake() -> str:
        EVENTS.append(("wake",))
        return gpu

    return [plan, resolve, persist, wake]


@workflow.defn(name="PredictSpeciesImagesWorkflow")
class _Gpu:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: dict) -> list:
        return [SpeciesPredictionResult(
            capture_id=i["capture_id"], headtail_prediction_id=i["headtail_prediction_id"],
            status="predicted", predictor_version=1, model_id="m",
            predicted_choice=CANDIDATES[0].choice, top1_probability=0.9, margin=0.8,
        ).model_dump(mode="json") for i in payload["images"]]  # fmt: skip


async def _run(fn, arg, **stubs):
    EVENTS.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue=QUEUE, activities=_stubs(**stubs),
                          workflows=[PhoneSpeciesForMissionWorkflow,
                                     PhoneSpeciesRunWorkflow]), \
                   Worker(env.client, task_queue=PROCESSOR_GPU_TASK_QUEUE,
                          workflows=[_Gpu]):  # fmt: skip
            return await env.client.execute_workflow(
                fn, arg, id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE
            )


TARGET = PhoneSpeciesTarget(TENANT, uuid.uuid4(), SOCAL)


async def test_a_mission_pages_until_done_and_counts_its_outcomes():
    counts = await _run(PhoneSpeciesForMissionWorkflow.run, TARGET,
                        pages=((2, 1, False), (1, 0, False)))  # fmt: skip
    assert counts == {"predicted": 3, "recorded": 3, "unreadable_mask": 1}
    assert EVENTS == [("resolve", 0, SOCAL), ("wake",), ("persist", 2),
                      ("resolve", 1, SOCAL), ("persist", 1),
                      ("resolve", 2, SOCAL)]  # fmt: skip


async def test_a_page_with_no_bucket_wakes_nothing():
    counts = await _run(PhoneSpeciesForMissionWorkflow.run, TARGET,
                        pages=((3, 0, True),))  # fmt: skip
    assert counts == {"no_bucket": 3}
    assert ("wake",) not in EVENTS


async def test_no_gpu_worker_sends_nothing():
    with pytest.raises(WorkflowFailureError):
        await _run(PhoneSpeciesForMissionWorkflow.run, TARGET, gpu="unavailable")
    assert EVENTS == [("resolve", 0, SOCAL), ("wake",)]


def test_the_digest_is_the_region_and_missions_not_the_counts():
    def plan(region, mission, n):
        return {"tenant_id": "t", "region": region,
                "missions": [{"mission_id": mission, "number": 1, "captures": n}]}  # fmt: skip

    assert plan_digest(plan(SOCAL, "x", 5)) == plan_digest(plan(SOCAL, "x", 3))
    assert plan_digest(plan(SOCAL, "x", 5)) != plan_digest(plan(SOCAL, "y", 5))
    assert plan_digest(plan(SOCAL, "x", 5)) != plan_digest(
        plan("Florida Keys, USA", "x", 5)
    )


async def test_a_dry_run_plans_and_classifies_nothing():
    report = await _run(PhoneSpeciesRunWorkflow.run,
                        PhoneSpeciesRequest(tenant="ccfrp-ucsd", region=SOCAL),
                        missions=[(1, 10), (2, 5)])  # fmt: skip
    assert report["mode"] == "dry_run" and report["missions"] == []
    assert report["captures"] == 15 and report["region"] == SOCAL
    assert EVENTS == []


async def test_apply_refuses_a_changed_plan():
    with pytest.raises(WorkflowFailureError):
        await _run(PhoneSpeciesRunWorkflow.run,
                   PhoneSpeciesRequest(tenant="ccfrp-ucsd", region=SOCAL, apply=True,
                                       expected_plan_sha256="stale"))  # fmt: skip
    assert EVENTS == []


async def test_apply_runs_every_mission_and_goes_on_past_a_failing_one():
    request = PhoneSpeciesRequest(tenant="ccfrp-ucsd", region=SOCAL)
    stubs = dict(missions=[(1, 2), (2, 2)], broken=(1,))
    dry = await _run(PhoneSpeciesRunWorkflow.run, request, **stubs)
    request.apply, request.expected_plan_sha256 = True, dry["plan_sha256"]

    report = await _run(PhoneSpeciesRunWorkflow.run, request, **stubs)

    first, second = report["missions"]
    assert "error" in first and second["counts"]["recorded"] == 2
