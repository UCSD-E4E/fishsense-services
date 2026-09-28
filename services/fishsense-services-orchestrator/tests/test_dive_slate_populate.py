"""The dive-slate Label Studio project: create it, and populate it.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_populate_dive_slate_label_studio_project_activity.py and
create_dive_slate_label_studio_project_activity.py's contract. Names and
reasons are v1's; the harness changed: a fake Label Studio adapter, a fake
catalog in place of v1's mocked SDK, and a fake object store for the JPEG
gate. The target selection itself is the API store's (tested on Postgres).

v2 changes, each pinned here:

* the project is found or created through `LabelProjects` (recorded, looked
  up first), kind `slate`, titled with the dive's number;
* a task's image is the JPEG where the object store located it -- v1's key
  for a migrated frame, whose tasks already hold that URL -- never a URL
  rebuilt from settings;
* the retired slate predictor's pre-annotations are not seeded (its rows were
  removed in prod on 2026-08-03; v1 read an empty table): a task carries no
  predictions;
* the supersede pass and the publish check are the store's, given this run's
  candidates and the project's rows as they stood before the import.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from temporalio.testing import ActivityEnvironment

from fishsense_services_api.slate_store import SlatePopulateCapture
from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_orchestrator.object_store.contracts import StagingTarget
from fishsense_services_orchestrator.slates import populate as sut
from fishsense_services_orchestrator.slates.contracts import PopulateSlateProject

TENANT = uuid.UUID(int=1)
DIVE = uuid.UUID(int=42)
PROJECT = 66
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _capture(n: int, *, checksum=None, from_v1=False) -> SlatePopulateCapture:
    return SlatePopulateCapture(
        capture_id=uuid.UUID(int=100 + n),
        number=n,
        checksum=checksum or f"{n:032x}",
        from_v1=from_v1,
        captured_at=T0,
    )


def _jpeg(capture: SlatePopulateCapture) -> ObjectRef:
    return ObjectRef(
        bucket="labels",
        key=f"tenants/{TENANT}/preprocess_slate_images_jpeg/{capture.checksum}.JPG",
    )


class FakeCatalog:
    def __init__(self, candidates, *, had_rows=False):
        self.candidates = candidates
        self.had_rows = had_rows
        self.recorded = []
        self.superseded_with = None

    async def slate_populate_candidates(self, tenant_id, dive_id):
        assert (tenant_id, dive_id) == (TENANT, DIVE)
        return self.candidates

    async def dive_has_slate_labels_in_project(self, tenant_id, dive_id, project):
        assert project == PROJECT
        return self.had_rows

    async def record_slate_label(
        self, tenant_id, capture_id, *, ls_project_id, ls_task_id, image_url
    ):
        self.recorded.append((capture_id, ls_project_id, ls_task_id, image_url))

    async def supersede_stale_slate_labels(
        self, tenant_id, dive_id, *, ls_project_id, keep_capture_ids
    ):
        self.superseded_with = (ls_project_id, list(keep_capture_ids))
        return 0


class FakeStore:
    def __init__(self, present=None):
        #: checksums whose JPEG is written; None: all of them.
        self.present = present

    async def locate_processed_jpeg(self, tenant_id, folder, checksum, *, from_v1):
        assert folder == "preprocess_slate_images_jpeg"
        if self.present is not None and checksum not in self.present:
            return None
        return ObjectRef(
            bucket="labels",
            key=f"tenants/{tenant_id}/{folder}/{checksum}.JPG",
        )


class FakeLabelStudio:
    """The three calls populate makes: list a project's task URLs, import,
    publish. Imported tasks are listable at once unless `visible` is False."""

    def __init__(self, *, visible=True):
        self.visible = visible
        self.tasks: list[tuple[int, str]] = []
        self.imported: list[dict] = []
        self.updates: list[tuple[int, dict]] = []
        self._next = 4001

    async def task_image_urls(self, project_id, *, beat):
        return list(self.tasks)

    async def import_tasks(self, project_id, tasks, *, beat):
        self.imported.extend(tasks)
        if self.visible:
            for task in tasks:
                self.tasks.append((self._next, task["data"]["image"]))
                self._next += 1

    async def update_project(self, project_id, **fields):
        self.updates.append((project_id, fields))


class FakeProjects:
    def __init__(self):
        self.calls = []

    async def ensure_dive_project(
        self, tenant_id, dive_id, kind, *, suffix, labeling_config_xml
    ):
        self.calls.append((tenant_id, dive_id, kind, suffix, labeling_config_xml))
        return PROJECT


def _activities(catalog, *, store=None, ls=None, projects=None):
    return sut.DiveSlateProjectActivities(
        catalog=catalog,
        label_projects=projects or FakeProjects(),
        label_studio=ls or FakeLabelStudio(),
        store=store or FakeStore(),
    )


async def _populate(activities) -> int:
    return await ActivityEnvironment().run(
        activities.populate_dive_slate_label_studio_project,
        PopulateSlateProject(tenant_id=TENANT, dive_id=DIVE, ls_project_id=PROJECT),
    )


# ---------- the task ----------


def test_build_task_emits_dual_image_and_img_keys():
    """Pinned: dual-key `image` + `img` for legacy labeling configs, and the
    capture-order fields; the URL is where the JPEG was located."""
    capture = _capture(7, checksum="abc123")

    task = sut.build_slate_task(capture, _jpeg(capture))

    expected = f"s3://labels/tenants/{TENANT}/preprocess_slate_images_jpeg/abc123.JPG"
    assert task["data"]["image"] == expected
    assert task["data"]["img"] == expected
    assert task["data"]["image_id"] == 7
    assert task["data"]["taken"] == T0.isoformat()
    assert not task["annotations"]
    assert not task["predictions"]


# ---------- populate ----------


async def test_imports_only_candidate_images():
    catalog = FakeCatalog([_capture(1), _capture(3)])
    ls = FakeLabelStudio()

    n = await _populate(_activities(catalog, ls=ls))

    assert n == 2
    assert {c for c, *_ in catalog.recorded} == {
        _capture(1).capture_id,
        _capture(3).capture_id,
    }
    assert all(p == PROJECT for _, p, _, _ in catalog.recorded)
    assert all("preprocess_slate_images_jpeg" in url for *_, url in catalog.recorded)
    assert sorted(t for _, _, t, _ in catalog.recorded) == [4001, 4002]


async def test_no_slate_marked_images_is_a_no_op():
    catalog = FakeCatalog([])
    ls = FakeLabelStudio()

    n = await _populate(_activities(catalog, ls=ls))

    assert n == 0
    assert ls.imported == []
    assert catalog.recorded == []


async def test_publishes_project_after_import():
    ls = FakeLabelStudio()

    await _populate(_activities(FakeCatalog([_capture(1), _capture(3)]), ls=ls))

    assert ls.updates == [(PROJECT, {"is_published": True})]


async def test_does_not_publish_empty_project():
    """No candidates and no existing rows -> stay a hidden draft."""
    ls = FakeLabelStudio()

    await _populate(_activities(FakeCatalog([]), ls=ls))

    assert ls.updates == []


async def test_publishes_a_project_that_already_holds_the_dives_rows():
    """v1: publish iff the project holds tasks -- this run's, or the rows it
    already had before the import."""
    ls = FakeLabelStudio()

    await _populate(_activities(FakeCatalog([], had_rows=True), ls=ls))

    assert ls.updates == [(PROJECT, {"is_published": True})]


async def test_an_import_not_yet_listable_leaves_the_project_a_draft():
    """Hosted Label Studio imports asynchronously; a project must not be shown
    half-populated while tasks are still materialising."""
    import fishsense_services_orchestrator.labels.populate as pu

    ls = FakeLabelStudio(visible=False)
    original = pu._IMPORT_VISIBILITY_ATTEMPTS  # pylint: disable=protected-access
    pu._IMPORT_VISIBILITY_ATTEMPTS = 0  # pylint: disable=protected-access
    try:
        n = await _populate(_activities(FakeCatalog([_capture(1)]), ls=ls))
    finally:
        pu._IMPORT_VISIBILITY_ATTEMPTS = original  # pylint: disable=protected-access

    assert n == 0
    assert ls.updates == []


async def test_defers_images_whose_jpeg_is_not_in_garage():
    """Never seed a task for an unrendered frame, or the labeler sees a
    missing image and the dive leaves the stage-9 cohort that would have
    rendered it (dive 84)."""
    catalog = FakeCatalog(
        [_capture(1, checksum="a" * 32), _capture(3, checksum="c" * 32)]
    )

    n = await _populate(_activities(catalog, store=FakeStore(present={"a" * 32})))

    assert n == 1
    assert [c for c, *_ in catalog.recorded] == [_capture(1).capture_id]


async def test_deferred_image_keeps_its_live_row_in_this_project():
    """A deferred JPEG means "not yet", not "no longer wanted": the supersede
    pass exempts every candidate, not only those that survived the gate."""
    candidates = [_capture(1, checksum="a" * 32), _capture(3, checksum="c" * 32)]
    catalog = FakeCatalog(candidates)

    await _populate(_activities(catalog, store=FakeStore(present={"a" * 32})))

    assert catalog.superseded_with == (
        PROJECT,
        [c.capture_id for c in candidates],
    )


async def test_a_migrated_frames_task_points_where_v1_wrote_its_jpeg():
    """The store locates v1's key for a migrated frame; the task must use it,
    or dedup-by-URL misses the task v1 created and duplicates it."""
    capture = _capture(5, checksum="e" * 32, from_v1=True)
    legacy = ObjectRef(
        bucket="labels",
        key=f"fishsense-lite/preprocess_slate_images_jpeg/{'e' * 32}.JPG",
    )

    class LegacyStore(FakeStore):
        async def locate_processed_jpeg(self, tenant_id, folder, checksum, *, from_v1):
            assert from_v1 is True
            return legacy

    ls = FakeLabelStudio()
    ls.tasks.append((777, legacy.uri))
    catalog = FakeCatalog([capture])

    n = await _populate(_activities(catalog, store=LegacyStore(), ls=ls))

    assert n == 1
    assert ls.imported == [], "already in the project: dedup, not a second task"
    assert catalog.recorded == [(capture.capture_id, PROJECT, 777, legacy.uri)]


# ---------- create ----------


async def test_create_finds_or_makes_the_dives_slate_project():
    projects = FakeProjects()

    project = await ActivityEnvironment().run(
        _activities(
            FakeCatalog([]), projects=projects
        ).create_dive_slate_label_studio_project,
        StagingTarget(tenant_id=TENANT, dive_id=DIVE),
    )

    assert project == PROJECT
    ((tenant, dive, kind, suffix, xml),) = projects.calls
    assert (tenant, dive, kind, suffix) == (
        TENANT,
        DIVE,
        "slate",
        "Dive Slate Labeling",
    )
    assert xml == sut.DIVE_SLATE_LABELING_CONFIG_XML


def test_the_labeling_config_carries_the_controls_the_sync_reads():
    """`reference_points` (keypoints), `slate` (rectangle) and
    `skipped_points` (text) map 1:1 onto the slate label's fields; the
    `upside_down` choice was removed on 2026-07-31 and must not come back."""
    xml = sut.DIVE_SLATE_LABELING_CONFIG_XML

    for control in (
        '<KeyPointLabels name="reference_points"',
        '<RectangleLabels name="slate"',
        '<TextArea name="skipped_points"',
        '<Image name="image" value="$image"',
    ):
        assert control in xml
    assert "upside_down" not in xml
