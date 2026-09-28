"""The object store as a stage: registered through the stage registry, built
from settings validated at startup, and wired end to end.

v1 registered its staging and cleanup activities in the api-worker's one list
(fishsense-lite@77e8f8e5 worker.py) and read the object store's settings on
first use. v2's stage declares itself in `object_store/stage.py`, has no
workflows or schedules of its own -- the parents that stage and clean up are
the other stages' -- and reads ``FISHSENSE_OBJECT_STORE_*`` when it builds, so
a missing setting fails the worker's start, not its first staging.

The wiring test runs a parent through the real worker against the local
Temporal dev server, because the cleanup gate's question is a visibility query:
a stub can't say whether the real server accepts it, or whether a running
reader is actually seen.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from pydantic import ValidationError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_api.raw_staging_store import StagingCapture
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings
from fishsense_services_orchestrator.object_store.cleanup import (
    RawCleanupActivities,
    build_scratch_in_use_query,
    raw_scratch_reader_id,
)
from fishsense_services_orchestrator.object_store.contracts import StagingTarget
from fishsense_services_orchestrator.object_store.layout import ObjectLayout
from fishsense_services_orchestrator.object_store.staging import (
    RawStagingActivities,
    RawStagingSettings,
)
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)
from fishsense_services_orchestrator.registry import Deps, stages

from ._object_store_workflows import RawReaderStandInWorkflow, StageThenCleanUpWorkflow

BUCKET = "fishsense-test"
QUEUE = "object-store-wiring"

OBJECT_STORE = {
    "FISHSENSE_OBJECT_STORE_ENDPOINT_URL": "https://s3.e4e.example",
    "FISHSENSE_OBJECT_STORE_REGION": "garage",
    "FISHSENSE_OBJECT_STORE_ACCESS_KEY_ID": "GKexample",
    "FISHSENSE_OBJECT_STORE_SECRET_ACCESS_KEY": secrets.token_hex(16),
    "FISHSENSE_OBJECT_STORE_BUCKET": "fishsense-lite",
    "FISHSENSE_OBJECT_STORE_LEGACY_LABELS_PREFIX": "fishsense-lite",
}
NAS = {
    "FISHSENSE_NAS_URL": "https://nas.example.test:6021",
    "FISHSENSE_NAS_USERNAME": "svc",
    "FISHSENSE_NAS_PASSWORD": "unused",
    "FISHSENSE_NAS_RAW_ROOT_PATH": "/fishsense_data/REEF/data",
}


def _stage():
    (stage,) = [s for s in stages() if s.name == "object_store"]
    return stage


@pytest.fixture(name="env")
def env_fixture(monkeypatch):
    for name, value in {**OBJECT_STORE, **NAS}.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def test_the_stage_is_discovered_with_activities_only():
    stage = _stage()

    assert list(stage.workflows) == []
    assert list(stage.schedules) == []


def test_the_stage_builds_its_three_activities(env):
    activities = _stage().build_activities(Deps(engine=None, sub="svc"))

    assert sorted(a.__temporal_activity_definition.name for a in activities) == [
        "cleanup_raw_bytes_for_dive",
        "locate_processed_jpeg",
        "stage_raw_bytes_for_dive",
    ]


@pytest.mark.parametrize("missing", sorted(OBJECT_STORE))
def test_a_missing_object_store_setting_fails_the_build(env, missing):
    env.delenv(missing)

    with pytest.raises(ValidationError):
        _stage().build_activities(Deps(engine=None, sub="svc"))


def test_a_missing_nas_setting_fails_the_build(env):
    env.delenv("FISHSENSE_NAS_RAW_ROOT_PATH")

    with pytest.raises(ValidationError):
        _stage().build_activities(Deps(engine=None, sub="svc"))


# -- wiring ----------------------------------------------------------------------


def _md5(n: int) -> str:
    return f"{n:032x}"


class _Catalog:
    def __init__(self, captures):
        self._captures = captures

    async def captures_to_stage(self, tenant_id, dive_id):
        return list(self._captures)

    async def checksums_to_clean(self, tenant_id, dive_id):
        return sorted(c.checksum for c in self._captures)


class _Nas:
    def download_to(self, *, src_path, dest_dir):
        Path(dest_dir, Path(src_path).name).write_bytes(b"ORF:" + src_path.encode())


async def _until_visible(client, query, *, running: bool) -> None:
    """Visibility is eventually consistent; wait until it agrees."""
    for _ in range(100):
        seen = [w async for w in client.list_workflows(query=query)]
        if bool(seen) == running:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"visibility never showed running={running}: {query}")


async def test_a_parent_stages_and_cleanup_waits_for_a_running_reader():
    tenant, dive = uuid.uuid4(), uuid.uuid4()
    target = StagingTarget(tenant_id=tenant, dive_id=dive)
    captures = [StagingCapture(uuid.uuid4(), f"d/P{n}.ORF", _md5(n)) for n in (1, 2)]
    catalog = _Catalog(captures)

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        settings = ObjectStoreConnection(
            endpoint_url="http://garage.example.com", region="garage",
            access_key_id="k", secret_access_key="s", bucket=BUCKET,
            legacy_labels_prefix="fishsense-lite",
        )  # fmt: skip
        store = OrchestratorObjectStore(s3, ObjectLayout(settings))
        staging = RawStagingActivities(
            catalog=catalog,
            store=store,
            nas_settings=NasSettings(**{k.removeprefix("FISHSENSE_NAS_").lower(): v
                                        for k, v in NAS.items()}),
            staging_settings=RawStagingSettings(stage_concurrency=1),
            nas_client_factory=_Nas,
        )  # fmt: skip
        cleanup = RawCleanupActivities(catalog=catalog, store=store)

        def _staged() -> set[str]:
            resp = s3.list_objects_v2(Bucket=BUCKET)
            return {o["Key"] for o in resp.get("Contents", [])}

        async with await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter
        ) as temporal:
            client = temporal.client
            async with Worker(
                client,
                task_queue=QUEUE,
                workflows=[StageThenCleanUpWorkflow, RawReaderStandInWorkflow],
                activities=[
                    staging.stage_raw_bytes_for_dive,
                    cleanup.cleanup_raw_bytes_for_dive,
                ],
            ):
                reader = await client.start_workflow(
                    RawReaderStandInWorkflow.run,
                    id=raw_scratch_reader_id("preprocess-laser", dive),
                    task_queue=QUEUE,
                )
                query = build_scratch_in_use_query(dive)
                await _until_visible(client, query, running=True)

                staged, held = await client.execute_workflow(
                    StageThenCleanUpWorkflow.run,
                    target,
                    id=f"stage-then-clean-{dive}",
                    task_queue=QUEUE,
                )
                kept = _staged()

                await reader.signal(RawReaderStandInWorkflow.finish)
                await reader.result()
                await _until_visible(client, query, running=False)

                again, cleaned = await client.execute_workflow(
                    StageThenCleanUpWorkflow.run,
                    target,
                    id=f"stage-then-clean-again-{dive}",
                    task_queue=QUEUE,
                )

        left = _staged()

    assert staged.staged == 2
    assert held.deleted == 0, "cleanup deleted scratch under a running reader"
    assert kept == {f"tenants/{tenant}/raw/{_md5(n)}.ORF" for n in (1, 2)}
    assert again.skipped_already_present == 2
    assert cleaned.deleted == 2
    assert left == set()
