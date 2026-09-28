"""The stage registry: how the orchestrator finds what it serves.

Each stage package declares itself in a `stage.py` -- its workflows, its
schedules, and how to build its activities from the shared dependencies -- and
the worker discovers them. Adding a stage adds a package and edits nothing
shared, which is what lets stages be ported on parallel branches without
colliding in one registration list.
"""

from __future__ import annotations

import inspect
import re

import pytest

from fishsense_services_orchestrator.registry import Deps, Stage, stages
from fishsense_services_orchestrator.schedules import SCHEDULES
from fishsense_services_orchestrator.worker import WORKFLOWS

NAS = {
    "FISHSENSE_NAS_URL": "https://nas.example.test:6021",
    "FISHSENSE_NAS_USERNAME": "svc",
    "FISHSENSE_NAS_PASSWORD": "unused",
    "FISHSENSE_NAS_RAW_ROOT_PATH": "/fishsense_data/REEF/data",
    "FISHSENSE_LABEL_STUDIO_URL": "https://label-studio.example.test",
    "FISHSENSE_LABEL_STUDIO_API_KEY": "unused",
    "FISHSENSE_OBJECT_STORE_ENDPOINT_URL": "https://s3.example.test",
    "FISHSENSE_OBJECT_STORE_REGION": "garage",
    "FISHSENSE_OBJECT_STORE_ACCESS_KEY_ID": "unused",
    "FISHSENSE_OBJECT_STORE_SECRET_ACCESS_KEY": "unused",
    "FISHSENSE_OBJECT_STORE_BUCKET": "fishsense-lite",
    "FISHSENSE_OBJECT_STORE_LEGACY_LABELS_PREFIX": "fishsense-lite",
    # Where Label Studio presigns the processed JPEGs (the populate stages').
    "FISHSENSE_LABEL_STUDIO_S3_BUCKET": "labels-fishsense-lite",
    "FISHSENSE_LABEL_STUDIO_S3_ENDPOINT_URL": "https://s3.example.test",
    "FISHSENSE_LABEL_STUDIO_S3_REGION": "garage",
    "FISHSENSE_LABEL_STUDIO_S3_ACCESS_KEY": "unused",
    "FISHSENSE_LABEL_STUDIO_S3_SECRET_KEY": "unused",
}


@pytest.fixture
def deps(monkeypatch) -> Deps:
    for name, value in NAS.items():
        monkeypatch.setenv(name, value)
    # Catalogs only hold the engine until they're used; nothing connects here.
    return Deps(engine=None, sub="service:fishsense-orchestrator")


def test_the_stages_ported_so_far_are_discovered():
    assert {"ingest", "clustering", "labels", "nrp"} <= {s.name for s in stages()}
    assert all(isinstance(s, Stage) for s in stages())


def test_every_stage_workflow_is_served_and_none_twice():
    served = [w for s in stages() for w in s.workflows]

    assert WORKFLOWS == served
    assert len({w.__temporal_workflow_definition.name for w in served}) == len(served)


def test_activity_names_are_unique_across_stages(deps):
    names = [
        activity.__temporal_activity_definition.name
        for stage in stages()
        for activity in stage.build_activities(deps)
    ]

    assert names, "no activities built"
    assert len(set(names)) == len(names), sorted(names)


def test_every_activity_a_workflow_calls_is_built(deps):
    """Read from the workflows' own source, so it checks the real calls: a
    misspelt or unregistered name would otherwise sit unpolled until its
    timeout, with nothing in the logs."""
    built = {
        activity.__temporal_activity_definition.name
        for stage in stages()
        for activity in stage.build_activities(deps)
    }
    called = {
        name
        for workflow in WORKFLOWS
        for name in re.findall(
            r'execute_activity\(\s*"([A-Za-z0-9_]+)"',
            inspect.getsource(inspect.getmodule(workflow)),
        )
    }

    assert called, "no activity calls found"
    assert called <= built, sorted(called - built)


def test_schedules_come_from_the_stages_and_each_runs_a_served_workflow():
    assert SCHEDULES == tuple(sch for s in stages() for sch in s.schedules)
    assert {sch.workflow for sch in SCHEDULES} <= set(WORKFLOWS)
    assert len({sch.schedule_id for sch in SCHEDULES}) == len(SCHEDULES)
