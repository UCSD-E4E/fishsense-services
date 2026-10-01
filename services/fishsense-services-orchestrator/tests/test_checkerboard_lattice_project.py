"""The lattice study's Label Studio project: its tasks, predictions, import.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_checkerboard_lattice_predictions.py (names and reasons are v1's)
and create_checkerboard_lattice_label_studio_project_activity.py's contract.
The overlay itself is the processor's; what is pinned here is the translation
from a `CheckerboardLatticeRender` into a Label Studio task: the coordinate
conversion, which frames become tasks at all, and the control names -- a
silent contract with the labeling config.

v2 changes, each pinned here:

* **the project is titled per tenant** (port-plan): v1's title for `lab`,
  `"{v1 title} ({slug})"` for any other -- every tenant shares one Label
  Studio workspace, so a bare title search would find another tenant's
  project -- and it is found or created through `LabelProjects` (recorded,
  kind `checkerboard_lattice`, no dive);
* a task's image is the render's `ObjectRef` (v1 rebuilt a URL from a
  checksum), which still carries the checksum -- a verdict stays traceable
  to its frame -- and nothing that could unblind the study.
"""

from __future__ import annotations

import uuid

import pytest
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.slate_calibration import CheckerboardLatticeRender
from fishsense_services_orchestrator.calibration import lattice as sut
from fishsense_services_orchestrator.calibration.contracts import (
    LatticeImport,
    LatticeProject,
)
from fishsense_services_orchestrator.labels.populate import ImportResult

TENANT = uuid.UUID(int=1)
CHECKSUM = "a" * 32


def _image(checksum=CHECKSUM) -> ObjectRef:
    return ObjectRef(
        bucket="labels",
        key=f"tenants/{TENANT}/checkerboard_lattice_jpeg/{checksum}.JPG",
    )


def _render(**overrides) -> CheckerboardLatticeRender:
    kwargs = {
        "capture_id": uuid.UUID(int=7),
        "image": _image(),
        "detected_rows": 2,
        "detected_cols": 3,
        "median_spacing_px": 32.0,
        "corners": [
            [0.0, 0.0],
            [200.0, 0.0],
            [400.0, 0.0],
            [0.0, 150.0],
            [200.0, 150.0],
            [400.0, 150.0],
        ],
        "width": 400,
        "height": 300,
    }
    kwargs.update(overrides)
    return CheckerboardLatticeRender(**kwargs)


def test_one_keypoint_per_detected_corner():
    """The labeler counts marks, so the count must be the detection's."""
    [prediction] = sut.lattice_predictions(_render())

    assert len(prediction["result"]) == 6


def test_keypoints_are_percentages_of_the_rectified_frame():
    """Label Studio stores keypoints as percentages, not pixels. Getting this
    wrong does not error -- it silently scatters the marks, and a labeler
    would report a lattice fault that is really a units bug."""
    [prediction] = sut.lattice_predictions(_render())
    values = [item["value"] for item in prediction["result"]]

    assert (values[0]["x"], values[0]["y"]) == (0.0, 0.0)
    # (200, 150) of a 400x300 frame is dead centre.
    assert (values[4]["x"], values[4]["y"]) == (50.0, 50.0)
    # (400, 0) is the right edge, top.
    assert (values[2]["x"], values[2]["y"]) == (100.0, 0.0)


def test_results_carry_the_frame_dimensions():
    [prediction] = sut.lattice_predictions(_render())

    assert {i["original_width"] for i in prediction["result"]} == {400}
    assert {i["original_height"] for i in prediction["result"]} == {300}


def test_control_names_match_the_labeling_config():
    """`from_name`/`to_name` are a silent contract with the project XML: a
    mismatch stores the prediction and renders nothing -- the invisible-
    prediction failure that cost five frames of dive 94 by hand."""
    xml = sut.CHECKERBOARD_LATTICE_LABELING_CONFIG_XML

    [prediction] = sut.lattice_predictions(_render())
    item = prediction["result"][0]

    assert f'name="{item["from_name"]}"' in xml
    assert f'<Image name="{item["to_name"]}"' in xml
    assert item["type"] == "keypointlabels"
    assert f'value="{item["value"]["keypointlabels"][0]}"' in xml


def test_predictions_carry_a_model_version():
    """Inline-at-import is what makes them visible: `import_tasks` sets the
    project's `model_version` only when the task carries its predictions.
    This stage must never backfill predictions onto pre-existing tasks."""
    [prediction] = sut.lattice_predictions(_render())

    assert prediction["model_version"] == sut.LATTICE_MODEL_VERSION


@pytest.mark.parametrize(
    "overrides",
    [
        {"corners": None, "detected_rows": None, "detected_cols": None},
        {"width": None},
        {"height": None},
        {"corners": []},
    ],
)
def test_nothing_placeable_yields_no_prediction(overrides):
    assert not sut.lattice_predictions(_render(**overrides))


def test_a_skipped_frame_becomes_no_task():
    """A frame the detector rejected is not evidence either way about the
    lattice, and only dilutes the queue."""
    skipped = CheckerboardLatticeRender(
        capture_id=uuid.UUID(int=9), skip_reason="no_usable_board"
    )

    assert sut.renders_worth_labeling([_render(), skipped]) == [_render()]


def test_a_rendered_frame_without_corners_becomes_no_task():
    """Belt and braces on the processor contract: an image with no marks can
    only be read as a detection failure -- a verdict about the wrong thing."""
    hollow = _render(corners=None, detected_rows=None, detected_cols=None)

    assert sut.renders_worth_labeling([hollow]) == []


def test_a_render_with_no_image_becomes_no_task():
    """v2: the image ref is what the task shows; without one there is no task."""
    assert sut.renders_worth_labeling([_render(image=None)]) == []


def test_task_data_points_at_the_lattice_jpeg_folder():
    """Not the stage-0.1 folder: same checksum, different picture, and a laser
    labeler is looking at the other one."""
    data = sut.build_lattice_task(_render())["data"]

    assert "/checkerboard_lattice_jpeg/" in data["image"]
    assert "/preprocess_jpeg/" not in data["image"]


def test_task_data_leaks_nothing_about_the_detection():
    """Every data key is a sortable column in the Label Studio Data Manager:
    `median_spacing_px` is the diagnostic itself, the grid shape leaks it
    more weakly, and a monotonic id reassembles the per-dive blocks the
    shuffle exists to break up."""
    data = sut.build_lattice_task(_render())["data"]

    assert set(data) == {"image", "img"}


def test_a_verdict_is_still_traceable_to_its_frame():
    """The checksum rides in the image URL, and checksums sort randomly."""
    data = sut.build_lattice_task(_render())["data"]

    assert CHECKSUM in data["image"]


# --- the project --------------------------------------------------------------


class FakeProjects:
    def __init__(self):
        self.calls = []

    async def ensure_project(
        self, tenant_id, kind, *, title, labeling_config_xml, dive_id=None
    ):
        self.calls.append((tenant_id, kind, title, dive_id))
        return 4242


@pytest.mark.parametrize(
    ("slug", "title"),
    [
        ("lab", "Checkerboard Lattice Verification"),
        ("reef", "Checkerboard Lattice Verification (reef)"),
    ],
)
async def test_the_study_project_is_titled_per_tenant(slug, title):
    """v1's title for `lab` finds v1's project; any other tenant gets its own,
    since every tenant shares one workspace."""
    projects = FakeProjects()
    activities = sut.LatticeProjectActivities(
        label_projects=projects, label_studio=None
    )

    project = await ActivityEnvironment().run(
        activities.create_checkerboard_lattice_label_studio_project,
        LatticeProject(tenant_id=TENANT, tenant_slug=slug),
    )

    assert project == 4242
    assert projects.calls == [(TENANT, "checkerboard_lattice", title, None)]


def test_a_long_slug_still_fits_label_studios_title_limit():
    """Label Studio rejects a title over 50 characters with a 400."""
    title = sut.lattice_project_title("a-very-long-partner-organisation-slug")

    assert len(title) <= 50
    assert title.startswith("Checkerboard Lattice Verification (")


# --- the activity's handling of ImportResult ----------------------------------


def _patch_import(monkeypatch, result, published: list):
    async def _fake_import(_ls, **_kwargs):
        return result

    async def _fake_publish(_ls, project_id: int):
        published.append(project_id)

    monkeypatch.setattr(sut, "import_tasks_and_record_labels", _fake_import)
    monkeypatch.setattr(sut, "publish_label_studio_project", _fake_publish)


async def _populate(renders) -> int:
    activities = sut.LatticeProjectActivities(
        label_projects=None, label_studio=object()
    )
    return await ActivityEnvironment().run(
        activities.populate_checkerboard_lattice_label_studio_project,
        LatticeImport(tenant_id=TENANT, ls_project_id=7, renders=renders),
    )


async def test_the_activity_returns_a_count_not_the_result_tuple(monkeypatch):
    """`import_tasks_and_record_labels` returns an `ImportResult`; returned
    as-is it would serialise as `[recorded, deferred]` with no error."""
    published: list[int] = []
    _patch_import(monkeypatch, ImportResult(recorded=3, deferred=0), published)

    imported = await _populate([_render()])

    assert imported == 3
    assert isinstance(imported, int)


async def test_a_complete_import_publishes_the_project(monkeypatch):
    published: list[int] = []
    _patch_import(monkeypatch, ImportResult(recorded=2, deferred=0), published)

    await _populate([_render()])

    assert published == [7]


async def test_a_deferred_import_leaves_the_project_unpublished(monkeypatch):
    """The study is on-demand and has no next firing to reconcile a partial
    import, so an unnoticed partial publish is what a labeler would work."""
    published: list[int] = []
    _patch_import(monkeypatch, ImportResult(recorded=2, deferred=5), published)

    imported = await _populate([_render()])

    assert imported == 2
    assert not published


async def test_nothing_worth_labeling_imports_nothing(monkeypatch):
    published: list[int] = []
    _patch_import(monkeypatch, ImportResult(recorded=99, deferred=0), published)

    skipped = CheckerboardLatticeRender(
        capture_id=uuid.UUID(int=9), skip_reason="no_usable_board"
    )
    imported = await _populate([skipped])

    assert imported == 0
    assert not published
