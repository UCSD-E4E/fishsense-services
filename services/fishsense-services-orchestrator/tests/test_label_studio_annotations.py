"""Label Studio annotations through the real SDK, over a mocked transport.

The laser auto-accept apply (fishsense-lite@77e8f8e5
apply_laser_auto_accept_activity.py) lists a project's tasks once to learn
which nobody has started -- no annotation and no draft -- and annotates those
with `annotations.create`. Both calls live in the one adapter
(`labels.label_studio`), so every call backs off through a 429.
"""

from __future__ import annotations

import json

import httpx
from label_studio_sdk.client import LabelStudio
from temporalio.testing import ActivityEnvironment

from fishsense_services_orchestrator.labels.label_studio import LabelStudioClient


def _client(handle) -> LabelStudioClient:
    return LabelStudioClient(
        LabelStudio(
            base_url="https://label-studio.test",
            api_key="unused",
            httpx_client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
    )


def _tasks(tasks):
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tasks/"
        assert request.url.params["project"] == "42"
        if int(request.url.params.get("page", "1")) > 1:
            return httpx.Response(404, json={"detail": "Invalid page."})
        return httpx.Response(200, json={"tasks": tasks, "total": len(tasks)})

    return handle


async def test_only_tasks_nobody_has_started_are_untouched():
    """No annotation AND no draft: an annotation means done (never overwrite a
    human), a draft means someone is mid-click (never discard their work)."""
    client = _client(_tasks([
        {"id": 1, "annotations": [], "drafts": []},
        {"id": 2, "annotations": [{"id": 9, "result": []}], "drafts": []},
        {"id": 3, "annotations": [], "drafts": [{"id": 4, "result": []}]},
        {"id": 4, "data": {}},
    ]))  # fmt: skip

    untouched = await ActivityEnvironment().run(client.untouched_task_ids, 42)

    assert untouched == {1, 4}


async def test_an_annotation_is_created_on_the_task():
    sent = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == (
            "POST",
            "/api/tasks/7/annotations/",
        )
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 11, **sent[-1]})

    result = [{"from_name": "laser", "type": "keypointlabels", "value": {}}]

    await ActivityEnvironment().run(
        _client(handle).create_annotation, 7, 42, result, False
    )

    assert sent == [{"project": 42, "result": result, "ground_truth": False}]
