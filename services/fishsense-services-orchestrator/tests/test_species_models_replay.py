"""The BioCLIP model-class replay (read-only): the model classes are the
species config's "Fish Model" leaves, each with prompts of its own; a mission
pages its frames (the keypoint replay's) to the GPU with both candidate sets
on a real GPU only; the run goes on past a mission that fails."""

from __future__ import annotations

import uuid
from typing import List, Optional

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts import PROCESSOR_GPU_TASK_QUEUE
from fishsense_services_contracts.headtail_keypoints import (
    KeypointFrame,
    KeypointFramesInput,
)
from fishsense_services_contracts.species_models import SpeciesModelsResult
from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_orchestrator.headtail_keypoints.activities import (
    KeypointPage,
    KeypointTarget,
)
from fishsense_services_orchestrator.species_models.candidates import (
    MODEL_DESCRIPTIONS,
    model_candidates,
)
from fishsense_services_orchestrator.species_models.workflow import (
    ReplaySpeciesModelsForMissionWorkflow,
    ReplaySpeciesModelsWorkflow,
    SpeciesModelsMissionResult,
    SpeciesModelsRequest,
)


def test_every_fish_model_leaf_is_a_class_with_its_own_prompts():
    cands = model_candidates()
    assert {c.choice for c in cands} == {f"Fish Model, {k}" for k in MODEL_DESCRIPTIONS}
    assert len(cands) == 7 and all(len(c.prompts) == 4 for c in cands)
    assert all("plastic" in " ".join(c.prompts) for c in cands)
    # no environment cue: models turn up outside the pool too
    assert not any(w in p for c in cands for p in c.prompts for w in ("pool", "reef"))


def test_a_fish_model_leaf_without_a_description_fails_loudly():
    xml = """<View><Taxonomy name="species" toName="image">
      <Choice value="Fish Model"><Choice value="Snook"/><Choice value="Dolphin"/></Choice>
    </Taxonomy></View>"""
    with pytest.raises(ValueError, match="Dolphin"):
        model_candidates(xml)


TENANT = uuid.uuid4()
QUEUE = "test-species-models"
EVENTS: list = []
IDS = {n: uuid.uuid5(uuid.NAMESPACE_OID, f"s{n}") for n in range(1, 5)}


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
        return KeypointPage(payload=KeypointFramesInput(tenant_id=t.tenant_id, mission_id=t.mission_id,
                                                        frames=frames) if n else None,
                            last_seq=after + 1, unavailable=gone)  # fmt: skip

    @activity.defn(name="ensure_gpu_processor_running")
    async def wake() -> str:
        EVENTS.append(("wake",))
        return mode

    return [plan, resolve, wake]


@activity.defn(name="_seen")
async def _seen(e: list) -> None:
    EVENTS.append(tuple(e))


@workflow.defn(name="ScoreSpeciesModelsWorkflow")
class _Gpu:  # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: dict) -> list:
        from datetime import timedelta  # pylint: disable=import-outside-toplevel

        await workflow.execute_activity("_seen", ["candidates", len(payload["species"]),
                                                  len(payload["models"])],
                                        start_to_close_timeout=timedelta(seconds=10))  # fmt: skip
        return [SpeciesModelsResult(prediction_id=f["prediction_id"], capture_id=f["capture_id"],
                                    status="scored").model_dump(mode="json")
                for f in payload["frames"]]  # fmt: skip


async def _run(fn, arg, **stubs):
    EVENTS.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue=QUEUE, activities=_stubs(**stubs),
                          workflows=[ReplaySpeciesModelsForMissionWorkflow,
                                     ReplaySpeciesModelsWorkflow]), \
                   Worker(env.client, task_queue=PROCESSOR_GPU_TASK_QUEUE, workflows=[_Gpu],
                          activities=[_seen]):  # fmt: skip
            return await env.client.execute_workflow(
                fn, arg, id=f"{QUEUE}-{uuid.uuid4()}", task_queue=QUEUE
            )


TARGET = KeypointTarget(TENANT, IDS[1])


async def test_a_mission_is_scored_with_both_candidate_sets_and_returns_every_result():
    r: SpeciesModelsMissionResult = await _run(
        ReplaySpeciesModelsForMissionWorkflow.run, TARGET, pages=((3, 1), (2, 0))
    )
    assert (len(r.results), r.unavailable, r.no_gpu) == (5, 1, False)
    assert ("candidates", 13, 7) in EVENTS


async def test_only_a_real_gpu_scores():
    r = await _run(
        ReplaySpeciesModelsForMissionWorkflow.run, TARGET, mode="cpu_fallback"
    )
    assert (r.no_gpu, r.results) == (True, [])


async def test_the_run_goes_on_past_a_mission_that_fails():
    out = await _run(ReplaySpeciesModelsWorkflow.run, SpeciesModelsRequest(tenant="lab"),
                     broken=(2,))  # fmt: skip
    rows = {r["number"]: r for r in out["missions"]}
    assert set(rows) == {1, 2, 3} and "error" in rows[2] and "error" not in rows[1]
