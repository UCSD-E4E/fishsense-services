"""The dive-slate label sync (stage 12): Label Studio tasks into slate labels.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/
(sync_dive_slate_labels_for_label_studio_project_activity.py,
get_dive_slate_label_studio_project_ids_activity.py).

The Label Studio image is a composite -- the slate PDF panel on the left, the
photo on the right (stage 9) -- so reference points and the slate rectangle
arrive in composite pixels and are shifted left by the panel's rendered
width, `(pdf_w / pdf_h) * original_height`, to land in photo pixels.

v1's rules, kept:

* a task is applied only to the live label it anchors; a task with none is
  skipped (labels are created by populate, not by the sync);
* `completed` follows the task; skipped points (1-based in Label Studio,
  stored 0-based) and geometry are written only when the annotation holds
  them;
* **if the geometry is there but the offset cannot be computed** -- no
  original size, no slate template, no PDF -- **the label fails**, and with
  it the project's sync, so its cursor stays put: the old silent 0-px
  fallback stranded 104 prod rows in composite space;
* a shift that lands a point left of the photo (a too-wide panel: the wrong
  template) fails too. The right-hand bound cannot catch a too-narrow panel
  -- with the same panel on both sides it reduces to `x <= original_width` --
  and is kept as v1 had it;
* the PDF's aspect is read once per template per activity.

v2 changes: projects are listed across every tenant served; the cursor, the
concurrency and the heartbeats are the shared `labels.sync`'s (kind
`slate`, v1's `dive_slate` as `v1_migration` maps it); the store writes only
the columns the sync owns; and a PDF not in scratch is staged rather than
failing the project until a stage-9 run staged it (`SlatePdfs.aspect`).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, List, Protocol

from temporalio import activity

from fishsense_services_api.slate_store import SlateSync, SlateTaskTemplate
from fishsense_services_orchestrator.labels.label_studio import LabelStudioTask
from fishsense_services_orchestrator.labels.sync import (
    LabelProject,
    sync_label_studio_project,
)
from fishsense_services_orchestrator.slates.pdfs import (
    compute_pdf_panel_width_in_composite,
)

__all__ = [
    "SlateSyncActivities",
    "apply_slate_task",
    "assert_in_frame",
    "parse_results",
]

Point = tuple[float, float]


def _shift_x(points: List[Point], dx: float) -> List[Point]:
    return [(x - dx, y) for x, y in points]


def assert_in_frame(
    points: List[Point], photo_width: float, *, task_id: Any, slate_id: Any
) -> None:
    """Reject a panel-offset shift that lands any x outside [0, photo_width].

    `photo_width` is v1's argument, the composite's width; see the module
    docstring for what that bound can and cannot see.
    """
    for x, _ in points:
        if not 0 <= x <= float(photo_width):
            raise ValueError(
                f"slate label task_id={task_id} slate={slate_id}: panel-offset "
                f"shift put x={x:.1f} outside [0, {photo_width:.1f}] — wrong slate "
                f"template or missing offset. Refusing to persist wrong-space "
                f"geometry."
            )


def parse_results(annotation: dict[str, Any]) -> dict[str, Any]:
    """The slate fields of one Label Studio annotation, in composite pixels
    (no offset applied), with `original_width` / `original_height`.

    The `upside_down` Choices control was removed from the labeling config on
    2026-07-31 (never read downstream), so it is not parsed.
    """
    results = annotation.get("result") or []

    reference_points: List[Point] = [
        (
            r["value"]["x"] / 100.0 * r["original_width"],
            r["value"]["y"] / 100.0 * r["original_height"],
        )
        for r in results
        if r["from_name"] == "reference_points"
    ]

    slate_results = [r for r in results if r["from_name"] == "slate"]
    slate_rectangle: List[Point] | None = None
    if slate_results:
        sr = slate_results[0]
        ow = sr["original_width"]
        oh = sr["original_height"]
        slate_rectangle = [
            (sr["value"]["x"] / 100.0 * ow, sr["value"]["y"] / 100.0 * oh),
            (
                (sr["value"]["x"] + sr["value"]["width"]) / 100.0 * ow,
                (sr["value"]["y"] + sr["value"]["height"]) / 100.0 * oh,
            ),
        ]

    skipped_results = [r for r in results if r["from_name"] == "skipped_points"]
    skipped_points: List[int] | None = None
    if skipped_results:
        text = skipped_results[0]["value"].get("text") or []
        # Labelers see 1-based points; the notebook stored 0-based indices.
        skipped_points = [int(p) - 1 for p in text]

    original_height: float | None = None
    original_width: float | None = None
    for r in results:
        if original_height is None and "original_height" in r:
            original_height = float(r["original_height"])
        if original_width is None and "original_width" in r:
            original_width = float(r["original_width"])
        if original_height is not None and original_width is not None:
            break

    return {
        "reference_points": reference_points,
        "slate_rectangle": slate_rectangle,
        "skipped_points": skipped_points,
        "original_height": original_height,
        "original_width": original_width,
    }


class _Catalog(Protocol):
    async def slate_template_for_task(
        self, tenant_id: uuid.UUID, ls_task_id: int
    ) -> SlateTaskTemplate | None: ...

    async def apply_slate_sync(
        self, tenant_id: uuid.UUID, ls_task_id: int, sync: SlateSync
    ) -> bool: ...


class _Pdfs(Protocol):
    async def aspect(
        self, tenant_id: uuid.UUID, slate_template_id: uuid.UUID
    ) -> float: ...


async def _photo_geometry(
    tenant_id, task, anchor, parsed, *, pdfs: _Pdfs, aspects: dict
) -> tuple[list[Point] | None, list[Point] | None]:
    """The parsed geometry, shifted into photo pixels -- or a raise."""
    original_height = parsed["original_height"]
    original_width = parsed["original_width"]
    if original_height is None or original_width is None:
        raise ValueError(
            f"slate label task_id={task.id} has geometry but the LS result is "
            "missing original_width/height; cannot remove the composite panel "
            "offset."
        )
    template = anchor.slate_template_id
    if template is None:
        raise ValueError(
            f"slate label task_id={task.id} capture={anchor.capture_id}: its dive "
            "has no slate template; cannot remove the composite panel offset. "
            "Refusing to persist composite-space geometry."
        )
    if template not in aspects:
        try:
            aspects[template] = await pdfs.aspect(tenant_id, template)
        except Exception as exc:  # pylint: disable=broad-except
            raise ValueError(
                f"slate label task_id={task.id}: the PDF for slate template "
                f"{template} is unavailable ({exc}); cannot remove the composite "
                "panel offset. Refusing to persist composite-space geometry."
            ) from exc
    panel_width = compute_pdf_panel_width_in_composite(
        aspects[template], original_height
    )

    shifted = []
    for points in (parsed["reference_points"], parsed["slate_rectangle"]):
        if not points:
            shifted.append(None)
            continue
        moved = _shift_x(points, panel_width)
        assert_in_frame(moved, original_width, task_id=task.id, slate_id=template)
        shifted.append(moved)
    return shifted[0], shifted[1]


async def apply_slate_task(
    tenant_id: uuid.UUID,
    task: LabelStudioTask,
    *,
    catalog: _Catalog,
    pdfs: _Pdfs,
    aspects: dict,
) -> bool:
    """Write what one task says into the slate label it anchors. False when no
    live label anchors it (skipped). Raises rather than persist geometry in
    composite pixels."""
    anchor = await catalog.slate_template_for_task(tenant_id, task.id)
    if anchor is None:
        return False

    reference_points = slate_rectangle = skipped_points = None
    if task.annotations:
        parsed = parse_results(task.annotations[0])
        skipped_points = parsed["skipped_points"]
        if parsed["reference_points"] or parsed["slate_rectangle"]:
            reference_points, slate_rectangle = await _photo_geometry(
                tenant_id, task, anchor, parsed, pdfs=pdfs, aspects=aspects
            )

    return await catalog.apply_slate_sync(
        tenant_id,
        task.id,
        SlateSync(
            completed=task.is_labeled,
            reference_points=reference_points,
            slate_rectangle=slate_rectangle,
            skipped_points=skipped_points,
            ls_labeler_id=task.annotator_id,
            ls_updated_at=task.updated_at,
            ls_payload=task.payload,
        ),
    )


class SlateSyncActivities:
    def __init__(self, *, catalog, label_studio_factory: Callable, pdfs: _Pdfs) -> None:
        self._catalog = catalog
        self._label_studio_factory = label_studio_factory
        self._pdfs = pdfs

    @activity.defn(name="slate_label_projects")
    async def slate_label_projects(self) -> List[LabelProject]:
        """Every served tenant's Label Studio projects holding live slate labels."""
        projects = [
            LabelProject(tenant_id, project_id)
            for tenant_id in await self._catalog.member_tenants()
            for project_id in await self._catalog.slate_label_studio_projects(tenant_id)
        ]
        activity.logger.info("found %d dive-slate label projects", len(projects))
        return projects

    @activity.defn(name="sync_slate_labels")
    async def sync_slate_labels(self, project: LabelProject) -> None:
        """Sync one project's slate labels in from Label Studio."""
        aspects: dict = {}
        skipped = 0

        async def apply(task: LabelStudioTask) -> None:
            nonlocal skipped
            if not await apply_slate_task(
                project.tenant_id,
                task,
                catalog=self._catalog,
                pdfs=self._pdfs,
                aspects=aspects,
            ):
                skipped += 1

        await sync_label_studio_project(
            project,
            "slate",
            ls=self._label_studio_factory(),
            catalog=self._catalog,
            apply=apply,
        )
        if skipped:
            activity.logger.info(
                "slate sync project_id=%d skipped %d task(s) with no live label",
                project.ls_project_id,
                skipped,
            )
