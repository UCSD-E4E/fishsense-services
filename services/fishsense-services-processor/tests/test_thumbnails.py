"""Each render's thumbnail, for /fish's cards (2026-10-20): the stored render
read through a presigned GET, scaled to the cards' width, PUT through a
presigned PUT, its size and MD5 reported for the orchestrator to check."""

from __future__ import annotations

import hashlib
import io
import uuid
from typing import List

import pytest
from PIL import Image
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.temporal import DATA_CONVERTER
from fishsense_services_contracts.thumbnails import (
    THUMBNAIL_WIDTH,
    ThumbnailFrame,
    ThumbnailInput,
    ThumbnailResult,
    ThumbnailsInput,
)
from fishsense_services_processor import registry
from fishsense_services_processor.thumbnails.activities import ThumbnailActivities
from fishsense_services_processor.thumbnails.workflow import MakeThumbnailsWorkflow


def _jpeg(w=4014, h=3016) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(out, "JPEG")
    return out.getvalue()


RENDER = _jpeg()


class _Get:
    def __init__(self, body=RENDER):
        self.body, self.calls = body, []

    def __call__(self, url):
        self.calls.append(url)
        return self.body


class _Put:
    def __init__(self, status=200):
        self.status, self.calls = status, []

    def __call__(self, url, headers, data) -> int:
        self.calls.append((url, headers, data))
        return self.status


def _frame():
    return ThumbnailFrame(render_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                          get_url="https://g/render?sig", key="thumbnails/w640/x.JPG",
                          put_url="https://g/thumb?sig")  # fmt: skip


async def _run(acts, frame=None, width=THUMBNAIL_WIDTH):
    payload = ThumbnailInput(frame=frame or _frame(), width=width)
    return await ActivityEnvironment().run(acts.make_thumbnail, payload)


async def test_a_render_is_scaled_to_the_cards_width_and_put():
    get, put = _Get(), _Put()

    r = await _run(ThumbnailActivities(get=get, put=put))

    ((url, headers, data),) = put.calls
    assert get.calls == ["https://g/render?sig"] and url == "https://g/thumb?sig"
    assert headers == {"Content-Type": "image/jpeg"}
    with Image.open(io.BytesIO(data)) as thumb:
        assert thumb.size == (THUMBNAIL_WIDTH, round(3016 * THUMBNAIL_WIDTH / 4014))
    assert (r.status, r.bytes, r.md5) == ("made", len(data),
                                          hashlib.md5(data).hexdigest())  # fmt: skip
    assert (r.width, r.height) == (640, 481)
    assert len(data) < len(RENDER) / 10


async def test_a_render_not_there_is_an_abstention():
    put = _Put()
    r = await _run(ThumbnailActivities(get=_Get(body=None), put=put))
    assert r.status == "render_unavailable" and put.calls == []


async def test_a_refused_put_fails_the_try():
    with pytest.raises(RuntimeError, match="thumbnail"):
        await _run(ThumbnailActivities(get=_Get(), put=_Put(status=403)))


async def test_the_workflow_thumbnails_each_render():
    calls: List[str] = []

    class Acts(ThumbnailActivities):
        pass

    acts = Acts(get=_Get(), put=_Put())
    frames = [_frame(), _frame()]
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue="t", workflows=[MakeThumbnailsWorkflow],
                          activities=[acts.make_thumbnail]):  # fmt: skip
            results = await env.client.execute_workflow(
                MakeThumbnailsWorkflow.run,
                ThumbnailsInput(tenant_id=uuid.uuid4(), frames=frames),
                id="thumbs", task_queue="t", result_type=List[ThumbnailResult],
            )  # fmt: skip
    assert [r.render_id for r in results] == [f.render_id for f in frames]
    assert calls == []


def test_the_thumbnail_stage_runs_on_the_per_image_role():
    (stage,) = [s for s in registry.stages() if s.name == "thumbnails"]
    assert stage.role == registry.ROLE_PER_IMAGE
