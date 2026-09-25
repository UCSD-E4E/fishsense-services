"""The adapter against the real Label Studio SDK, over a mocked HTTP transport.

The other label tests fake the adapter's inputs. These don't: the SDK's own
client, pager and models read Label Studio's JSON, and the adapter reads what
they produce. That is where v1 was bitten -- hosted Label Studio's annotators
are dicts where the SDK's model says ints (the 422), and its models call
`annotations` a string -- and it is the only way to see the pager's real stop
condition (Label Studio answers the page after the last with 404).
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from label_studio_sdk.client import LabelStudio
from temporalio.testing import ActivityEnvironment

from fishsense_services_orchestrator.labels.label_studio import LabelStudioClient
from fishsense_services_orchestrator.labels.sync import laser_sync_from_task

# A task as hosted Label Studio returns it (app.heartex.com), trimmed.
TASK = {
    "id": 7,
    "data": {"img": "s3://fishsense/lab/P8290017.JPG"},
    "annotations": [
        {
            "id": 99,
            "result": [
                {
                    "from_name": "kp-1",
                    "to_name": "img",
                    "type": "keypointlabels",
                    "original_width": 4000,
                    "original_height": 3000,
                    "value": {"x": 25.0, "y": 50.0, "keypointlabels": ["laser"]},
                }
            ],
        }
    ],
    "annotators": [{"user_id": 141592, "id": 141592, "username": "ccrutchf"}],
    "is_labeled": True,
    "updated_at": "2026-05-01T12:30:00.000000Z",
}


def _label_studio(pages: list[list[dict]], *, project_status=200) -> LabelStudio:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/projects/42/":
            return httpx.Response(project_status, json={"id": 42})
        if request.url.path == "/api/tasks/":
            assert request.url.params["project"] == "42"
            page = int(request.url.params.get("page", "1"))
            if page > len(pages):
                return httpx.Response(404, json={"detail": "Invalid page."})
            return httpx.Response(200, json={"tasks": pages[page - 1],
                                             "total": sum(map(len, pages))})  # fmt: skip
        return httpx.Response(500, json={"detail": f"unexpected {request.url}"})

    return LabelStudio(
        base_url="https://label-studio.test",
        api_key="unused",
        httpx_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )


async def test_a_hosted_task_is_read_through_the_real_sdk():
    client = LabelStudioClient(_label_studio([[TASK]]))

    assert await ActivityEnvironment().run(client.project_exists, 42) is True
    (task,) = await ActivityEnvironment().run(client.list_tasks, 42)

    sync = laser_sync_from_task(task)
    assert task.id == 7
    assert (sync.completed, sync.x, sync.y, sync.label) == (True, 1000.0, 1500.0,
                                                             "laser")  # fmt: skip
    assert sync.ls_labeler_id == 141592
    assert sync.ls_updated_at == datetime(2026, 5, 1, 12, 30, tzinfo=UTC)
    assert sync.ls_payload["annotations"][0]["id"] == 99


async def test_the_listing_pages_until_label_studio_says_there_are_no_more():
    second = {**TASK, "id": 8}
    client = LabelStudioClient(_label_studio([[TASK], [second]]))

    tasks = await ActivityEnvironment().run(client.list_tasks, 42)

    assert [t.id for t in tasks] == [7, 8]


async def test_a_missing_project_is_reported_missing():
    client = LabelStudioClient(_label_studio([], project_status=404))

    assert await ActivityEnvironment().run(client.project_exists, 42) is False
