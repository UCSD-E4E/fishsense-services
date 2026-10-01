"""Every per-dive Label Studio kind creates then populates on one policy.

v1 had one `_populate` module (`create_then_populate`, `_POPULATE_RETRY`) that
every kind called. The slices each ported a copy, and two drifted: the laser
and head/tail create steps took Temporal's default policy, so a tenant the
orchestrator no longer serves (`NotAMember`) retried forever instead of
failing the child. One policy, in `labels.populate_policy`, pinned here for
every kind.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

import pytest
from temporalio import workflow

from fishsense_services_orchestrator.headtail import workflow as headtail
from fishsense_services_orchestrator.headtail.activities import HeadtailTarget
from fishsense_services_orchestrator.labels import populate_policy
from fishsense_services_orchestrator.laser import workflow as laser
from fishsense_services_orchestrator.laser.contracts import LaserTarget
from fishsense_services_orchestrator.object_store.contracts import StagingTarget
from fishsense_services_orchestrator.slates import workflows as slates
from fishsense_services_orchestrator.species import workflows as species
from fishsense_services_orchestrator.species.contracts import SpeciesTarget

TENANT, DIVE = uuid.uuid4(), uuid.uuid4()

KINDS = {
    "species": lambda: species.create_then_populate(SpeciesTarget(TENANT, DIVE)),
    "headtail": lambda: headtail.create_then_populate(HeadtailTarget(TENANT, DIVE)),
    "laser": lambda: laser.PopulateLaserLabelStudioProjectWorkflow().run(
        LaserTarget(tenant_id=TENANT, dive_id=DIVE)
    ),
    "slate": lambda: slates.PopulateDiveSlateLabelStudioProjectWorkflow().run(
        StagingTarget(TENANT, DIVE)
    ),
}


@pytest.fixture(name="calls")
def _calls(monkeypatch):
    seen: List[Dict[str, Any]] = []

    async def fake_execute_activity(name, *args, **kwargs):
        seen.append({"name": name, **kwargs})
        return 7 if name.startswith("create_") else 42

    monkeypatch.setattr(workflow, "execute_activity", fake_execute_activity)
    return seen


@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_creating_the_project_fails_fast_for_a_tenant_not_served(kind, calls):
    await KINDS[kind]()

    create = calls[0]
    assert create["name"].startswith("create_")
    policy = create.get("retry_policy")
    assert policy is not None, "the default policy retries NotAMember forever"
    assert "NotAMember" in (policy.non_retryable_error_types or [])


@pytest.mark.parametrize("kind", sorted(KINDS))
async def test_populating_takes_v1s_one_bounded_policy(kind, calls):
    assert await KINDS[kind]() == 42

    populate = calls[1]
    assert populate["name"].startswith("populate_")
    assert populate["retry_policy"] is populate_policy.POPULATE_RETRY


def test_the_one_policy_is_v1s():
    policy = populate_policy.POPULATE_RETRY
    assert policy.maximum_attempts == populate_policy.POPULATE_MAX_ATTEMPTS == 5
    assert {"NotAMember", "ForeignRows"} <= set(policy.non_retryable_error_types)


CREATE_ONLY = {
    "species": lambda: species.CreateSpeciesLabelStudioProjectWorkflow().run(
        SpeciesTarget(TENANT, DIVE)
    ),
    "headtail": lambda: headtail.CreateHeadTailLabelStudioProjectWorkflow().run(
        HeadtailTarget(TENANT, DIVE)
    ),
    "laser": lambda: laser.CreateLaserLabelStudioProjectWorkflow().run(
        LaserTarget(tenant_id=TENANT, dive_id=DIVE)
    ),
    "slate": lambda: slates.CreateDiveSlateLabelStudioProjectWorkflow().run(
        StagingTarget(TENANT, DIVE)
    ),
}


@pytest.mark.parametrize("kind", sorted(CREATE_ONLY))
async def test_the_create_only_workflow_takes_the_same_create_policy(kind, calls):
    await CREATE_ONLY[kind]()

    assert calls[0]["retry_policy"] is populate_policy.CREATE_PROJECT_RETRY
