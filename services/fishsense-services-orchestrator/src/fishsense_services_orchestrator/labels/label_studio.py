"""The Label Studio adapter: the one place the SDK is touched.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/utils.py (the throttle handling, the
heartbeat-pumped listing, `_coerce_updated_at`, `resolve_annotator_label_studio_id`).
Behaviour is v1's. v2 change: the SDK is wrapped once, and its tasks become
`LabelStudioTask`s here. The SDK's models are loosely typed (its task model
calls `annotations` a string; the payload is a list of dicts), and v1 read them
loosely all through the sync.

The write side's SDK calls are here too, ported from fishsense-lite@77e8f8e5
.../activities/populate_utils.py (`_resolve_workspace_id`, `_call_ls` and the
`ls.*` calls in its create, heal, storage, import and publish helpers); the
helpers themselves are in `labels.populate`. v2 change: every one of those
calls backs off through a 429. v1 did so only for the import's listing and
import, so a throttle on create, heal, storage or publish failed the activity.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from label_studio_sdk.client import LabelStudio
from label_studio_sdk.core import ApiError
from pydantic import BaseModel, SecretStr
from pydantic_core import to_jsonable_python
from pydantic_settings import BaseSettings, SettingsConfigDict
from temporalio import activity

__all__ = [
    "LabelStudioClient",
    "LabelStudioPrediction",
    "LabelStudioProject",
    "LabelStudioSettings",
    "LabelStudioTask",
    "THROTTLE_DEFAULT_WAIT_SECONDS",
    "THROTTLE_MAX_ATTEMPTS",
    "resolve_annotator_label_studio_id",
    "throttle_wait_seconds",
]

THROTTLE_MAX_ATTEMPTS = 5
THROTTLE_DEFAULT_WAIT_SECONDS = 30.0

# Heartbeat cadence for the long initial listing on a backlog project.
# Comfortably under the workflow's 2 min heartbeat_timeout.
_LISTING_HEARTBEAT_INTERVAL_SECONDS = 30.0


class LabelStudioSettings(BaseSettings):
    """Label Studio, from ``FISHSENSE_LABEL_STUDIO_*``."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_LABEL_STUDIO_")

    url: str
    api_key: SecretStr
    #: The workspace per-dive projects are created in (`FishSense` in prod).
    #: Unset (OSS Label Studio, local dev) means the default workspace.
    workspace: str = ""


async def _throttle_sleep(seconds: float) -> None:
    """Back off after a 429. Indirected so tests exercise the path unslowed."""
    await asyncio.sleep(seconds)


def throttle_wait_seconds(error: ApiError) -> float | None:
    """Seconds to wait if `error` is a throttle, else None.

    Label Studio answers 429 with `{"detail": "Request was throttled.
    Expected available in 48 seconds."}` -- honour that hint rather than
    guessing, plus a small margin.
    """
    if getattr(error, "status_code", None) != 429:
        return None
    body = getattr(error, "body", None)
    detail = body.get("detail", "") if isinstance(body, dict) else str(body or "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*second", str(detail))
    return float(match.group(1)) + 2.0 if match else THROTTLE_DEFAULT_WAIT_SECONDS


def _coerce_updated_at(value: Any) -> datetime | None:
    """A `datetime` from a task's `updated_at`: the SDK hands back ISO strings
    or datetimes. Anything else is None -- "no comparable timestamp", which
    conservatively processes the task regardless of the cursor."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _coerce_label_studio_id(value: Any) -> int | None:
    """An int id from `value`, or None. `bool` is rejected explicitly --
    it subclasses int, and `True` must never resolve to user 1."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def resolve_annotator_label_studio_id(annotators: Any) -> int | None:
    """Label Studio user id of the most recent annotator, or None.

    Self-hosted LS returns `task.annotators` as a list of ints; hosted LS
    (app.heartex.com) as a list of dicts (``{"user_id": 141592, "id": 141592,
    ...}``). Accepts either, so a mixed or rolled-back instance still works.
    """
    if not annotators:
        return None
    last = annotators[-1]
    if isinstance(last, dict):
        # `user_id` is the annotator; `id` is the same value on hosted LS but
        # is checked second in case a future payload separates them.
        for key in ("user_id", "id"):
            resolved = _coerce_label_studio_id(last.get(key))
            if resolved is not None:
                return resolved
        return None
    return _coerce_label_studio_id(last)


def _payload(raw: Any) -> dict[str, Any]:
    """The task as Label Studio sent it, as JSON.

    Read from the model's values rather than its serializer: the SDK's models
    are wrong about hosted Label Studio (annotations are not strings,
    annotators not ints), so serializing warns on every task, every hour, and
    its unchecked model doesn't pass `warnings=False` on.
    """
    if not isinstance(raw, BaseModel):
        return {}
    values = {**vars(raw), **(getattr(raw, "__pydantic_extra__", None) or {})}
    return to_jsonable_python(
        {k: v for k, v in values.items() if not k.startswith("_")}
    )


@dataclass(frozen=True)
class LabelStudioTask:
    """A Label Studio task, read once from the SDK's loosely-typed object."""

    id: int
    annotator_id: int | None = None
    annotations: list[dict[str, Any]] = field(default_factory=list)
    is_labeled: bool = False
    updated_at: datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_sdk(cls, raw: Any) -> "LabelStudioTask":
        annotations = getattr(raw, "annotations", None)
        return cls(
            id=raw.id,
            annotator_id=resolve_annotator_label_studio_id(
                getattr(raw, "annotators", None)
            ),
            annotations=annotations if isinstance(annotations, list) else [],
            is_labeled=bool(getattr(raw, "is_labeled", False)),
            updated_at=_coerce_updated_at(getattr(raw, "updated_at", None)),
            payload=_payload(raw),
        )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class LabelStudioProject:
    """What the write side reads of a project. `label_config` is None when the
    listing omitted it (then the detail view has it)."""

    id: int
    title: str
    label_config: str | None = None
    model_version: str | None = None

    @classmethod
    def from_sdk(cls, raw: Any, *, id: int | None = None) -> "LabelStudioProject":
        # pylint: disable=redefined-builtin
        return cls(
            id=getattr(raw, "id", None) if id is None else id,
            title=_text(getattr(raw, "title", None)) or "",
            label_config=_text(getattr(raw, "label_config", None)),
            model_version=_text(getattr(raw, "model_version", None)),
        )


#: What a heartbeat sends. The import heartbeats a marker (see
#: `labels.populate.IMPORT_ISSUED`), and a bare `activity.heartbeat()` would
#: clear it, so every call a heartbeat can come from takes the caller's.
Beat = Callable[[], None]

#: The details each activity attempt last heartbeated through
#: `sticky_heartbeat`, by task token, so `heartbeat_again` can re-send them.
#: Bounded: attempts end, and a token is never reused.
_STICKY: "OrderedDict[bytes, tuple]" = OrderedDict()
_STICKY_MAX = 4096


def sticky_heartbeat(*details: Any) -> None:
    """Heartbeat `details`, and have every later heartbeat in this attempt
    re-send them (`heartbeat_again`). The import's IMPORT_ISSUED marker is
    what makes its retry safe; a bare heartbeat after it -- a throttled
    publish in the same activity, say -- would clear it."""
    token = activity.info().task_token
    _STICKY[token] = details
    _STICKY.move_to_end(token)
    while len(_STICKY) > _STICKY_MAX:
        _STICKY.popitem(last=False)
    activity.heartbeat(*details)


def heartbeat_again() -> None:
    """Heartbeat, re-sending whatever this attempt last sent sticky."""
    activity.heartbeat(*_STICKY.get(activity.info().task_token, ()))


@dataclass(frozen=True)
class LabelStudioPrediction:
    """A prediction attached to a task: what the backfills dedupe on."""

    task_id: int | None
    model_version: str | None


class LabelStudioClient:
    def __init__(self, sdk: LabelStudio) -> None:
        self._sdk = sdk

    @classmethod
    def from_settings(cls, settings: LabelStudioSettings) -> "LabelStudioClient":
        return cls(
            LabelStudio(
                base_url=settings.url, api_key=settings.api_key.get_secret_value()
            )
        )

    async def project_exists(self, project_id: int) -> bool:
        """Whether `project_id` exists, retrying through rate limits.

        A 429 is NOT a missing project. Both arrive as `ApiError`, and v1
        treating them the same meant a throttled probe logged "missing" and
        returned -- silently skipping the project *and* returning before the
        cursor write, so the skip repeated every hour forever (prod project
        274633: 86/86 labeled in Label Studio, 0 completed in the database).

        Raises when still throttled after `THROTTLE_MAX_ATTEMPTS`, so the
        activity fails and Temporal retries: a loud failure is correct here,
        because the alternative is the silent skip this replaces.
        """
        for attempt in range(THROTTLE_MAX_ATTEMPTS):
            try:
                await asyncio.to_thread(self._sdk.projects.get, project_id)
                return True
            except ApiError as error:
                wait = throttle_wait_seconds(error)
                if wait is None:
                    # 404 and friends -- genuinely gone. Skipping is right.
                    activity.logger.warning(
                        "label studio project missing project_id=%d error=%s",
                        project_id,
                        error,
                    )
                    return False
                activity.logger.info(
                    "label studio throttled project_id=%d attempt=%d/%d; "
                    "backing off %.0fs",
                    project_id,
                    attempt + 1,
                    THROTTLE_MAX_ATTEMPTS,
                    wait,
                )
                heartbeat_again()
                await asyncio.sleep(wait)

        raise RuntimeError(
            f"Label Studio still throttling project {project_id} after "
            f"{THROTTLE_MAX_ATTEMPTS} attempts -- failing rather than skipping "
            "it, so the cursor is not advanced and the next run retries."
        )

    async def list_tasks(self, project_id: int) -> list[LabelStudioTask]:
        """Every task in the project, paged in a worker thread while the main
        thread heartbeats: on a backlog project the pager's synchronous HTTP
        calls can run for the activity's whole timeout."""
        raw = await self._listing(project_id, beat=heartbeat_again)
        return [LabelStudioTask.from_sdk(task) for task in raw]

    async def _listing(self, project_id: int, *, beat: Beat) -> list[Any]:
        async def _pump() -> None:
            while True:
                await asyncio.sleep(_LISTING_HEARTBEAT_INTERVAL_SECONDS)
                beat()

        pump = asyncio.create_task(_pump())
        try:
            return await asyncio.to_thread(
                lambda: list(self._sdk.tasks.list(project=project_id))
            )
        finally:
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass

    # -- the write side ---------------------------------------------------------------

    async def _throttled(
        self,
        call: Callable[[], Awaitable[Any]],
        *,
        what: str,
        beat: Beat | None = None,
    ) -> Any:
        """Run `call`, backing off through 429s (v1's `_call_ls`).

        A throttle that failed a populate activity was retried by Temporal,
        which is the one thing that duplicates tasks (see
        `labels.populate.import_tasks_and_record_labels`). Swallowing the retry
        here keeps a throttle from becoming a duplicate.
        """
        for attempt in range(THROTTLE_MAX_ATTEMPTS):
            try:
                return await call()
            except ApiError as error:
                wait = throttle_wait_seconds(error)
                if wait is None:
                    raise
                activity.logger.info(
                    "label studio throttled on %s attempt=%d/%d; backing off %.0fs",
                    what,
                    attempt + 1,
                    THROTTLE_MAX_ATTEMPTS,
                    wait,
                )
                (beat or heartbeat_again)()
                await _throttle_sleep(wait)
        raise RuntimeError(
            f"Label Studio still throttling {what} after {THROTTLE_MAX_ATTEMPTS} "
            "attempts"
        )

    async def _sdk_call(
        self, what: str, call: Callable[[], Any], *, beat: Beat | None = None
    ) -> Any:
        """A blocking SDK call, off the event loop and through throttles."""
        return await self._throttled(
            lambda: asyncio.to_thread(call), what=what, beat=beat
        )

    async def workspace_id(self, name: str) -> int | None:
        """The id of the workspace titled `name`; None when `name` is unset.

        Raises if a name is configured but no such workspace exists -- a
        silent fallback would scatter projects into the wrong workspace.
        """
        name = (name or "").strip()
        if not name:
            return None
        workspaces = await self._sdk_call(
            "workspaces.list", lambda: list(self._sdk.workspaces.list())
        )
        matches = [w for w in workspaces if w.title == name]
        if not matches:
            raise RuntimeError(
                f"Label Studio workspace {name!r} not found "
                "(FISHSENSE_LABEL_STUDIO_WORKSPACE) -- create it or fix the config."
            )
        return matches[0].id

    async def projects(self, workspace_id: int | None) -> list[LabelStudioProject]:
        """Every project in the workspace (every project, when None).
        `workspaces` is a server-side filter."""

        def _list() -> list[Any]:
            if workspace_id is None:
                return list(self._sdk.projects.list())
            return list(self._sdk.projects.list(workspaces=[workspace_id]))

        raw = await self._sdk_call("projects.list", _list)
        return [LabelStudioProject.from_sdk(p) for p in raw]

    async def project(self, project_id: int) -> LabelStudioProject | None:
        """The project's detail view; None if Label Studio has no such project."""
        try:
            raw = await self._sdk_call(
                f"projects.get({project_id})",
                lambda: self._sdk.projects.get(id=project_id),
            )
        except ApiError as error:
            if getattr(error, "status_code", None) == 404:
                return None
            raise
        return LabelStudioProject.from_sdk(raw, id=project_id)

    async def create_project(
        self, *, title: str, label_config: str, workspace_id: int | None
    ) -> int:
        """Create a project as a draft (`is_published` left at Label Studio's
        default): it is published once its task set is complete."""
        created = await self._sdk_call(
            f"projects.create({title!r})",
            lambda: self._sdk.projects.create(
                title=title, label_config=label_config, workspace=workspace_id
            ),
        )
        return created.id

    async def update_project(self, project_id: int, **fields: Any) -> None:
        await self._sdk_call(
            f"projects.update({project_id})",
            lambda: self._sdk.projects.update(id=project_id, **fields),
        )

    # -- predictions (the laser and head/tail backfills) ---------------------------

    async def predictions(self, project_id: int) -> list[LabelStudioPrediction]:
        """What is already attached across a project: the backfills' idempotency
        key is `(task, model_version)` (fishsense-lite@77e8f8e5
        backfill_{laser,headtail}_predictions_activity)."""
        listed = await self._sdk_call(
            f"predictions.list({project_id})",
            lambda: list(self._sdk.predictions.list(project=project_id)),
        )
        return [
            LabelStudioPrediction(
                task_id=getattr(p, "task", None),
                model_version=getattr(p, "model_version", None),
            )
            for p in listed
        ]

    async def create_prediction(
        self, task_id: int, model_version: str, result: Sequence[dict]
    ) -> None:
        """Attach a prediction to an existing task -- not a re-import, which
        would duplicate the task."""
        await self._sdk_call(
            f"predictions.create(task={task_id})",
            lambda: self._sdk.predictions.create(
                task=task_id, model_version=model_version, result=list(result)
            ),
        )

    async def s3_import_storages(self, project_id: int) -> list[tuple[Any, Any]]:
        """`(bucket, title)` of each S3 source storage on the project."""
        storages = await self._sdk_call(
            f"import_storage.s3.list({project_id})",
            lambda: list(self._sdk.import_storage.s3.list(project=project_id)),
        )
        return [
            (getattr(s, "bucket", None), getattr(s, "title", None)) for s in storages
        ]

    async def create_s3_import_storage(self, **fields: Any) -> None:
        await self._sdk_call(
            f"import_storage.s3.create({fields.get('project')})",
            lambda: self._sdk.import_storage.s3.create(**fields),
        )

    async def task_image_urls(
        self, project_id: int, *, beat: Beat
    ) -> list[tuple[int, str | None]]:
        """`(task id, image URL as listed)` for every task in the project.

        The URL is `data.image`, else `data.img` (prod configs use either),
        read raw: hosted Label Studio lists it as a presign resolve-wrapper.
        """
        raw = await self._throttled(
            lambda: self._listing(project_id, beat=beat),
            what=f"tasks.list({project_id})",
            beat=beat,
        )
        urls = []
        for task in raw:
            data = getattr(task, "data", None) or {}
            urls.append((task.id, data.get("image") or data.get("img")))
        return urls

    async def import_tasks(
        self, project_id: int, tasks: Sequence[dict], *, beat: Beat
    ) -> None:
        """Import `tasks`. Hosted Label Studio imports asynchronously: this
        returns an import job, not task ids, and the tasks may not be listable
        yet."""
        await self._sdk_call(
            f"import_tasks({project_id})",
            lambda: self._sdk.projects.import_tasks(
                project_id, request=list(tasks), return_task_ids=True
            ),
            beat=beat,
        )
