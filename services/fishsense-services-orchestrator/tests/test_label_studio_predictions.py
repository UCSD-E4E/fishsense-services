"""Label Studio predictions through the real SDK, over a mocked transport.

The laser and head/tail backfills (fishsense-lite@77e8f8e5
backfill_laser_predictions_activity.py and
backfill_headtail_predictions_activity.py) attach a prediction to an
existing task with `predictions.create`, and key idempotency on
`(task, model_version)` from `predictions.list(project=...)`. Both calls
live in the one adapter, so the two slices don't each add them.
"""

from __future__ import annotations

import json

import httpx
from label_studio_sdk.client import LabelStudio
from temporalio.testing import ActivityEnvironment

from fishsense_services_orchestrator.labels.label_studio import (
    LabelStudioClient,
    LabelStudioPrediction,
)


def _client(handle) -> LabelStudioClient:
    return LabelStudioClient(
        LabelStudio(
            base_url="https://label-studio.test",
            api_key="unused",
            httpx_client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
    )


async def test_a_projects_predictions_are_listed_by_task_and_model_version():
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/predictions/"
        assert request.url.params["project"] == "42"
        return httpx.Response(200, json=[
            {"id": 1, "task": 7, "model_version": "laser-detector-v2", "result": []},
            {"id": 2, "task": 8, "model_version": None, "result": []},
        ])  # fmt: skip

    predictions = await ActivityEnvironment().run(_client(handle).predictions, 42)

    assert predictions == [
        LabelStudioPrediction(task_id=7, model_version="laser-detector-v2"),
        LabelStudioPrediction(task_id=8, model_version=None),
    ]


async def test_a_prediction_is_attached_to_an_existing_task():
    sent = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("POST", "/api/predictions/")
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 3, **sent[-1]})

    result = [{"from_name": "kp-1", "type": "keypointlabels", "value": {}}]

    await ActivityEnvironment().run(
        _client(handle).create_prediction, 7, "laser-detector-v2", result
    )

    assert sent == [{"task": 7, "model_version": "laser-detector-v2", "result": result}]


async def test_a_throttled_prediction_backs_off_and_is_created_once():
    responses = iter([
        httpx.Response(429, json={"detail": "Expected available in 0 seconds."}),
        httpx.Response(201, json={"id": 3}),
    ])  # fmt: skip
    posts = []

    def handle(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return next(responses)

    import fishsense_services_orchestrator.labels.label_studio as ls_mod

    async def no_sleep(_seconds):
        return None

    original, ls_mod._throttle_sleep = ls_mod._throttle_sleep, no_sleep
    try:
        await ActivityEnvironment().run(
            _client(handle).create_prediction, 7, "laser-detector-v2", []
        )
    finally:
        ls_mod._throttle_sleep = original

    assert len(posts) == 2
