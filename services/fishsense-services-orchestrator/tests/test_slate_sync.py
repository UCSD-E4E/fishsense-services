"""The dive-slate label sync: parsing a task, removing the panel offset.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_sync_dive_slate_labels_activity.py. Names, bodies and reasons are
v1's; the harness changed (a fake catalog and fake PDFs in place of v1's
mocked clients). The cursor, the bounded concurrency and the per-task
heartbeat are the shared `labels.sync.sync_label_studio_project`'s, pinned in
test_label_sync.py.

The Label Studio image is a composite -- the PDF panel on the left, the photo
on the right -- so reference points and the slate rectangle arrive in
composite pixels and must be shifted left by the panel's width to land in the
photo. If the geometry is there but the offset cannot be computed, the label
FAILS rather than persisting composite-space geometry with a 0-px shift: the
old silent fallback stranded 104 prod rows out of frame.

v1's in-frame bound is kept as it is, and what it can and cannot see is
pinned (`test_the_right_edge_bound_cannot_see_an_under_shift`).

v2 changes, each pinned here:

* a missing PDF is staged before the sync gives up on it (`SlatePdfs.aspect`);
* the sync writes a `SlateSync` through the store (only the columns it owns);
* the task's slate template comes from the store's lookup by task, which also
  says whether a live label anchors it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pymupdf
import pytest
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.slate_store import SlateTaskTemplate
from fishsense_services_orchestrator.labels.label_studio import LabelStudioTask
from fishsense_services_orchestrator.labels.sync import LabelProject
from fishsense_services_orchestrator.slates import sync as sut
from fishsense_services_orchestrator.slates.pdfs import (
    compute_pdf_panel_aspect_ratio,
    compute_pdf_panel_width_in_composite,
)

LAB, REEF = uuid.UUID(int=1), uuid.UUID(int=2)
SLATE = uuid.UUID(int=7)
CAPTURE = uuid.UUID(int=10)


def _task(task_id: int, *, annotations=None, is_labeled=True, annotator=141592):
    raw = SimpleNamespace(
        id=task_id,
        annotators=[annotator] if annotator else [],
        annotations=annotations or [],
        is_labeled=is_labeled,
        updated_at="2026-05-01T00:00:00Z",
    )
    return LabelStudioTask.from_sdk(raw)


# ----------------------------- pure parser -----------------------------


def test_parse_results_extracts_all_fields():
    annotation = {
        "result": [
            {
                "from_name": "reference_points",
                "value": {"x": 50.0, "y": 25.0, "keypointlabels": ["Reference Point"]},
                "original_width": 1000,
                "original_height": 500,
            },
            {
                "from_name": "reference_points",
                "value": {"x": 75.0, "y": 30.0, "keypointlabels": ["Reference Point"]},
                "original_width": 1000,
                "original_height": 500,
            },
            {
                "from_name": "slate",
                "value": {
                    "x": 60.0,
                    "y": 10.0,
                    "width": 20.0,
                    "height": 30.0,
                    "rectanglelabels": ["Slate"],
                },
                "original_width": 1000,
                "original_height": 500,
            },
            {
                "from_name": "skipped_points",
                "value": {"text": ["1", "3", "5"]},
            },
        ]
    }

    parsed = sut.parse_results(annotation)
    assert "upside_down" not in parsed  # removed 2026-07-31
    assert parsed["reference_points"] == [(500.0, 125.0), (750.0, 150.0)]
    assert parsed["slate_rectangle"] == [(600.0, 50.0), (800.0, 200.0)]
    assert parsed["skipped_points"] == [0, 2, 4]
    assert parsed["original_height"] == 500.0
    assert parsed["original_width"] == 1000.0


def test_parse_results_handles_minimal_annotation():
    parsed = sut.parse_results({"result": []})
    assert parsed["reference_points"] == []
    assert parsed["slate_rectangle"] is None
    assert parsed["skipped_points"] is None
    assert parsed["original_height"] is None
    assert parsed["original_width"] is None


def test_parse_results_ignores_removed_upside_down_control():
    # A stale project may still emit an `upside_down` result; it must be a no-op.
    annotation = {
        "result": [
            {"from_name": "upside_down", "value": {"choices": ["Slate upside down"]}}
        ]
    }
    parsed = sut.parse_results(annotation)
    assert "upside_down" not in parsed
    assert parsed["reference_points"] == []


# --------------------------- pdf aspect ratio --------------------------


def _make_synthetic_pdf(width_pts: float, height_pts: float) -> bytes:
    doc = pymupdf.open()
    doc.new_page(width=width_pts, height=height_pts)
    out = doc.tobytes()
    doc.close()
    return out


def test_compute_pdf_panel_aspect_ratio_matches_page_rect():
    pdf_bytes = _make_synthetic_pdf(216.0, 108.0)  # 2:1
    aspect = compute_pdf_panel_aspect_ratio(pdf_bytes)
    assert aspect == pytest.approx(2.0, rel=1e-6)


def test_compute_pdf_panel_width_in_composite_scales_by_original_height():
    # 2:1 aspect, composite height 500 -> panel width 1000.
    pw = compute_pdf_panel_width_in_composite(2.0, 500.0)
    assert pw == pytest.approx(1000.0, rel=1e-6)


# ------------------- panel-offset fail-hard + bounds (Bug 1) -------------------


def test_assert_in_frame_rejects_out_of_bounds():
    sut.assert_in_frame([(50.0, 1.0)], 100.0, task_id=1, slate_id=SLATE)
    sut.assert_in_frame([(100.0, 1.0)], 100.0, task_id=1, slate_id=SLATE)
    with pytest.raises(ValueError):
        sut.assert_in_frame([(150.0, 1.0)], 100.0, task_id=1, slate_id=SLATE)
    with pytest.raises(ValueError):
        sut.assert_in_frame([(-5.0, 1.0)], 100.0, task_id=1, slate_id=SLATE)


def _geometry_annotation(composite_x: float, y: float, ow: float, oh: float) -> dict:
    """One reference point at composite pixel (composite_x, y) on an ow x oh canvas."""
    return {
        "result": [
            {
                "from_name": "reference_points",
                "value": {
                    "x": composite_x / ow * 100.0,
                    "y": y / oh * 100.0,
                    "keypointlabels": ["Reference Point"],
                },
                "original_width": ow,
                "original_height": oh,
            }
        ]
    }


class FakeCatalog:
    def __init__(self, *, template=SLATE, anchored=True):
        self.template = template
        self.anchored = anchored
        self.applied: list[tuple[uuid.UUID, int, Any]] = []
        self.advanced = []

    async def member_tenants(self):
        return [LAB, REEF]

    async def slate_label_studio_projects(self, tenant_id):
        return {LAB: [66, 67], REEF: [90]}[tenant_id]

    async def slate_template_for_task(self, tenant_id, ls_task_id):
        if not self.anchored:
            return None
        return SlateTaskTemplate(capture_id=CAPTURE, slate_template_id=self.template)

    async def apply_slate_sync(self, tenant_id, ls_task_id, sync):
        self.applied.append((tenant_id, ls_task_id, sync))
        return True

    async def sync_cursor(self, tenant_id, kind, project_id):
        return None

    async def advance_sync_cursor(self, tenant_id, kind, project_id, at):
        self.advanced.append((tenant_id, kind, project_id))


class FakePdfs:
    def __init__(self, pdf: bytes | None):
        self.pdf = pdf
        self.asked = []

    async def aspect(self, tenant_id, slate_template_id):
        self.asked.append((tenant_id, slate_template_id))
        if self.pdf is None:
            raise ValueError("slate template has no NAS path")
        return compute_pdf_panel_aspect_ratio(self.pdf)


async def _apply(task, *, catalog=None, pdf=b"", cache=None):
    catalog = catalog or FakeCatalog()
    pdfs = FakePdfs(_make_synthetic_pdf(216.0, 108.0) if pdf == b"" else pdf)
    await sut.apply_slate_task(
        LAB, task, catalog=catalog, pdfs=pdfs, aspects={} if cache is None else cache
    )
    return catalog, pdfs


async def test_update_slate_label_subtracts_panel_offset_and_persists():
    # 2:1 PDF, composite height 100 -> panel width 200; canvas 300 wide ->
    # photo width 100. A point at composite x=250 lands at photo x=50.
    task = _task(1, annotations=[_geometry_annotation(250.0, 50.0, 300.0, 100.0)])

    catalog, _ = await _apply(task)

    ((tenant, task_id, sync),) = catalog.applied
    assert (tenant, task_id) == (LAB, 1)
    assert len(sync.reference_points) == 1
    px, py = sync.reference_points[0]
    assert px == pytest.approx(50.0)
    assert py == pytest.approx(50.0)
    assert sync.completed is True
    assert sync.ls_labeler_id == 141592


async def test_update_slate_label_raises_and_skips_persist_when_pdf_missing():
    # The old fallback silently persisted composite-space coords with a 0px
    # shift. It must fail the label instead -- no write, exception propagates.
    task = _task(1, annotations=[_geometry_annotation(250.0, 50.0, 300.0, 100.0)])

    with pytest.raises(ValueError, match="panel offset"):
        await _apply(task, pdf=None)


async def test_update_slate_label_raises_when_slate_unresolvable():
    catalog = FakeCatalog(template=None)  # the dive has no slate template
    task = _task(1, annotations=[_geometry_annotation(250.0, 50.0, 300.0, 100.0)])

    with pytest.raises(ValueError, match="slate template"):
        await _apply(task, catalog=catalog)

    assert catalog.applied == []


async def test_update_slate_label_raises_when_shift_lands_out_of_frame():
    # Wrong-template panel width: composite x=250 needs a 200px shift, but a
    # too-large panel (aspect 3.0 -> 300px) overshoots to -50, out of frame.
    task = _task(1, annotations=[_geometry_annotation(250.0, 50.0, 300.0, 100.0)])
    catalog = FakeCatalog()

    with pytest.raises(ValueError, match="outside"):
        await _apply(task, catalog=catalog, pdf=_make_synthetic_pdf(324.0, 108.0))

    assert catalog.applied == []


async def test_the_right_edge_bound_cannot_see_an_under_shift():
    """The limit of the bound, pinned so nobody reads more into it.

    The port map flagged v1 passing the composite's width, not the photo's,
    as the right-hand bound. Subtracting the same panel from both sides makes
    the photo-width form `x - panel <= width - panel`, i.e. `x <= width`,
    which every Label Studio point satisfies -- so neither form can catch an
    under-shifted (too-narrow panel) point, and v1's bound is kept. What does
    catch a wrong template is the left bound: a too-*wide* panel overshoots
    past 0 (the test above). A too-narrow one is caught by nothing here.
    """
    narrow_panel = _make_synthetic_pdf(54.0, 108.0)  # 0.5:1 -> 50 px, real 200
    catalog, _ = await _apply(
        _task(1, annotations=[_geometry_annotation(290.0, 50.0, 300.0, 100.0)]),
        pdf=narrow_panel,
    )

    ((_, _, sync),) = catalog.applied
    assert sync.reference_points[0][0] == pytest.approx(240.0)


async def test_skipped_points_are_stored_zero_based():
    """Label Studio shows them 1-based to humans; v1 stored 0-based."""
    task = _task(
        1,
        annotations=[{"result": [{"from_name": "skipped_points",
                                  "value": {"text": ["2", "4"]}}]}],
    )  # fmt: skip

    catalog, pdfs = await _apply(task)

    ((_, _, sync),) = catalog.applied
    assert sync.skipped_points == [1, 3]
    assert sync.reference_points is None and sync.slate_rectangle is None
    assert pdfs.asked == [], "no geometry, so no offset is needed"


async def test_a_task_with_no_live_label_is_skipped():
    """Labels are created when a project is populated; the sync only updates
    them, and a task with none is not an error."""
    catalog = FakeCatalog(anchored=False)

    await _apply(_task(1, annotations=[_geometry_annotation(250, 50, 300, 100)]),
                 catalog=catalog)  # fmt: skip

    assert catalog.applied == []


async def test_an_unlabeled_task_still_syncs_its_state():
    """`completed` follows the task whatever it holds (v1)."""
    catalog, _ = await _apply(_task(1, is_labeled=False, annotator=None))

    ((_, _, sync),) = catalog.applied
    assert sync.completed is False
    assert sync.ls_labeler_id is None


async def test_the_pdf_aspect_is_read_once_per_template():
    """v1's per-activity cache: a project's tasks share one dive's template."""
    cache: dict = {}
    pdfs = FakePdfs(_make_synthetic_pdf(216.0, 108.0))
    catalog = FakeCatalog()
    for task_id in (1, 2, 3):
        await sut.apply_slate_task(
            LAB,
            _task(
                task_id, annotations=[_geometry_annotation(250.0, 50.0, 300.0, 100.0)]
            ),
            catalog=catalog,
            pdfs=pdfs,
            aspects=cache,
        )

    assert pdfs.asked == [(LAB, SLATE)]


# ----------------------------- the activities -----------------------------


class FakeLabelStudio:
    def __init__(self, tasks):
        self.tasks = tasks

    async def project_exists(self, project_id):
        return True

    async def list_tasks(self, project_id):
        return self.tasks


async def test_lists_every_served_tenants_slate_projects():
    activities = sut.SlateSyncActivities(
        catalog=FakeCatalog(),
        label_studio_factory=lambda: FakeLabelStudio([]),
        pdfs=FakePdfs(None),
    )

    projects = await ActivityEnvironment().run(activities.slate_label_projects)

    assert projects == [
        LabelProject(LAB, 66),
        LabelProject(LAB, 67),
        LabelProject(REEF, 90),
    ]


async def test_syncs_a_projects_tasks_under_the_slate_cursor():
    catalog = FakeCatalog()
    activities = sut.SlateSyncActivities(
        catalog=catalog,
        label_studio_factory=lambda: FakeLabelStudio(
            [_task(1, annotations=[_geometry_annotation(250.0, 50.0, 300.0, 100.0)])]
        ),
        pdfs=FakePdfs(_make_synthetic_pdf(216.0, 108.0)),
    )

    await ActivityEnvironment().run(activities.sync_slate_labels, LabelProject(LAB, 66))

    assert [task_id for _, task_id, _ in catalog.applied] == [1]
    assert catalog.advanced == [(LAB, "slate", 66)]
