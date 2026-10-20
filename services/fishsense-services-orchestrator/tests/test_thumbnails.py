"""Each render's thumbnail, for /fish's cards (2026-10-20; review of #151).

Pinned:
* owed per tenant with a bucket, in it: each frame gets a presigned GET of its
  render and a PUT for its thumbnail, at `thumbnail_key`;
* a thumbnail is recorded only if the stored object's size and MD5 match the
  processor's report; a render not there is recorded as such; a result for a
  render not asked for is refused;
* the backlog wakes the processor only when something is owed, pages through
  each tenant, and stops a tenant whose page recorded nothing (no loop on a
  page that keeps failing);
* off by default: no schedule unless enabled.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import List, Optional

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from fishsense_services_api.tenant_stores import (
    NoTenantStore,
    PresignedUpload,
    StoredObject,
)
from fishsense_services_api.thumbnails_store import OwedThumbnail
from fishsense_services_contracts import PROCESSOR_TASK_QUEUE
from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_contracts.thumbnails import (
    THUMBNAIL_WIDTH,
    ThumbnailFrame,
    ThumbnailResult,
    ThumbnailsInput,
    thumbnail_key,
)
from fishsense_services_orchestrator.renders.settings import RenderSettings
from fishsense_services_orchestrator.thumbnails.activities import (
    ThumbnailActivities,
    ThumbnailPage,
    ThumbnailPersistReport,
)
from fishsense_services_orchestrator.thumbnails.stage import thumbnail_schedules
from fishsense_services_orchestrator.thumbnails.workflow import (
    ThumbnailBacklogReport,
    ThumbnailBacklogWorkflow,
)

LAB, PARTNER = uuid.uuid4(), uuid.uuid4()
MD5 = "0123456789abcdef0123456789abcdef"
BUCKET = "fishsense-tenant-lab"


class _Objects:
    bucket = BUCKET

    def __init__(self, stored=None):
        self.stored = stored or {}

    def presign_get(self, key, *, expires_seconds=3600):
        return f"https://garage/{self.bucket}/{key}?get&expires={expires_seconds}"

    def presign_render_put(self, key):
        return PresignedUpload(url=f"https://garage/{self.bucket}/{key}?put",
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


def _owed(n):
    return [OwedThumbnail(uuid.uuid4(), uuid.uuid4(), f"renders/r1_ab/{i:032x}.JPG")
            for i in range(n)]  # fmt: skip


class _Catalog:
    def __init__(self, owed=2):
        self.owed = _owed(owed)
        self.slugs = {LAB: "lab", PARTNER: "partner"}
        self.asked = []
        self.persisted = []

    async def member_tenants(self):
        return list(self.slugs)

    async def tenant_slug(self, tenant_id):
        return self.slugs[tenant_id]

    async def owed_thumbnails(self, tenant_id, *, bucket, width, limit=None):
        self.asked.append((bucket, width, limit))
        return self.owed[:limit]

    async def persist_thumbnails(self, tenant_id, rows):
        self.persisted.extend(rows)
        return len(rows)


def _acts(catalog=None, objects=None):
    return ThumbnailActivities(catalog=catalog or _Catalog(),
                               stores=_Stores(lab=objects or _Objects()))  # fmt: skip


async def _run(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


async def test_only_tenants_with_a_bucket_are_thumbnailed(caplog):
    with caplog.at_level("WARNING"):
        got = await _run(_acts().select_thumbnail_tenants)
    assert got == [LAB]
    assert "partner has no bucket" in caplog.text


async def test_owed_renders_come_with_their_get_and_their_thumbnails_put():
    catalog = _Catalog(owed=3)

    page = await _run(_acts(catalog).resolve_thumbnails, LAB, 2)

    assert catalog.asked == [(BUCKET, THUMBNAIL_WIDTH, 2)]
    assert page.payload.width == THUMBNAIL_WIDTH and len(page.payload.frames) == 2
    for frame, owed in zip(page.payload.frames, catalog.owed):
        key = thumbnail_key(owed.render_key)
        assert (frame.render_id, frame.capture_id) == (owed.render_id, owed.capture_id)
        assert frame.get_url.startswith(
            f"https://garage/{BUCKET}/{owed.render_key}?get"
        )
        # A week, as the render's: the payload can wait in a queue.
        assert "expires=604800" in frame.get_url
        assert (frame.key, frame.put_url) == (key, f"https://garage/{BUCKET}/{key}?put")


async def test_a_tenant_without_a_bucket_has_no_page():
    page = await _run(_acts().resolve_thumbnails, PARTNER, 10)
    assert page.payload is None


def _payload(catalog):
    return ThumbnailsInput(tenant_id=LAB, frames=[
        ThumbnailFrame(render_id=o.render_id, capture_id=o.capture_id, get_url="g",
                       key=thumbnail_key(o.render_key), put_url="p")
        for o in catalog.owed
    ])  # fmt: skip


def _made(render_id, **extra):
    fields = dict(render_id=render_id, status="made", bytes=40_000, md5=MD5,
                  width=640, height=481)  # fmt: skip
    fields.update(extra)
    return ThumbnailResult(**fields)


async def test_only_a_thumbnail_the_stored_object_confirms_is_recorded():
    catalog = _Catalog(owed=3)
    payload = _payload(catalog)
    good, wrong, gone = payload.frames
    objects = _Objects({
        good.key: StoredObject(size=40_000, sha256=None, md5=MD5),
        wrong.key: StoredObject(size=40_000, sha256=None, md5="f" * 32),
    })  # fmt: skip
    results = [_made(good.render_id), _made(wrong.render_id),
               ThumbnailResult(render_id=gone.render_id, status="render_unavailable")]  # fmt: skip

    report = await _run(_acts(catalog, objects).persist_thumbnails, LAB, payload,
                        results)  # fmt: skip

    assert (report.made, report.missing, report.unverified) == (1, 1, [wrong.render_id])
    made, missing = catalog.persisted
    assert (made.render_id, made.capture_id, made.bucket, made.key, made.target_width,
            made.width, made.height) == (good.render_id, good.capture_id, BUCKET,
                                         good.key, THUMBNAIL_WIDTH, 640, 481)  # fmt: skip
    assert (missing.status, missing.key) == ("render_unavailable", None)


async def test_a_result_for_a_render_not_asked_for_is_refused():
    catalog = _Catalog(owed=1)
    with pytest.raises(ApplicationError) as error:
        await _run(_acts(catalog).persist_thumbnails, LAB, _payload(catalog),
                   [_made(uuid.uuid4())])  # fmt: skip
    assert error.value.non_retryable and error.value.type == "InvalidThumbnails"
    assert catalog.persisted == []


# -- the backlog ------------------------------------------------------------------------

EVENTS: list = []
QUEUE = "test-thumbnails"


def _stubs(*, pages, record=True):
    """`pages[tenant]` is the pages resolve hands out, in order."""

    @activity.defn(name="select_thumbnail_tenants")
    async def select() -> List[uuid.UUID]:
        return list(pages)

    @activity.defn(name="resolve_thumbnails")
    async def resolve(tenant_id: uuid.UUID, limit: int) -> ThumbnailPage:
        EVENTS.append(("resolve", tenant_id))
        queue = pages[tenant_id]
        return queue.pop(0) if queue else ThumbnailPage(payload=None)

    @activity.defn(name="ensure_per_image_processor_running")
    async def wake() -> None:
        EVENTS.append(("wake",))

    @activity.defn(name="persist_thumbnails")
    async def persist(tenant_id: uuid.UUID, p: ThumbnailsInput,
                      r: List[ThumbnailResult]) -> ThumbnailPersistReport:  # fmt: skip
        EVENTS.append(("persist", len(r)))
        return ThumbnailPersistReport(made=len(r) if record else 0)

    return [select, resolve, wake, persist]


@workflow.defn(name="MakeThumbnailsWorkflow")
class _ProcessorThumbnails:
    @workflow.run
    async def run(self, payload: ThumbnailsInput) -> List[ThumbnailResult]:
        EVENTS.append(("thumbnails", len(payload.frames)))
        return [_made(f.render_id) for f in payload.frames]


def _page(n):
    catalog = _Catalog(owed=n)
    return ThumbnailPage(payload=_payload(catalog))


async def _execute(pages, record=True):
    EVENTS.clear()
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue=QUEUE,
                          activities=_stubs(pages=pages, record=record),
                          workflows=[ThumbnailBacklogWorkflow]), \
                Worker(env.client, task_queue=PROCESSOR_TASK_QUEUE,
                       workflows=[_ProcessorThumbnails],
                       workflow_runner=UnsandboxedWorkflowRunner()):  # fmt: skip
            return await env.client.execute_workflow(
                ThumbnailBacklogWorkflow.run, None, id=f"t-{uuid.uuid4()}",
                task_queue=QUEUE, result_type=ThumbnailBacklogReport,
            )  # fmt: skip


async def test_the_backlog_pages_through_each_tenant_waking_the_processor_once():
    other = uuid.uuid4()
    report = await _execute({LAB: [_page(2), _page(1)], other: [_page(3)]})

    kinds = [e[0] for e in EVENTS]
    assert kinds.count("wake") == 1 and kinds.count("thumbnails") == 3
    assert report.made == 6


async def test_nothing_owed_wakes_nothing():
    report = await _execute({LAB: []})
    assert [e[0] for e in EVENTS] == ["resolve"] and report.made == 0


async def test_a_page_that_records_nothing_stops_its_tenant():
    """Every thumbnail of the page failing its tries (left out, still owed)
    would hand the same page out forever."""
    report = await _execute({LAB: [_page(2), _page(2), _page(2)]}, record=False)
    assert [e[0] for e in EVENTS].count("thumbnails") == 1 and report.made == 0


def test_off_by_default():
    assert thumbnail_schedules(RenderSettings()) == []
    (s,) = thumbnail_schedules(RenderSettings(thumbnails_enabled=True))
    assert s.schedule_id == "thumbnail-backlog"
