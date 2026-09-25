"""The Label Studio adapter: the one place the SDK is touched.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/utils.py (the throttle handling, the
heartbeat-pumped listing, `_coerce_updated_at`, `resolve_annotator_label_studio_id`).
Behaviour is v1's. v2 change: the SDK is wrapped once, and its tasks become
`LabelStudioTask`s here. The SDK's models are loosely typed (its task model
calls `annotations` a string; the payload is a list of dicts), and v1 read them
loosely all through the sync.
"""

from __future__ import annotations

import asyncio
import re
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
                activity.heartbeat()
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

        async def _pump() -> None:
            while True:
                await asyncio.sleep(_LISTING_HEARTBEAT_INTERVAL_SECONDS)
                activity.heartbeat()

        pump = asyncio.create_task(_pump())
        try:
            raw = await asyncio.to_thread(
                lambda: list(self._sdk.tasks.list(project=project_id))
            )
        finally:
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
        return [LabelStudioTask.from_sdk(task) for task in raw]
