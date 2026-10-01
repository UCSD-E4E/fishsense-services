"""The Label Studio adapter: throttling, listing, annotators, task shape.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/tests/
test_sync_throttle_handling.py, test_resolve_annotator_id.py and the listing
half of test_sync_cursor_behavior.py. Names, bodies and reasons are v1's; v2
adaptation: the SDK is wrapped once, and its tasks become `LabelStudioTask`s
at the boundary rather than being read loosely throughout the sync.
"""

from __future__ import annotations

# Tests exercise the throttle helpers directly.
# pylint: disable=protected-access

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from label_studio_sdk.core import ApiError
from temporalio.testing import ActivityEnvironment

from fishsense_services_orchestrator.labels import label_studio as sut
from fishsense_services_orchestrator.labels.label_studio import (
    LabelStudioClient,
    LabelStudioTask,
    resolve_annotator_label_studio_id as resolve,
)

# -- throttling (v1's test_sync_throttle_handling.py) --------------------------
#
# `sync_label_studio_project` probed `ls.projects.get(project_id)` and treated
# any `ApiError` as "project is gone" -- log a warning, return. A 429 throttle
# raises the same `ApiError` as a 404, so a rate-limited probe silently skipped
# the project AND returned before the cursor write, repeating every hour with
# no error surfaced. Observed in prod: project 274633 sat at 86/86 tasks
# labeled in Label Studio, 0 completed in the DB, and no cursor row had ever
# been written.


def _throttle(seconds: int = 48) -> ApiError:
    return ApiError(
        status_code=429,
        body={
            "detail": f"Request was throttled. Expected available in {seconds} seconds."
        },
    )


def test_throttle_wait_honours_the_hint():
    assert sut.throttle_wait_seconds(_throttle(48)) == 50.0  # hint + margin


def test_throttle_wait_falls_back_when_hint_is_unparseable():
    err = ApiError(status_code=429, body={"detail": "Request was throttled."})
    assert sut.throttle_wait_seconds(err) == sut.THROTTLE_DEFAULT_WAIT_SECONDS


@pytest.mark.parametrize("code", [404, 403, 500])
def test_non_throttle_errors_are_not_treated_as_throttles(code):
    assert sut.throttle_wait_seconds(ApiError(status_code=code, body={})) is None


async def test_a_404_still_means_missing():
    """Genuinely-gone projects must still be skipped -- legacy ids 57-117
    all 404 on the hosted instance and would otherwise fail every run."""
    sdk = MagicMock()
    sdk.projects.get.side_effect = ApiError(status_code=404, body={})

    exists = await ActivityEnvironment().run(LabelStudioClient(sdk).project_exists, 73)
    assert exists is False


async def test_a_throttle_retries_then_succeeds(monkeypatch):
    """The 274633 case: throttled first, fine on retry -- must NOT be skipped."""
    sleeps: list[float] = []

    async def _no_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(sut.asyncio, "sleep", _no_sleep)
    sdk = MagicMock()
    sdk.projects.get.side_effect = [_throttle(48), MagicMock()]

    exists = await ActivityEnvironment().run(
        LabelStudioClient(sdk).project_exists, 274633
    )
    assert exists is True
    assert sleeps == [50.0], "should have honoured the retry-after hint"


async def test_persistent_throttle_raises_rather_than_skipping(monkeypatch):
    """Failing loudly is the point: a raise leaves the cursor unadvanced so
    the next run retries, instead of silently dropping the project."""

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(sut.asyncio, "sleep", _no_sleep)
    sdk = MagicMock()
    sdk.projects.get.side_effect = _throttle(60)

    with pytest.raises(RuntimeError, match="still throttling"):
        await ActivityEnvironment().run(LabelStudioClient(sdk).project_exists, 274633)
    assert sdk.projects.get.call_count == sut.THROTTLE_MAX_ATTEMPTS


# -- listing ---------------------------------------------------------------------


async def test_heartbeat_pumps_during_slow_listing(monkeypatch):
    """A backlog project's pager iterates synchronously inside a worker
    thread; the adapter must pump heartbeats from the asyncio main thread
    so the 2m heartbeat_timeout doesn't trip mid-listing."""
    monkeypatch.setattr(sut, "_LISTING_HEARTBEAT_INTERVAL_SECONDS", 0.05)

    def _slow_list(*_args, **_kwargs):
        # Block the worker thread long enough for >= 2 heartbeat ticks.
        time.sleep(0.25)
        return [SimpleNamespace(id=1, updated_at=None)]

    sdk = MagicMock()
    sdk.tasks.list.side_effect = _slow_list
    heartbeats: list[tuple] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *args: heartbeats.append(args)

    tasks = await env.run(LabelStudioClient(sdk).list_tasks, 42)

    assert [t.id for t in tasks] == [1]
    assert len(heartbeats) >= 2


def test_a_task_is_read_once_at_the_boundary():
    """v2: the SDK's task objects are loosely typed (its model calls
    `annotations` a string; the payload is a list of dicts). They're read once,
    here, into a plain shape."""
    raw = SimpleNamespace(
        id=7,
        annotators=[{"user_id": 141592}],
        annotations=[{"result": []}],
        is_labeled=True,
        updated_at="2026-05-01T00:00:00Z",
    )

    task = LabelStudioTask.from_sdk(raw)

    assert task == LabelStudioTask(
        id=7,
        annotator_id=141592,
        annotations=[{"result": []}],
        is_labeled=True,
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        payload={},  # only an SDK model has one; see test_label_studio_sdk.py
    )


@pytest.mark.parametrize(
    "value", [None, "", "not a date", 17], ids=["none", "empty", "garbage", "int"]
)
def test_an_unreadable_updated_at_is_none(value):
    """None means "no comparable timestamp", which conservatively processes
    the task regardless of the cursor."""
    raw = SimpleNamespace(id=1, updated_at=value)

    assert LabelStudioTask.from_sdk(raw).updated_at is None


# -- annotators (v1's test_resolve_annotator_id.py) --------------------------------
#
# Self-hosted LS returned `task.annotators` as a list of ints; hosted LS
# (app.heartex.com) returns a list of dicts. v1 handed `annotators[-1]` to a
# user lookup, so on hosted LS a dict went into the URL path and came back 422
# -- which escaped the per-task TaskGroup and failed the entire project's sync.
# v2 stores the Label Studio id itself, so there is no lookup to fail, but the
# id still has to be read correctly from either shape.

_HOSTED = {
    "user_id": 141592,
    "annotated": True,
    "id": 141592,
    "username": "ccrutchf",
    "email": "ccrutchf@ucsd.edu",
}


def test_hosted_ls_dict_yields_the_user_id():
    """The prod shape that caused the 422."""
    assert resolve([_HOSTED]) == 141592


def test_self_hosted_int_list_still_works():
    """Accepted too, so a rollback or mixed instance doesn't break."""
    assert resolve([7, 42]) == 42


def test_takes_the_most_recent_annotator():
    assert resolve([{"user_id": 1}, {"user_id": 2}]) == 2


@pytest.mark.parametrize(
    "annotators",
    [None, [], [{}], [{"username": "no-id"}], ["not-a-number"], [None]],
    ids=["none", "empty", "empty-dict", "no-id-key", "non-numeric", "null-entry"],
)
def test_unusable_shapes_yield_none_rather_than_raising(annotators):
    """None means "skip attribution" -- never a value that lands anywhere."""
    assert resolve(annotators) is None


def test_numeric_strings_are_accepted():
    assert resolve(["141592"]) == 141592
    assert resolve([{"user_id": "141592"}]) == 141592


def test_bool_is_not_treated_as_an_id():
    """bool subclasses int; True must not become user 1."""
    assert resolve([True]) is None
