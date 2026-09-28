"""The import path must never create a second copy of a task.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/tests/
test_populate_import_retry_dedup.py. Names, bodies and reasons are v1's; the
v2 adaptation is only that the fake is the SDK under `LabelStudioClient`, and
the helper is handed the client rather than fetching a global one.

Ten Label Studio projects were found on 2026-09-09 holding two tasks per
image, and one held 23 copies of three images. `import_tasks_and_record_labels`
made them itself: hosted LS imports asynchronously, the helper polled ~24s for
the new tasks to become listable, and **raised** when they had not appeared.
The populate activities declared no `RetryPolicy`, so Temporal retried inside
the 30-minute `schedule_to_close`, the retry's dedup listing still could not
see the in-flight import, and it imported the whole set again. Both copies then
materialised.

The consequences went past wasted labeller queue: a duplicate splits a row from
its own annotation (seven dive-437 head/tail labels were invisible to the
pipeline because the row tracked the empty twin), and it hands a labeller a
second, *ungated* copy of a frame the laser auto-accept gate had already
judged -- all 66 of dive 517's calibration frames were re-done by hand that way.

So the contract these tests pin is: **an import that has not become visible is
not an error.** Write rows for what is visible, leave the rest to the next
scheduled run, and never let a retry re-issue the import.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import List
from unittest.mock import MagicMock

import pytest
from label_studio_sdk.core import ApiError
from temporalio.testing import ActivityEnvironment

from fishsense_services_orchestrator.labels import label_studio as ls_mod
from fishsense_services_orchestrator.labels import populate as sut
from fishsense_services_orchestrator.labels.label_studio import LabelStudioClient


def _resolve_url(task_id: int, s3_uri: str) -> str:
    """The presign wrapper hosted LS serves back instead of the s3:// URI."""
    fileuri = base64.b64encode(s3_uri.encode()).decode()
    return f"/tasks/{task_id}/resolve/?fileuri={fileuri}"


class SlowImportLabelStudio:
    """Hosted LS whose imported tasks only become listable after a delay.

    `visible_after_lists` is how many `tasks.list` calls must go by before the
    tasks an import created show up. That is the real behaviour the helper has
    to survive.
    """

    def __init__(self, task_ids: List[int], *, visible_after_lists: int = 0):
        self._ids = iter(task_ids)
        # (listable_from, task). A task is served once `_lists` reaches its
        # threshold, which is how the materialisation lag is expressed.
        self._entries: List[tuple[int, SimpleNamespace]] = []
        self._lists = 0
        self._lag = visible_after_lists
        self.list_raises: List[Exception] = []
        self.projects = MagicMock(import_tasks=MagicMock(side_effect=self._import))
        self.tasks = MagicMock(list=MagicMock(side_effect=self._list))

    @property
    def import_calls(self) -> List[List[dict]]:
        """The tasks each `import_tasks` call sent, in call order."""
        return [
            list(call.kwargs["request"])
            for call in self.projects.import_tasks.call_args_list
        ]

    def _import(self, project_id, request, return_task_ids=False):
        # pylint: disable=unused-argument
        for task in request:
            task_id = next(self._ids)
            s3_uri = task["data"].get("image") or task["data"].get("img")
            self._entries.append(
                (
                    self._lists + self._lag,
                    SimpleNamespace(
                        id=task_id, data={"image": _resolve_url(task_id, s3_uri)}
                    ),
                )
            )
        return SimpleNamespace(import_=1)

    def _list(self, project=None):  # pylint: disable=unused-argument
        if self.list_raises:
            raise self.list_raises.pop(0)
        visible = [t for due_at, t in self._entries if due_at <= self._lists]
        self._lists += 1
        return visible

    def reveal_all(self):
        """Let every still-pending task become listable on the next list."""
        self._entries = [(0, task) for _, task in self._entries]

    def seed(self, *tasks):
        """Put tasks in the project as though an earlier run imported them."""
        self._entries.extend((0, task) for task in tasks)


def _task(checksum: str) -> dict:
    return {"data": {"image": f"s3://bucket/preprocess_jpeg/{checksum}.JPG"}}


@pytest.fixture(name="fast_poll")
def _fast_poll(monkeypatch):
    """Keep the visibility budget short and sleep-free."""
    monkeypatch.setattr(sut, "_IMPORT_VISIBILITY_ATTEMPTS", 2)
    monkeypatch.setattr(sut, "_IMPORT_VISIBILITY_INTERVAL_S", 0)


def _call(ls, tasks, items, recorded, project_id=901):
    async def record_label(item, task_id):
        recorded.append((item, task_id))

    async def call():
        return await sut.import_tasks_and_record_labels(
            LabelStudioClient(ls),
            project_id=project_id,
            tasks=tasks,
            record_label=record_label,
            items=items,
        )

    return call


async def _run(ls, tasks, items, recorded, project_id=901):
    return await ActivityEnvironment().run(
        _call(ls, tasks, items, recorded, project_id)
    )


@pytest.mark.usefixtures("fast_poll")
async def test_invisible_import_does_not_raise_and_is_not_reimported():
    """The whole bug, end to end.

    An import that has not materialised inside the budget must not raise --
    raising is what made Temporal retry, and the retry is what duplicated. The
    next run must then reconcile the tasks rather than import them again.
    """
    ls = SlowImportLabelStudio([11, 12], visible_after_lists=99)
    tasks = [_task("aaa"), _task("bbb")]

    recorded: List[tuple] = []
    result = await _run(ls, tasks, ["a", "b"], recorded)

    assert len(ls.import_calls) == 1, "the batch is imported exactly once"
    assert result == (0, 2), "nothing visible yet: no rows, both deferred"
    assert not result.complete and not recorded

    # The next scheduled populate run, by which time LS has caught up.
    ls.reveal_all()
    recorded.clear()
    result = await _run(ls, tasks, ["a", "b"], recorded)

    assert len(ls.import_calls) == 1, "the second run must NOT re-import"
    assert result == (2, 0) and result.complete
    assert sorted(recorded) == [("a", 11), ("b", 12)]


@pytest.mark.usefixtures("fast_poll")
async def test_partial_visibility_records_what_landed():
    """A task that did land gets its row now, not an hour later."""
    ls = SlowImportLabelStudio([21, 22], visible_after_lists=0)
    ls.seed(
        SimpleNamespace(
            id=99, data={"image": _resolve_url(99, "s3://bucket/p/aaa.JPG")}
        )
    )

    recorded: List[tuple] = []
    result = await _run(
        ls,
        [{"data": {"image": "s3://bucket/p/aaa.JPG"}}, _task("bbb")],
        ["a", "b"],
        recorded,
    )

    assert ls.import_calls == [[_task("bbb")]], "only the missing task imported"
    assert result == (2, 0) and result.complete
    assert sorted(recorded) == [("a", 99), ("b", 21)]


@pytest.mark.usefixtures("fast_poll")
async def test_repeated_url_in_one_batch_is_imported_once():
    """Dedup has to cover the input batch, not just the project.

    The helper only ever compared against tasks *already in the project*, so a
    caller whose item query returned an image twice imported it twice in a
    single call, with nothing in the project yet to compare against.
    """
    ls = SlowImportLabelStudio([31])
    dup = _task("same")

    recorded: List[tuple] = []
    result = await _run(ls, [dup, dict(dup)], ["first", "second"], recorded)

    assert len(ls.import_calls[0]) == 1, "the repeated URL is imported once"
    assert result == (2, 0) and result.complete
    assert sorted(recorded) == [("first", 31), ("second", 31)]


async def _no_sleep(_seconds):
    return None


@pytest.mark.usefixtures("fast_poll")
async def test_throttled_listing_is_retried_not_propagated(monkeypatch):
    """A 429 is the other route into the duplicating retry.

    `populate_utils` had no throttle handling at all, unlike the sync path
    beside it, so a rate limit failed the activity and Temporal retried it --
    the same race, reached a different way.
    """
    monkeypatch.setattr(ls_mod, "_throttle_sleep", _no_sleep)
    ls = SlowImportLabelStudio([41])
    ls.list_raises = [
        ApiError(status_code=429, body={"detail": "Request was throttled."})
    ]

    recorded: List[tuple] = []
    result = await _run(ls, [_task("ccc")], ["c"], recorded)

    assert result == (1, 0) and result.complete
    assert recorded == [("c", 41)]


@pytest.mark.usefixtures("fast_poll")
async def test_a_retry_reconciles_instead_of_re_importing():
    """The bound on `POPULATE_MAX_ATTEMPTS` is only safe because of this.

    An attempt that already issued an import records the marker in its
    heartbeat; Temporal hands that to the next attempt, which must reconcile
    rather than import again. Without it, widening the retry window -- so an LS
    blip does not fail the populate child and, through `dispatch_populate`, its
    preprocess parent -- would widen the duplication window with it.
    """
    ls = SlowImportLabelStudio([51, 52], visible_after_lists=99)
    tasks = [_task("ddd"), _task("eee")]
    recorded: List[tuple] = []
    call = _call(ls, tasks, ["d", "e"], recorded)

    beats: List[tuple] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *args: beats.append(args)
    await env.run(call)

    assert len(ls.import_calls) == 1
    assert (sut.IMPORT_ISSUED, 901) in beats, "the import must be heartbeated"

    # Attempt 2 of the *same* activity: Temporal replays the last heartbeat.
    env2 = ActivityEnvironment()
    env2.info = replace(
        env2.info, attempt=2, heartbeat_details=[sut.IMPORT_ISSUED, 901]
    )
    ls.reveal_all()
    await env2.run(call)

    assert len(ls.import_calls) == 1, "the retry must not re-issue the import"
    assert sorted(recorded) == [("d", 51), ("e", 52)]


@pytest.mark.usefixtures("fast_poll")
async def test_a_retry_reconciles_even_while_the_import_is_still_invisible():
    """The case the marker exists for. v1's test above lets the import
    materialise before attempt 2, so the project listing alone already stops
    the re-import there; here it has not, and only the marker does."""
    ls = SlowImportLabelStudio([91, 92], visible_after_lists=99)
    tasks = [_task("iii"), _task("jjj")]
    call = _call(ls, tasks, ["i", "j"], [])
    await ActivityEnvironment().run(call)

    retry = ActivityEnvironment()
    retry.info = replace(
        retry.info, attempt=2, heartbeat_details=[sut.IMPORT_ISSUED, 901]
    )
    result = await retry.run(call)

    assert len(ls.import_calls) == 1, "the retry must not re-issue the import"
    assert result == (0, 2)


@pytest.mark.usefixtures("fast_poll")
async def test_another_projects_marker_does_not_stop_this_import():
    """The marker names its project: an attempt that imported into one does
    not stop the import into another."""
    ls = SlowImportLabelStudio([95])
    retry = ActivityEnvironment()
    retry.info = replace(
        retry.info, attempt=2, heartbeat_details=[sut.IMPORT_ISSUED, 12345]
    )

    result = await retry.run(_call(ls, [_task("kkk")], ["k"], []))

    assert len(ls.import_calls) == 1
    assert result == (1, 0)


@pytest.mark.usefixtures("fast_poll")
async def test_the_poll_stops_once_the_import_is_visible():
    """One listing to dedupe, one to find the import: no more."""
    ls = SlowImportLabelStudio([96])

    await _run(ls, [_task("lll")], ["l"], [])

    assert ls.tasks.list.call_count == 2


@pytest.mark.usefixtures("fast_poll")
async def test_every_heartbeat_after_the_import_carries_the_marker(monkeypatch):
    """A bare `activity.heartbeat()` clears the details, so one after the
    import would hand the next attempt nothing to reconcile by -- including
    the back-off heartbeat of a throttled listing."""
    monkeypatch.setattr(ls_mod, "_throttle_sleep", _no_sleep)
    ls = SlowImportLabelStudio([81, 82], visible_after_lists=1)
    original_import = ls.projects.import_tasks.side_effect

    def import_then_throttle(*args, **kwargs):
        result = original_import(*args, **kwargs)
        ls.list_raises = [ApiError(status_code=429, body={"detail": "slow down"})]
        return result

    ls.projects.import_tasks.side_effect = import_then_throttle
    beats: List[tuple] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *args: beats.append(args)

    await env.run(_call(ls, [_task("ggg"), _task("hhh")], ["g", "h"], []))

    first = beats.index((sut.IMPORT_ISSUED, 901))
    assert beats[first:] == [(sut.IMPORT_ISSUED, 901)] * len(beats[first:])
    assert len(beats[first:]) > 1


@pytest.mark.usefixtures("fast_poll")
async def test_existing_duplicates_are_reported_loudly(caplog):
    """Nothing else notices a duplicate.

    The label tables are unique on (image, project), so the DB cannot show one.
    It surfaces only as a labeller served the same frame twice, or as a row
    tracking the empty twin of its own annotation.
    """
    ls = SlowImportLabelStudio([61])
    same = "s3://bucket/p/dup.JPG"
    ls.seed(
        SimpleNamespace(id=1, data={"image": _resolve_url(1, same)}),
        SimpleNamespace(id=2, data={"image": _resolve_url(2, same)}),
    )

    recorded: List[tuple] = []
    with caplog.at_level(logging.ERROR):
        await _run(ls, [{"data": {"image": same}}], ["d"], recorded)

    assert any(
        "duplicate task" in record.getMessage() for record in caplog.records
    ), "a project already holding duplicates must be logged at ERROR"


@pytest.mark.usefixtures("fast_poll")
async def test_a_deferred_batch_is_reported_loudly(caplog):
    """The old raise was loud; the tolerant path has to replace that signal."""
    ls = SlowImportLabelStudio([71], visible_after_lists=99)

    recorded: List[tuple] = []
    with caplog.at_level(logging.ERROR):
        result = await _run(ls, [_task("fff")], ["f"], recorded)

    assert result.deferred == 1
    assert any(
        "deferred to the next run" in record.getMessage() for record in caplog.records
    )


async def test_an_empty_batch_touches_nothing():
    ls = SlowImportLabelStudio([])

    result = await _run(ls, [], [], [])

    assert result == (0, 0) and result.complete
    ls.tasks.list.assert_not_called()


async def test_tasks_and_items_must_be_parallel():
    with pytest.raises(RuntimeError, match="must be parallel"):
        await _run(SlowImportLabelStudio([]), [_task("a")], [], [])


async def test_a_task_without_an_image_cannot_be_anchored():
    with pytest.raises(RuntimeError, match="missing an image URL"):
        await _run(SlowImportLabelStudio([]), [{"data": {}}], ["x"], [])
