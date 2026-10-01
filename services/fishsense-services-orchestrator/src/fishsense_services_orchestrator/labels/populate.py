"""The Label Studio write side every populate and prediction backfill shares.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/populate_utils.py, with the create
step the create_{laser,species,headtail,dive_slate,checkerboard_lattice}_
label_studio_project activities shared. Each populate stage creates LS tasks
pointing at the processed JPEGs, imports them in one batch, then records a
per-image label row anchoring the (image, LS task, LS project) triple. Each
create stage idempotently materialises a per-dive project from a stored
labeling-config XML; populate calls create first to get the target project.
Behaviour is v1's. The kinds' own populate activities belong to their slices;
this is the part they share, parameterised by kind.

v2 changes:

* **projects are recorded** (label_studio_projects, via `LabelProjectCatalog`)
  and looked up there first, keyed on the dive, so a renamed dive keeps its
  project. v1's title search is the fallback, and it heals the record, so
  every project v1 created is still found. A project is recorded as soon as it
  exists, before its storage is registered;
* titles embed the dive's `number`, which is v1's dive id for a migrated dive,
  so they are v1's titles;
* `ensure_project_shows_predictions` matches `#{number}` as a whole number:
  v1's substring test let dive 9 claim dive 94's project;
* the SDK is touched only in `labels.label_studio`, whose calls all back off
  through 429s (v1: only the import's listing and import);
* the Label Studio S3 storage has its own settings (one bucket, an optional
  prefix, and the key Label Studio presigns with), where v1 fell back from a
  labels bucket to its scratch bucket and from a presign key to its main key.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, NamedTuple, Protocol

from label_studio_sdk.core import ApiError
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from temporalio import activity

from fishsense_services_api.label_project_store import (
    KINDS,
    DiveForTitle,
    RecordedProject,
)
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_orchestrator.labels.label_studio import (
    Beat,
    LabelStudioClient,
    LabelStudioProject,
    heartbeat_again,
    sticky_heartbeat,
)

__all__ = [
    "IMPORT_ISSUED",
    "LS_PROJECT_TITLE_MAX",
    "LS_S3_STORAGE_TITLE",
    "ImportResult",
    "LabelProjectCatalog",
    "LabelProjects",
    "LabelStudioStorageSettings",
    "TaskImage",
    "build_image_url",
    "build_per_dive_title",
    "build_task_data",
    "create_or_get_label_studio_project",
    "ensure_label_studio_s3_storage",
    "ensure_project_shows_predictions",
    "heal_labeling_config",
    "import_tasks_and_record_labels",
    "publish_label_studio_project",
]

# Title given to the per-project Garage S3 source storage. Matching on
# (bucket, title) makes registration idempotent across re-runs.
LS_S3_STORAGE_TITLE = "garage"

# LS rejects `Project.title` over 50 characters with a 400.
LS_PROJECT_TITLE_MAX = 50


class LabelStudioStorageSettings(BaseSettings):
    """Where Label Studio reads the processed JPEGs, from
    ``FISHSENSE_LABEL_STUDIO_S3_*``.

    v1's `object_store.labels_bucket` / `labels_prefix` and its presign key.
    A migrated project's tasks are deduplicated by the URL built from these,
    so for v1's JPEGs they must stay v1's (prod: `labels-fishsense-lite`,
    prefix `fishsense-lite`).
    """

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_LABEL_STUDIO_S3_")

    bucket: str
    prefix: str = ""
    endpoint_url: str
    region: str
    #: The key Label Studio presigns GETs with: read-only is enough.
    access_key: str
    secret_key: SecretStr

    @property
    def key_prefix(self) -> str:
        return (self.prefix or "").strip("/")


def build_image_url(image: ObjectRef) -> str:
    """The `s3://` URI of a processed JPEG, exactly where the object store
    located (or wrote) it: v1's key for a migrated frame, whose tasks already
    hold that URL, and the tenant's for a new one. Label Studio resolves it to
    a presigned GET at serve time through the project's S3 source storage.
    Never rebuilt from settings, so it can't name a key nothing wrote."""
    return image.uri


@dataclass(frozen=True)
class TaskImage:
    """The capture a task shows: what `build_task_data` needs of it."""

    #: The capture's `number`: v1's image id for a migrated capture.
    number: int
    #: Its processed JPEG, as the object store located it.
    image: ObjectRef
    captured_at: datetime | None


def build_task_data(image: TaskImage) -> dict:
    """The `data` payload for one imported Label Studio task.

    `image` and `img` are both emitted because prod labeling configs across
    the four stages reference one or the other; that predates this helper.

    `taken` and `image_id` exist so a project can be sorted back into capture
    order from the Data Manager. Task order is fixed at import -- `id` and
    `inner_id` are assigned then, and the sequential labeling stream walks
    `id` -- so a project that imported out of order cannot be reordered
    afterwards without deleting its tasks and losing their annotations.

    `taken` is ISO-8601 **text**, not a number: Label Studio sorts data columns
    lexically, so a numeric index sorts "0, 1, 10, 100". ISO-8601 is the format
    whose text order is its chronological order. `image_id` is the tiebreak,
    since EXIF resolution is one second and these cameras fire ~4 frames a
    second; it keeps v1's key, and is the capture's number (v1's image id for
    a migrated capture).
    """
    url = build_image_url(image.image)
    taken = image.captured_at
    return {
        "image": url,
        "img": url,
        "taken": taken.isoformat() if taken is not None else None,
        "image_id": image.number,
    }


async def ensure_label_studio_s3_storage(
    ls: LabelStudioClient, project_id: int, storage: LabelStudioStorageSettings
) -> None:
    """Idempotently register a Garage S3 *source* storage on `project_id` so
    LS presigns the `s3://` image URIs in each task's data.

    Open-source LS storages are per-project and projects are per-dive, so
    every freshly-created project needs this. Matches on (bucket, title) to
    avoid duplicate registrations on re-runs. Does NOT sync: tasks are
    imported explicitly; this connection exists only to enable presigning.
    """
    for bucket, title in await ls.s3_import_storages(project_id):
        if bucket == storage.bucket and title == LS_S3_STORAGE_TITLE:
            return

    fields: dict[str, Any] = {
        "project": project_id,
        "title": LS_S3_STORAGE_TITLE,
        "bucket": storage.bucket,
        "s3endpoint": storage.endpoint_url,
        "region_name": storage.region,
        "aws_access_key_id": storage.access_key,
        "aws_secret_access_key": storage.secret_key.get_secret_value(),
        "presign": True,
        "use_blob_urls": False,
    }
    if storage.key_prefix:
        fields["prefix"] = storage.key_prefix
    await ls.create_s3_import_storage(**fields)
    activity.logger.info(
        "registered LS S3 storage project_id=%d bucket=%s", project_id, storage.bucket
    )


def _canonical_label_config(config: str | None) -> str | None:
    """Structure-only form of a labeling-config XML, or None if unparseable.

    LS reformats `label_config` server-side (indentation, self-closing
    style, attribute order), so a raw string compare reports drift on every
    single run and would re-PATCH the project hourly forever. Comparing
    parsed structure instead means we only write when the *choices* really
    changed.
    """
    if not config or not isinstance(config, str):
        return None
    try:
        root = ET.fromstring(config)
    except ET.ParseError:
        return None

    def _node(element):
        return (
            element.tag,
            tuple(sorted((k, (v or "").strip()) for k, v in element.attrib.items())),
            tuple(_node(child) for child in element),
        )

    return repr(_node(root))


def _label_config_differs(current: str | None, desired: str) -> bool:
    """True when `current` needs to be replaced by `desired`."""
    canonical_current = _canonical_label_config(current)
    canonical_desired = _canonical_label_config(desired)
    if canonical_current is None or canonical_desired is None:
        # Unparseable on either side -- fall back to a whitespace-normalized
        # compare rather than guessing (and rather than looping on a PATCH
        # that can never converge).
        raw_current = current if isinstance(current, str) else ""
        return " ".join(raw_current.split()) != " ".join(desired.split())
    return canonical_current != canonical_desired


async def heal_labeling_config(
    ls: LabelStudioClient, project: Any, desired_xml: str
) -> bool:
    """Push `desired_xml` onto an already-created project when it drifted.

    Without this, editing a stage's labeling-config constant only affects
    projects created *after* the deploy -- every existing per-dive project
    keeps the config it was born with, so a taxonomy change (e.g. swapping
    the Fish Model choices) silently never reaches annotators.

    Returns True when the config was rewritten.
    """
    current = getattr(project, "label_config", None)
    if not isinstance(current, str):
        # `projects.list` may omit the config; fetch the detail view.
        detail = await ls.project(project.id)
        current = None if detail is None else detail.label_config

    if not _label_config_differs(current, desired_xml):
        return False

    try:
        await ls.update_project(project.id, label_config=desired_xml)
    except ApiError as e:
        # LS rejects a config that would invalidate existing annotations
        # (e.g. dropping a choice value someone already used). Keep the old
        # config and keep populating rather than failing the whole stage.
        activity.logger.warning(
            "Could not update labeling config for LS project id=%d: %s. "
            "Project keeps its previous config; reconcile by hand if the "
            "taxonomy change is required.",
            project.id,
            e,
        )
        return False

    activity.logger.info(
        "Updated labeling config for LS project id=%d (config drift healed)",
        project.id,
    )
    return True


async def _converge(
    ls: LabelStudioClient,
    project: LabelStudioProject,
    labeling_config_xml: str,
    storage: LabelStudioStorageSettings,
) -> None:
    """What an existing project gets on every create: its config healed onto
    the current constant, and its storage."""
    if labeling_config_xml:
        await heal_labeling_config(ls, project, labeling_config_xml)
    await ensure_label_studio_s3_storage(ls, project.id, storage)


async def _find_by_title(
    ls: LabelStudioClient, title: str, workspace: str
) -> tuple[LabelStudioProject | None, int | None]:
    """v1's lookup: the project titled `title` in the workspace, and the
    workspace's id. Scoped to the workspace so a same-titled project in
    another can't shadow it."""
    workspace_id = await ls.workspace_id(workspace)
    matches = [p for p in await ls.projects(workspace_id) if p.title == title]
    if len(matches) > 1:
        activity.logger.warning(
            "Multiple LS projects titled %r; using id=%d", title, matches[0].id
        )
    return (matches[0] if matches else None), workspace_id


async def _create(
    ls: LabelStudioClient,
    *,
    title: str,
    labeling_config_xml: str,
    workspace_id: int | None,
) -> int:
    if not labeling_config_xml:
        raise RuntimeError(
            f"Cannot create LS project {title!r}: the labeling-config XML "
            "constant is empty. Paste the labeling-config XML from your existing "
            "prod project (Project Settings -> Labeling Interface -> Code) into "
            "the corresponding constant."
        )
    # Created as a draft. Per-dive projects are only published once their task
    # set is complete -- see `publish_label_studio_project`, called by the
    # populate activities -- so a still-filling or JPEG-deferred project stays
    # hidden from annotators until every intended task exists.
    project_id = await ls.create_project(
        title=title, label_config=labeling_config_xml, workspace_id=workspace_id
    )
    activity.logger.info("Created LS project %r (id=%d)", title, project_id)
    return project_id


async def create_or_get_label_studio_project(
    ls: LabelStudioClient,
    *,
    project_title: str,
    labeling_config_xml: str,
    workspace: str,
    storage: LabelStudioStorageSettings,
) -> int:
    """Idempotent create by title: v1's `create_or_get_label_studio_project`.

    Returns the LS project id for `project_title`, creating one with
    `labeling_config_xml` if none exists, healing an existing one's config,
    and idempotently registering the Garage S3 source storage on it. Callers
    with a tenant go through `LabelProjects`, which records the project and
    looks it up first; this is its fallback.
    """
    found, workspace_id = await _find_by_title(ls, project_title, workspace)
    if found is not None:
        await _converge(ls, found, labeling_config_xml, storage)
        return found.id
    project_id = await _create(
        ls,
        title=project_title,
        labeling_config_xml=labeling_config_xml,
        workspace_id=workspace_id,
    )
    await ensure_label_studio_s3_storage(ls, project_id, storage)
    return project_id


def build_per_dive_title(number: int, name: str | None, suffix: str) -> str:
    """Build a per-dive LS project title `"{dive.name} #{number} - {suffix}"`.

    `#{number}` is **always** included so dives that share a `name` still get
    distinct projects -- dive names are NOT unique in prod (mislabeled
    captures, duplicate-named dives, and same-site/same-camera repeats all
    exist), so keying the title on the name alone silently merged two dives
    into one project. `number` is v1's dive id for a migrated dive, so this is
    the title v1 gave its project.

    `name` is truncated (never the `#{number}` tail) to fit LS's 50-char cap,
    so the uniqueness survives even for long names. A nameless dive yields
    `"#{number} - {suffix}"`.
    """
    tail = f"#{number} - {suffix}"
    name = (name or "").strip()
    if not name:
        return tail[:LS_PROJECT_TITLE_MAX]
    budget = LS_PROJECT_TITLE_MAX - len(tail) - 1  # 1 for the space before tail
    if budget < 1:
        return tail[:LS_PROJECT_TITLE_MAX]
    return f"{name[:budget].rstrip()} {tail}"


class LabelProjectCatalog(Protocol):
    """See ``fishsense_services_api.label_project_store.LabelProjectCatalog``."""

    async def dive_for_title(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> DiveForTitle | None: ...

    async def recorded_project(
        self,
        tenant_id: uuid.UUID,
        kind: str,
        *,
        dive_id: uuid.UUID | None,
        title: str,
    ) -> RecordedProject | None: ...

    async def record_project(
        self,
        tenant_id: uuid.UUID,
        kind: str,
        *,
        dive_id: uuid.UUID | None,
        ls_project_id: int,
        title: str,
    ) -> None: ...


class LabelProjects:
    """Find or create a tenant's Label Studio project of a kind, record first.

    What every slice's create step calls (v1's create_*_label_studio_project
    activities): the laser, species, head/tail and slate stages with a dive
    and a title suffix, the lattice study with its fixed title and no dive.
    """

    def __init__(
        self,
        *,
        catalog: LabelProjectCatalog,
        label_studio: LabelStudioClient,
        workspace: str,
        storage: LabelStudioStorageSettings,
    ) -> None:
        self._catalog = catalog
        self._ls = label_studio
        self._workspace = workspace
        self._storage = storage

    async def ensure_dive_project(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        kind: str,
        *,
        suffix: str,
        labeling_config_xml: str,
    ) -> int:
        """The dive's project of `kind`, titled `{name} #{number} - {suffix}`."""
        dive = await self._catalog.dive_for_title(tenant_id, dive_id)
        if dive is None:
            raise RuntimeError(
                f"Cannot build LS project title for dive {dive_id}: "
                "no such dive in the tenant"
            )
        return await self.ensure_project(
            tenant_id,
            kind,
            title=build_per_dive_title(dive.number, dive.name, suffix),
            labeling_config_xml=labeling_config_xml,
            dive_id=dive_id,
        )

    async def ensure_project(
        self,
        tenant_id: uuid.UUID,
        kind: str,
        *,
        title: str,
        labeling_config_xml: str,
        dive_id: uuid.UUID | None = None,
    ) -> int:
        """The project of `kind` for the dive (or, with no dive, titled `title`).

        The record first. A recorded project still gets its config healed and
        its storage ensured, as v1's found project did. With no record, or one
        whose project was deleted in Label Studio, v1's title search and create
        run, and whatever they return is recorded.
        """
        _check_kind(kind)
        recorded = await self._catalog.recorded_project(
            tenant_id, kind, dive_id=dive_id, title=title
        )
        if recorded is not None:
            project = await self._ls.project(recorded.ls_project_id)
            if project is not None:
                await _converge(self._ls, project, labeling_config_xml, self._storage)
                return project.id
            activity.logger.warning(
                "recorded LS project id=%d (%r) is gone from Label Studio; "
                "finding or creating %r instead",
                recorded.ls_project_id,
                recorded.title,
                title,
            )

        found, workspace_id = await _find_by_title(self._ls, title, self._workspace)
        if found is not None:
            await self._record(tenant_id, kind, dive_id, found.id, title)
            await _converge(self._ls, found, labeling_config_xml, self._storage)
            return found.id

        project_id = await _create(
            self._ls,
            title=title,
            labeling_config_xml=labeling_config_xml,
            workspace_id=workspace_id,
        )
        # Recorded before its storage: the project exists now, and a failure
        # registering the storage must not leave it unknown to the registry.
        await self._record(tenant_id, kind, dive_id, project_id, title)
        await ensure_label_studio_s3_storage(self._ls, project_id, self._storage)
        return project_id

    async def _record(self, tenant_id, kind, dive_id, project_id, title) -> None:
        await self._catalog.record_project(
            tenant_id, kind, dive_id=dive_id, ls_project_id=project_id, title=title
        )


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown Label Studio project kind {kind!r}; one of {KINDS}")


# -- import -------------------------------------------------------------------------

# Hosted LS's import is asynchronous -- the created tasks are usually listable
# immediately, but the import call returns before they're guaranteed queryable.
# Bounded poll so a small lag doesn't force a Temporal activity retry.
_IMPORT_VISIBILITY_ATTEMPTS = 12
_IMPORT_VISIBILITY_INTERVAL_S = 2.0

# Heartbeat marker recording that this activity execution already issued an
# import for a project. Temporal hands the last heartbeat's details to the next
# *attempt* of the same activity, so this is what makes a retry reconcile
# rather than re-import -- and therefore what makes a generous retry policy
# safe. Every heartbeat after the import must carry it: `activity.heartbeat()`
# with no args clears the details.
IMPORT_ISSUED = "ls-import-issued"


def _import_already_issued(project_id: int) -> bool:
    """Whether an earlier attempt of this activity already imported."""
    try:
        details = activity.info().heartbeat_details
    except RuntimeError:
        return False
    return (
        len(details) >= 2 and details[0] == IMPORT_ISSUED and details[1] == project_id
    )


def _tasks_needing_import(
    tasks: List[dict], urls: List[str], known: dict, project_id: int
) -> List[dict]:
    """The subset of `tasks` that should actually be sent to LS.

    Three ways a task drops out, and each one is a duplicate that reached prod:

    * it is already in the project (the #343 dedup);
    * the same URL appears earlier in this very batch -- the project comparison
      alone could not catch that, so a caller whose item query returned an
      image twice imported it twice in one call;
    * an earlier attempt of *this* activity already issued an import. What it
      created may still be materialising, so importing again is precisely the
      duplicate-making move. Reconcile instead.
    """
    to_import: List[dict] = []
    batch_seen: set = set()
    for task, url in zip(tasks, urls):
        if url in known or url in batch_seen:
            continue
        batch_seen.add(url)
        to_import.append(task)

    if to_import and _import_already_issued(project_id):
        activity.logger.info(
            "populate: attempt %d already issued an import for project %d; "
            "reconciling rather than re-importing %d task(s)",
            activity.info().attempt,
            project_id,
            len(to_import),
        )
        return []
    return to_import


class ImportResult(NamedTuple):
    """What `import_tasks_and_record_labels` managed to anchor.

    `deferred` is items whose LS task is not yet listable, so no label row
    could be written for them. It is not an error -- see the function's
    docstring -- but callers must not publish a project while it is non-zero,
    or annotators get a half-populated task list.
    """

    recorded: int
    deferred: int

    @property
    def complete(self) -> bool:
        """Every item in the batch now has a task and a row."""
        return self.deferred == 0


def _task_image_url(task: dict) -> str | None:
    data = task.get("data", {}) or {}
    return data.get("image") or data.get("img")


def _normalize_image_url(url: str | None) -> str | None:
    """Reduce an LS task image URL to a stable comparison key (the s3 URI).

    Freshly-built task data carries `s3://...`, but when hosted LS *lists*
    tasks it returns `data.image` as a per-task presign resolve-wrapper
    `/tasks/{task_id}/resolve/?fileuri=<base64(s3://...)>`. The embedded task
    id makes that string unique per task, so comparing it raw against the
    built `s3://` URL never matched -- silently defeating dedup and
    re-importing the whole batch every run (projects ballooned to thousands of
    tasks, 0 rows). Decode the `fileuri` back to the s3 URI so both forms
    compare equal.
    """
    if not url or url.startswith("s3://"):
        return url
    if "fileuri=" in url:
        query = urllib.parse.urlparse(url).query
        fileuri = urllib.parse.parse_qs(query).get("fileuri", [None])[0]
        if fileuri:
            try:
                return base64.b64decode(fileuri).decode("utf-8")
            except (binascii.Error, ValueError, UnicodeDecodeError):
                return url
    return url


async def import_tasks_and_record_labels(
    ls: LabelStudioClient,
    *,
    project_id: int,
    tasks: List[dict],
    record_label: Callable[[Any, int], Awaitable[None]],
    items: Iterable[Any],
) -> ImportResult:
    """Import `tasks` to LS, then record one label row per (item, task_id).

    `record_label(item, task_id)` is the per-kind hook that writes the label
    row anchoring the (image, LS task, project) triple. Rows are written in
    parallel; the hook must be an upsert so partial-failure replay is safe.

    **Hosted LS imports asynchronously**: `import_tasks` returns an import-job
    id, NOT task ids -- only OSS returns task ids. v1 once read
    `imported.task_ids`, which crashed *after* the tasks were created but
    *before* writing any label rows, so every Temporal retry re-imported the
    whole batch (projects ballooned to tens of thousands of tasks with zero
    label rows).

    So task ids are resolved by listing the project's tasks and matching each
    input task by its image URL (checksum-based, unique per image), and tasks
    **already in the project are not imported again** -- a retry after a
    mid-activity failure then re-imports nothing and just writes the missing
    rows.

    **An import that has not become visible is not an error.** v1 used to
    raise, saying "retrying (dedupe prevents dupes)" -- which holds only once
    the previous import has materialised, exactly the condition that just
    failed. Temporal retried, the retry's dedup listing still could not see the
    in-flight import, and it imported the whole set again: ten projects came to
    hold two tasks per image, one held 23 copies of three images (found
    2026-09-09).

    A duplicate is not merely wasted queue: it splits a row from its own
    annotation (seven dive-437 head/tail labels were invisible to the pipeline
    because the row tracked the empty twin), and it hands a labeller a second,
    *ungated* copy of a frame the laser auto-accept gate had already judged.

    So rows are written for whatever is visible and the shortfall is logged at
    ERROR. The images whose tasks are still in flight keep their place in the
    cohort and are reconciled by the next scheduled populate, by which time the
    dedup listing can see them. Slower, and it cannot duplicate.

    Two things replace the signal the raise used to carry. `ImportResult.
    deferred` is non-zero while the task set is incomplete, and **every caller
    must gate `publish_label_studio_project` on it**, so a shortfall shows up
    as a project that stays a draft rather than one annotators see
    half-populated. And a project that already contains duplicate tasks is
    logged at ERROR on every run, because nothing else notices -- the label
    tables are unique on (capture, project), so a duplicate is invisible to
    the database.

    Within one activity execution a retry never re-imports: the first import
    heartbeats `IMPORT_ISSUED`, and Temporal hands that to the next attempt.
    """
    tasks = list(tasks)
    items_list = list(items)
    if not tasks:
        return ImportResult(recorded=0, deferred=0)
    if len(tasks) != len(items_list):
        raise RuntimeError(
            f"import_tasks_and_record_labels: {len(tasks)} tasks for "
            f"{len(items_list)} items -- the two lists must be parallel"
        )

    urls = [_normalize_image_url(_task_image_url(t)) for t in tasks]
    if any(u is None for u in urls):
        raise RuntimeError(
            "import task missing an image URL in `data` -- cannot anchor label"
        )

    beat: Beat = heartbeat_again

    async def _list_known() -> dict:
        """`url -> task_id` for the whole project."""
        mapping: dict = {}
        duplicates = 0
        for task_id, raw_url in await ls.task_image_urls(project_id, beat=beat):
            url = _normalize_image_url(raw_url)
            if url is None:
                continue
            if url in mapping:
                duplicates += 1
            mapping[url] = task_id
        if duplicates:
            # Loud, because nothing else notices: duplicates are invisible to
            # the database (the label tables are unique on (capture, project)),
            # they split a row from its annotation, and they hand a labeller
            # an ungated copy of a frame the auto-accept gate already judged.
            activity.logger.error(
                "populate: project %d already holds %d duplicate task(s) -- "
                "a labeller is being served the same image twice",
                project_id,
                duplicates,
            )
        return mapping

    # Dedupe against what's already imported so a retry doesn't duplicate.
    known = await _list_known()
    to_import = _tasks_needing_import(tasks, urls, known, project_id)

    if to_import:
        await ls.import_tasks(project_id, to_import, beat=beat)

        def beat_imported() -> None:
            sticky_heartbeat(IMPORT_ISSUED, project_id)

        beat = beat_imported
        beat()

        want = {u for u in urls if u not in known}
        for _ in range(_IMPORT_VISIBILITY_ATTEMPTS):
            known = await _list_known()
            if want <= known.keys():
                break
            beat()
            await asyncio.sleep(_IMPORT_VISIBILITY_INTERVAL_S)

    recorded = 0
    deferred = 0
    async with asyncio.TaskGroup() as tg:
        for item, url in zip(items_list, urls):
            task_id = known.get(url)
            if task_id is None:
                deferred += 1
                continue
            recorded += 1
            tg.create_task(record_label(item, task_id))
            beat()

    if deferred:
        activity.logger.error(
            "populate: %d of %d task(s) for project %d are not listable yet, "
            "so their label rows are deferred to the next run; the project is "
            "left unpublished. Re-importing them is what duplicates tasks. "
            "If this repeats every run the URLs are not matching -- compare "
            "`_normalize_image_url` against what `tasks.list` returns.",
            deferred,
            len(items_list),
            project_id,
        )

    return ImportResult(recorded=recorded, deferred=deferred)


async def publish_label_studio_project(ls: LabelStudioClient, project_id: int) -> None:
    """Idempotently publish an LS project so annotators can see it.

    Called by the populate activities **only once a project's task set is
    complete** -- never at create time. On LS Enterprise a project is created
    as a draft (invisible to annotators); publishing is deferred until every
    intended task exists so a still-filling project (e.g. species images
    whose JPEGs haven't been processed yet, which the JPEG gate defers) is
    never shown half-populated. `is_published=True` is idempotent, so
    re-running populate on an already-complete project is a harmless no-op.
    """
    await ls.update_project(project_id, is_published=True)
    activity.logger.info("Published LS project id=%d", project_id)


def _is_dives_own(title: str, dive_number: int) -> bool:
    """Whether `title` carries `#{dive_number}` -- the whole number, so dive 9
    is not taken for dive 94 (v1's substring test did)."""
    return re.search(rf"#{dive_number}(?!\d)", title) is not None


async def ensure_project_shows_predictions(
    ls: LabelStudioClient,
    dive_number: int,
    tags_by_project: Mapping[int, Mapping[str, int]] | None,
    current_tag: str | None = None,
) -> int:
    """Point each per-dive project's `model_version` at a tier it actually has.

    **Attaching a prediction is not enough to show one.** Label Studio
    surfaces predictions to annotators only for the version named in the
    *project's* `model_version`; with it unset, `show_collab_predictions=True`
    and hundreds of stored predictions still render a blank task. A labeler
    worked five frames of dive 94 by hand on 2026-09-10 with 334 invisible
    predictions sitting on the project.

    It hid because `import_tasks` sets the field for free when tasks carry
    `predictions` inline, so a dive whose tasks were created *after* its
    predictions looked fine. Only dives backfilled onto pre-existing tasks were
    blank -- which is most of the corpus, since populate seeds tasks long
    before any prediction exists. Both the laser and head/tail backfills attach
    that way, so both need this.

    `tags_by_project` maps a project id to a count of the tags its placeable
    predictions carry.

    **The tag with the most predictions wins**, ties broken toward
    `current_tag`. Not "prefer the newest tier": a project already showing 40
    fallback predictions must not be switched to a tier holding 2, which would
    blank 38 tasks to reveal 2. Maximising what a labeler can see is the
    objective, and it converges on its own: once an upgrade pass overtakes the
    old tier, the newer tag becomes the majority and the project flips.

    **Only per-dive projects are touched**, identified by `#{dive_number}` in
    the title -- the marker `build_per_dive_title` always emits.
    `model_version` is project-*global* while these tags are derived from one
    dive, so on a grandfathered shared project (the legacy 71/76 layout) two
    dives at different tiers would overwrite each other on alternate runs,
    each write blanking the other's tasks while both logged success.

    Never raises: the predictions are attached and correct either way, and
    display configuration is not worth failing an activity that did its work.
    """
    updated = 0
    for project_id, tags in (tags_by_project or {}).items():
        if not tags:
            continue
        want = max(tags.items(), key=lambda kv: (kv[1], kv[0] == current_tag))[0]
        try:
            project = await ls.project(project_id)
            title = "" if project is None else project.title
            if not _is_dives_own(title, dive_number):
                activity.logger.info(
                    "project %s (%r) is not dive %d's own project; "
                    "leaving model_version alone",
                    project_id,
                    title[:60],
                    dive_number,
                )
                continue
            if (project.model_version or "") == want:
                continue
            await ls.update_project(project_id, model_version=want)
            updated += 1
            activity.logger.info(
                "project %s: model_version -> %s (predictions now visible)",
                project_id,
                want,
            )
        except Exception as exc:  # pylint: disable=broad-except
            # Logged with the exception rather than just its type: a
            # persistently failing update (missing write scope, 404, a rejected
            # value) leaves the invisibility bug live, and the type alone
            # cannot tell you which. Same reasoning as `heal_labeling_config`.
            activity.logger.warning(
                "project %s: could not set model_version to %r: %s",
                project_id,
                want,
                exc,
            )
    return updated
