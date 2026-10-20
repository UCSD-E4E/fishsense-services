"""The render stage (new in v2, 2026-10-19): every capture's raw rectified to a
JPEG, in its tenant's own bucket; the orchestrator presigns, the processor
renders and PUTs, and only what the stored object confirms is recorded.

Pinned:
* a tenant with no bucket gets no renders, and the log says so (no shared
  fallback);
* each owed capture gets its staged raw, its render key and a presigned PUT;
* a render is recorded only if the stored object's size and MD5 match the
  processor's report; an abstention is recorded; a result for a capture not
  asked for, or for another render, is refused;
* the mission workflow stages, renders as a raw-scratch reader, records and
  cleans up; with no bucket it stages nothing;
* the backlog runs every mission it selects;
* off by default: no schedule unless enabled.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import List, Optional

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from fishsense_services_api.renders_store import (
    RenderCapture,
    RenderInputs,
    RenderMission,
)
from fishsense_services_api.tenant_stores import (
    NoTenantStore,
    PresignedUpload,
    StoredObject,
)
from fishsense_services_contracts import PROCESSOR_TASK_QUEUE
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.renders import (
    RENDER_VERSION,
    RenderCapturesInput,
    RenderFrame,
    RenderResult,
    render_key,
)
from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_orchestrator.object_store.contracts import (
    CleanupRawBytesResult,
    StageRawBytesResult,
    StagingTarget,
)
from fishsense_services_orchestrator.renders.activities import (
    RenderActivities,
    RenderPersistReport,
    RenderTarget,
)
from fishsense_services_orchestrator.renders.settings import RenderSettings
from fishsense_services_orchestrator.renders.stage import render_schedules
from fishsense_services_orchestrator.renders.workflow import (
    RenderBacklogInput,
    RenderBacklogReport,
    RenderBacklogWorkflow,
    RenderMissionReport,
    RenderMissionWorkflow,
)

LAB, PARTNER = uuid.uuid4(), uuid.uuid4()
MISSION = uuid.uuid4()
CAMERA = uuid.uuid4()
K = [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]
MD5 = "0123456789abcdef0123456789abcdef"


class _Objects:
    bucket = "fishsense-tenant-lab"

    def __init__(self, stored=None):
        self.stored = stored or {}

    def presign_render_put(self, key):
        return PresignedUpload(url=f"https://garage/{self.bucket}/{key}?sig",
                               headers={"Content-Type": "image/jpeg"})  # fmt: skip

    def head(self, key):
        return self.stored.get(key)


class _Stores:
    def __init__(self, **by_slug):
        self.by_slug = by_slug

    def for_tenant(self, slug):
        if slug not in self.by_slug:
            raise NoTenantStore(slug)
        return self.by_slug[slug]


class _Catalog:
    def __init__(self, captures=2):
        self.persisted = []
        self.captures = [
            RenderCapture(uuid.uuid4(), f"{i:032x}") for i in range(captures)
        ]

    async def member_tenants(self):
        return [LAB, PARTNER]

    async def tenant_slug(self, tenant_id):
        return "lab" if tenant_id == LAB else "partner"

    async def render_missions(self, tenant_id, *, render_version, limit=None):
        assert render_version == RENDER_VERSION
        return [RenderMission(MISSION, len(self.captures), True)]

    async def render_inputs(self, tenant_id, mission_id, *, render_version):
        return RenderInputs(CAMERA, K, [0.0] * 5, self.captures)

    async def persist_renders(self, tenant_id, mission_id, rows):
        self.persisted.extend(rows)
        return len(rows)


class _Layout:
    def raw(self, tenant_id, checksum):
        return ObjectRef(
            bucket="scratch", key=f"tenants/{tenant_id}/raw/{checksum}.ORF"
        )


def _acts(catalog=None, objects=None):
    return RenderActivities(
        catalog=catalog or _Catalog(),
        store=SimpleNamespace(layout=_Layout()),
        stores=_Stores(lab=objects or _Objects()),
    )


async def _run(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


# -- activities --------------------------------------------------------------------------


async def test_a_tenant_with_no_bucket_gets_no_renders(caplog):
    with caplog.at_level("WARNING"):
        got = await _run(_acts().select_render_missions)
    assert got == [RenderTarget(LAB, MISSION)]
    assert "partner has no bucket" in caplog.text


async def test_each_owed_capture_gets_its_raw_its_key_and_a_presigned_put():
    catalog = _Catalog()
    got = await _run(_acts(catalog).resolve_render_inputs, RenderTarget(LAB, MISSION))

    assert (got.render_version, got.camera_calibration_id) == (RENDER_VERSION, CAMERA)
    for frame, c in zip(got.frames, catalog.captures):
        key = render_key(RENDER_VERSION, CAMERA, c.checksum)
        assert frame.key == key
        assert frame.raw.key == f"tenants/{LAB}/raw/{c.checksum}.ORF"
        assert frame.put_url == f"https://garage/fishsense-tenant-lab/{key}?sig"


async def test_with_no_bucket_there_is_nothing_to_render():
    got = await _run(_acts().resolve_render_inputs, RenderTarget(PARTNER, MISSION))
    assert got is None


def _payload(catalog):
    frames = [
        RenderFrame(capture_id=c.capture_id, raw=ObjectRef(bucket="s", key="r"),
                    key=render_key(RENDER_VERSION, CAMERA, c.checksum), put_url="u")
        for c in catalog.captures
    ]  # fmt: skip
    return RenderCapturesInput(
        tenant_id=LAB, mission_id=MISSION, render_version=RENDER_VERSION,
        camera_calibration_id=CAMERA, camera_matrix=K,
        distortion_coefficients=[0.0] * 5, frames=frames,
    )  # fmt: skip


def _rendered(capture_id, **extra):
    fields = dict(capture_id=capture_id, status="rendered",
                  render_version=RENDER_VERSION, camera_calibration_id=CAMERA,
                  bytes=100, md5=MD5, width=4, height=3)  # fmt: skip
    fields.update(extra)
    return RenderResult(**fields)


async def test_only_a_render_the_stored_object_confirms_is_recorded():
    catalog = _Catalog(captures=4)
    payload = _payload(catalog)
    good, wrong_md5, missing, abstained = payload.frames
    objects = _Objects({
        good.key: StoredObject(size=100, sha256=None, md5=MD5),
        wrong_md5.key: StoredObject(size=100, sha256=None, md5="f" * 32),
    })  # fmt: skip
    results = [
        _rendered(good.capture_id), _rendered(wrong_md5.capture_id),
        _rendered(missing.capture_id),
        RenderResult(capture_id=abstained.capture_id, status="decode_failed",
                     render_version=RENDER_VERSION, camera_calibration_id=CAMERA),
    ]  # fmt: skip

    report = await _run(_acts(catalog, objects).persist_renders,
                        RenderTarget(LAB, MISSION), payload, results)  # fmt: skip

    assert (report.rendered, report.abstained) == (1, 1)
    assert set(report.unverified) == {wrong_md5.capture_id, missing.capture_id}
    rendered, decode_failed = catalog.persisted
    assert (rendered.capture_id, rendered.bucket, rendered.key, rendered.md5) == (
        good.capture_id,
        "fishsense-tenant-lab",
        good.key,
        MD5,
    )
    assert (decode_failed.status, decode_failed.key) == ("decode_failed", None)


@pytest.mark.parametrize(
    "bad",
    [
        lambda c: _rendered(uuid.uuid4()),
        lambda c: _rendered(c, render_version=RENDER_VERSION + 1),
        lambda c: _rendered(c, camera_calibration_id=uuid.uuid4()),
    ],
    ids=["not-asked-for", "another-version", "another-camera"],
)
async def test_a_result_that_is_not_the_render_asked_for_is_refused(bad):
    catalog = _Catalog(captures=1)
    payload = _payload(catalog)
    with pytest.raises(ApplicationError) as error:
        await _run(_acts(catalog).persist_renders, RenderTarget(LAB, MISSION),
                   payload, [bad(payload.frames[0].capture_id)])  # fmt: skip
    assert error.value.non_retryable and error.value.type == "InvalidRenders"
    assert catalog.persisted == []


# -- workflows ---------------------------------------------------------------------------

EVENTS: list = []
QUEUE = "test-renders"


def _stubs(*, payload, results=None):
    @activity.defn(name="resolve_render_inputs")
    async def resolve(target: RenderTarget) -> Optional[RenderCapturesInput]:
        EVENTS.append(("resolve", target.mission_id))
        return payload

    @activity.defn(name="ensure_per_image_processor_running")
    async def wake() -> None:
        EVENTS.append(("wake",))

    @activity.defn(name="stage_raw_bytes_for_dive")
    async def stage(target: StagingTarget) -> StageRawBytesResult:
        EVENTS.append(("stage", target.mission_id))
        return StageRawBytesResult(staged=1, skipped_already_present=0, no_path=0)

    @activity.defn(name="cleanup_raw_bytes_for_dive")
    async def cleanup(target: StagingTarget) -> CleanupRawBytesResult:
        EVENTS.append(("cleanup", target.mission_id))
        return CleanupRawBytesResult(deleted=1)

    @activity.defn(name="persist_renders")
    async def persist(target: RenderTarget, p: RenderCapturesInput,
                      r: List[RenderResult]) -> RenderPersistReport:  # fmt: skip
        EVENTS.append(("persist", len(r)))
        return RenderPersistReport(written=len(r), rendered=len(r))

    @activity.defn(name="select_render_missions")
    async def select(limit: Optional[int]) -> List[RenderTarget]:
        return [RenderTarget(LAB, uuid.uuid4()) for _ in range(3)]

    return [resolve, wake, stage, cleanup, persist, select]


@workflow.defn(name="RenderCapturesWorkflow")
class _ProcessorRender:
    @workflow.run
    async def run(self, payload: RenderCapturesInput) -> List[RenderResult]:
        EVENTS.append(("render", workflow.info().workflow_id, len(payload.frames)))
        return [_rendered(f.capture_id) for f in payload.frames[:-1]]  # one left out


async def _execute(wf, arg, *, payload, result_type):
    EVENTS.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue=QUEUE, activities=_stubs(payload=payload),
                          workflows=[RenderMissionWorkflow, RenderBacklogWorkflow]), \
                Worker(env.client, task_queue=PROCESSOR_TASK_QUEUE,
                       workflows=[_ProcessorRender],
                       workflow_runner=UnsandboxedWorkflowRunner()):  # fmt: skip
            return await env.client.execute_workflow(
                wf, arg, id=f"t-{uuid.uuid4()}", task_queue=QUEUE,
                result_type=result_type,
            )  # fmt: skip


async def test_a_mission_is_staged_rendered_recorded_and_cleaned_up():
    payload = _payload(_Catalog(captures=3))
    report = await _execute(RenderMissionWorkflow.run, RenderTarget(LAB, MISSION),
                            payload=payload, result_type=RenderMissionReport)  # fmt: skip

    assert [e[0] for e in EVENTS] == [
        "resolve",
        "wake",
        "stage",
        "render",
        "persist",
        "cleanup",
    ]
    assert ("render", f"render-captures-{MISSION}", 3) in EVENTS
    assert (report.owed, report.rendered, report.left_out) == (3, 2, 1)


async def test_a_tenant_with_no_bucket_stages_nothing():
    report = await _execute(RenderMissionWorkflow.run, RenderTarget(PARTNER, MISSION),
                            payload=None, result_type=RenderMissionReport)  # fmt: skip
    assert report.no_bucket and [e[0] for e in EVENTS] == ["resolve"]


async def test_the_backlog_renders_every_mission_it_selects():
    payload = _payload(_Catalog(captures=1))
    report = await _execute(RenderBacklogWorkflow.run, RenderBacklogInput(concurrency=2),
                            payload=payload, result_type=RenderBacklogReport)  # fmt: skip
    assert len(report.missions) == 3
    assert sum(e[0] == "render" for e in EVENTS) == 3


def test_off_by_default():
    assert render_schedules(RenderSettings(renders_enabled=False)) == []
    (schedule,) = render_schedules(RenderSettings(renders_enabled=True))
    assert schedule.workflow is RenderBacklogWorkflow
    assert schedule.every == timedelta(hours=1)
