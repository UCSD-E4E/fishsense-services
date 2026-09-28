"""Processor roles: which queue a worker polls, and what it registers.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_worker_roles.py. v1's reasons, kept: the processor
runs one role per Deployment, each on its own queue, and the failure modes of
getting the split wrong are silent in both directions -- a workflow or activity
registered nowhere sits pending until its timeout with nothing logged, and one
registered on the wrong queue lands on a pod that may lack what it needs (a GPU,
or the memory for a full-res decode).

v2 changes, each pinned below:

* **the roles are declared by the stages.** v1 kept three hand-maintained lists
  in roles.py; v2 stages declare their role in `<package>/stage.py` (like the
  orchestrator's registry), so porting a stage edits nothing shared. "In
  exactly one role" then holds by construction for a stage, and what is left
  to check is that no name is registered by two stages;
* the per-image role is ``per_image``, not v1's ``cpu``: the light role is CPU
  too, and the name says what the role is for;
* **no ``all`` role.** v1 ran every queue in one process for its devcontainer.
  v2's compose runs a processor per role it needs, each with its own cap, so a
  local run exercises the same concurrency the pods do;
* a role with nothing registered fails at startup with a message that says so,
  rather than Temporal's generic "at least one workflow or activity".
"""

from __future__ import annotations

import inspect
import re
from datetime import timedelta

import pytest
from temporalio.testing import WorkflowEnvironment

from fishsense_services_contracts import (
    PROCESSOR_GPU_TASK_QUEUE,
    PROCESSOR_LIGHT_TASK_QUEUE,
    PROCESSOR_TASK_QUEUE,
)
from fishsense_services_processor import registry
from fishsense_services_processor.clustering.activities import cluster_dive_frames
from fishsense_services_processor.clustering.workflow import (
    DiveFrameClusteringWorkflow,
)
from fishsense_services_processor.registry import Stage, registration_for_role
from fishsense_services_processor.worker import (
    GRACEFUL_SHUTDOWN_TIMEOUT,
    build_worker,
)


def _stage(role: str) -> Stage:
    """A stage in any role. It borrows clustering's workflow (a workflow
    defined in a test module can't pass the workflow sandbox's validation):
    what is under test is the role's wiring, not the work."""
    return Stage(
        name=f"fake-{role}",
        role=role,
        workflows=[DiveFrameClusteringWorkflow],
        activities=[cluster_dive_frames],
    )


def test_role_queues_are_distinct_and_come_from_the_shared_contract():
    assert registry.ROLE_TASK_QUEUES == {
        registry.ROLE_PER_IMAGE: PROCESSOR_TASK_QUEUE,
        registry.ROLE_GPU: PROCESSOR_GPU_TASK_QUEUE,
        registry.ROLE_LIGHT: PROCESSOR_LIGHT_TASK_QUEUE,
    }
    assert len(set(registry.ROLE_TASK_QUEUES.values())) == 3


def test_every_stage_declares_one_of_the_roles():
    assert registry.stages(), "no processor stages discovered"
    for stage in registry.stages():
        assert stage.role in registry.ROLES, stage


def test_every_registration_lands_in_exactly_one_role():
    """No workflow or activity may be registered by two stages -- two stages
    in different roles would put it on two queues, and two in the same role
    would fail the worker's startup. (A stage declares one role, so "in none"
    can't happen to anything a stage lists.)"""
    workflows = [
        w.__temporal_workflow_definition.name
        for s in registry.stages()
        for w in s.workflows
    ]
    activities = [
        a.__temporal_activity_definition.name
        for s in registry.stages()
        for a in s.activities
    ]

    assert len(set(workflows)) == len(workflows), sorted(workflows)
    assert len(set(activities)) == len(activities), sorted(activities)


def test_every_activity_a_workflow_calls_is_registered_in_its_role():
    """An activity runs on its workflow's task queue, so the two must share a
    role: otherwise `execute_activity` waits on a queue whose pod doesn't
    serve it, until schedule-to-close. Read from the workflows' source, as the
    orchestrator's registry test does."""
    for stage in registry.stages():
        served = {
            a.__temporal_activity_definition.name
            for s in registry.stages()
            if s.role == stage.role
            for a in s.activities
        }
        for wf in stage.workflows:
            called = set(
                re.findall(
                    r'execute_activity\(\s*"([A-Za-z0-9_]+)"',
                    inspect.getsource(inspect.getmodule(wf)),
                )
            )
            assert called <= served, (stage.name, sorted(called - served))


def test_clustering_is_a_light_stage():
    """Stage 1 holds no image bytes -- timestamps in, groups out -- so it must
    not wait behind the per-image role's memory cap (v1 moved it to the light
    queue on 2026-09-04, after sub-second work expired on ScheduleToStart)."""
    registration = registration_for_role(registry.ROLE_LIGHT)
    assert DiveFrameClusteringWorkflow in registration.workflows
    assert cluster_dive_frames in registration.activities


def test_the_per_image_role_is_capped_at_two_concurrent_activities():
    """A memory ceiling, not a throughput choice: each per-image activity
    decodes a full-res `.ORF` and peaks at 1-3 GB. v1's pod OOMKilled at the
    SDK default of 100 and again at 4 (17 restarts, 2026-07-21), so the cap is
    2 and throughput comes from replicas."""
    assert registry.ROLE_MAX_CONCURRENT_ACTIVITIES[registry.ROLE_PER_IMAGE] == 2


def test_the_light_role_is_not_bound_by_the_decoders_cap():
    """Nothing on the light queue decodes an image, so the per-image cap does
    not apply (v1's light pod: 8)."""
    assert registry.ROLE_MAX_CONCURRENT_ACTIVITIES[registry.ROLE_LIGHT] == 8


@pytest.mark.parametrize(
    ("role", "expected_queue"),
    [
        (registry.ROLE_PER_IMAGE, PROCESSOR_TASK_QUEUE),
        (registry.ROLE_GPU, PROCESSOR_GPU_TASK_QUEUE),
        (registry.ROLE_LIGHT, PROCESSOR_LIGHT_TASK_QUEUE),
    ],
)
async def test_build_worker_polls_the_queue_for_its_role(role, expected_queue):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = build_worker(env.client, role=role, stages=[_stage(role)])
        config = worker.config()
        assert config["task_queue"] == expected_queue
        assert (
            config["max_concurrent_activities"]
            == registry.ROLE_MAX_CONCURRENT_ACTIVITIES[role]
        )
        # Tear-down deletes the Deployment, which SIGTERMs a pod that may be
        # mid-activity: give it v1's window to finish (the manifests'
        # terminationGracePeriodSeconds is longer).
        assert config["graceful_shutdown_timeout"] == GRACEFUL_SHUTDOWN_TIMEOUT
        assert GRACEFUL_SHUTDOWN_TIMEOUT == timedelta(seconds=30)


async def test_a_stage_serves_only_its_own_role():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = build_worker(
            env.client,
            role=registry.ROLE_GPU,
            stages=[_stage(registry.ROLE_GPU), _stage(registry.ROLE_LIGHT)],
        )
        assert worker.config()["workflows"] == [DiveFrameClusteringWorkflow]


async def test_a_pod_may_lower_its_cap():
    """The GPU queue's CPU fallback runs one image at a time (v1's
    `MAX_CONCURRENT_ACTIVITIES=1` on that Deployment)."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = build_worker(
            env.client,
            role=registry.ROLE_GPU,
            stages=[_stage(registry.ROLE_GPU)],
            max_concurrent_activities=1,
        )
        assert worker.config()["max_concurrent_activities"] == 1


@pytest.mark.parametrize("role", ["tpu", "all", "cpu"])
async def test_build_worker_rejects_an_unknown_role(role):
    """Including v1's ``all`` and ``cpu``: a pod configured with v1's
    vocabulary must fail at startup, not quietly serve some other queue."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with pytest.raises(ValueError, match="role"):
            build_worker(env.client, role=role, stages=[_stage("light")])


async def test_a_role_with_nothing_registered_fails_loudly():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        with pytest.raises(ValueError, match="nothing registered"):
            build_worker(env.client, role=registry.ROLE_GPU, stages=[])
