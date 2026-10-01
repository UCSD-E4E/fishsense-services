"""The head/tail populate, create and backfill activities.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/tests/
test_populate_headtail_label_studio_project_activity.py (all but the two
tests of helpers that moved: `_select_target_images` is the store's
candidates query, tested on Postgres, and `_build_task` is in
test_headtail_labeling.py; the case of a row with no id cannot arise in v2).
Names, bodies and reasons are v1's. The harness is v1's too, rebuilt: a fake
hosted Label Studio SDK under the real `LabelStudioClient` (imports are
asynchronous and listings serve presign resolve-wrappers, which is what the
dedup turns on), a fake catalog in place of v1's mocked API client, and the
real `OrchestratorObjectStore` over a fake S3 for the JPEG gate.

v1's two populate invariants:

* only images with a *valid* laser, not yet completed, and **already visited
  by the detector** are imported (prediction-gated: seeding first would drop an
  image from the predict cohort for good), and only once their JPEG exists;
* the supersede pass retires incomplete live rows the project no longer owns
  -- rows in another project, and rows here whose image stopped being a
  *candidate* -- and exempts (same project AND still a candidate), never
  "targets", which is what made dive 341 oscillate and erased project 285990
  from the landing page.

v2 changes pinned here: tasks point at the JPEG where the object store found
it (v1's key for a migrated frame, so its dedup URL is v1's); the backfill
lists and attaches through the one adapter; projects are the recorded ones.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.headtail_store import (
    CurrentHeadTailPrediction,
    HeadtailCandidate,
    LiveHeadTailLabel,
    PopulateCandidate,
    PopulateState,
)
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.headtail.activities import HeadtailTarget
from fishsense_services_orchestrator.headtail.labeling import (
    HEADTAIL_LABELING_CONFIG_XML,
    HEADTAIL_PROJECT_TITLE_SUFFIX,
)
from fishsense_services_orchestrator.headtail.populate import (
    HeadtailLabelActivities,
)
from fishsense_services_orchestrator.labels import populate as populate_utils
from fishsense_services_orchestrator.labels.label_studio import LabelStudioClient
from fishsense_services_orchestrator.object_store.layout import ObjectLayout
from fishsense_services_orchestrator.object_store.store import OrchestratorObjectStore

T0 = datetime(2026, 9, 1, tzinfo=UTC)
LAB, REEF = uuid.uuid4(), uuid.uuid4()
DIVE = uuid.uuid4()
TARGET = HeadtailTarget(LAB, DIVE)
PROJECT = 71
LAYOUT = ObjectLayout(
    ObjectStoreConnection(
        endpoint_url="https://s3.example.test", region="garage", access_key_id="k",
        secret_access_key="s", bucket="fishsense-lite",
        labels_bucket="labels-fishsense-lite", legacy_labels_prefix="fishsense-lite",
    )  # fmt: skip
)
FOLDER = "preprocess_headtail_jpeg"


@pytest.fixture(autouse=True)
def _no_visibility_wait(monkeypatch):
    monkeypatch.setattr(populate_utils, "_IMPORT_VISIBILITY_INTERVAL_S", 0)


# -- the fakes ------------------------------------------------------------------------


def _candidate(n, *, from_v1=False):
    return PopulateCandidate(
        capture_id=uuid.UUID(int=n), number=n, checksum=f"{n:032x}",
        from_v1=from_v1, captured_at=T0 + timedelta(seconds=n),
    )  # fmt: skip


def _cid(n):
    return uuid.UUID(int=n)


def _abstention(n):
    """Passes the prediction gate (the detector visited) while seeding no
    keypoints, so these tests are about import and supersede, not content."""
    return CurrentHeadTailPrediction(
        capture_id=_cid(n), status="no_detections", head_x=None, head_y=None,
        tail_x=None, tail_y=None, width=None, height=None, silhouette_ratio=None,
        rejected_low_confidence=False, predictor_version=2,
    )  # fmt: skip


def _predicted(n, version=2):
    return CurrentHeadTailPrediction(
        capture_id=_cid(n), status="predicted", head_x=100.0, head_y=150.0,
        tail_x=400.0, tail_y=150.0, width=4000, height=3000, silhouette_ratio=0.25,
        rejected_low_confidence=False, predictor_version=version,
    )  # fmt: skip


def _row(n, *, completed=False, project=PROJECT, task=None):
    return LiveHeadTailLabel(
        id=uuid.uuid4(), capture_id=_cid(n), ls_project_id=project,
        ls_task_id=task if task is not None else n * 11, completed=completed,
    )  # fmt: skip


class FakeCatalog:
    def __init__(self, candidates=(), labels=(), predictions=None, number=42):
        candidates = list(candidates)
        if predictions is None:
            predictions = [_abstention(c.number) for c in candidates]
        self.state = PopulateState(number, candidates, list(predictions), list(labels))
        self.recorded: list[tuple] = []
        self.superseded: list[uuid.UUID] = []

    async def member_tenants(self):
        return [LAB, REEF]

    async def dives_needing_headtail_population(self, tenant_id):
        return {
            LAB: [HeadtailCandidate(DIVE, T0 + timedelta(hours=1))],
            REEF: [HeadtailCandidate(_cid(9), T0)],
        }[tenant_id]

    async def headtail_populate_state(self, tenant_id, dive_id):
        assert (tenant_id, dive_id) == (LAB, DIVE)
        return self.state

    async def record_head_tail_task(self, tenant_id, capture_id, *, ls_project_id,
                                    ls_task_id):  # fmt: skip
        self.recorded.append((capture_id, ls_project_id, ls_task_id))

    async def supersede_head_tail_labels(self, tenant_id, dive_id, label_ids):
        self.superseded.extend(label_ids)
        return len(label_ids)

    def superseded_captures(self):
        by_id = {label.id: label for label in self.state.labels}
        return {by_id[i].capture_id for i in self.superseded}


class FakeLabelStudioSdk:
    """Hosted Label Studio: an import assigns ids but returns none; listings
    serve a presign resolve-wrapper, not the imported s3:// URI."""

    def __init__(self, task_ids=(), *, projects=None, predictions=None):
        self._ids = iter(task_ids)
        self.stored: list[SimpleNamespace] = []
        self.imports: list[list[dict]] = []
        self.updates: list[dict] = []
        self.created_predictions: list[dict] = []
        self.prediction_lists: list[int] = []
        self._projects = projects or {}
        self._predictions = predictions or {}
        self.projects = SimpleNamespace(
            import_tasks=self._import, update=self._update, get=self._get
        )
        self.tasks = SimpleNamespace(list=lambda project=None: list(self.stored))
        self.predictions = SimpleNamespace(
            list=self._list_predictions, create=self._create_prediction
        )

    def _import(self, project_id, request, return_task_ids=False):
        # pylint: disable=unused-argument
        self.imports.append(list(request))
        for task in request:
            task_id = next(self._ids)
            uri = task["data"]["image"]
            fileuri = base64.b64encode(uri.encode()).decode()
            self.stored.append(
                SimpleNamespace(
                    id=task_id,
                    data={"image": f"/tasks/{task_id}/resolve/?fileuri={fileuri}"},
                )
            )
        return SimpleNamespace(import_=1)

    def _update(self, id, **fields):  # pylint: disable=redefined-builtin
        self.updates.append({"id": id, **fields})

    def _get(self, id):  # pylint: disable=redefined-builtin
        return self._projects[id]

    def _list_predictions(self, project):
        self.prediction_lists.append(project)
        return self._predictions.get(project, [])

    def _create_prediction(self, task, model_version, result):
        self.created_predictions.append(
            {"task": task, "model_version": model_version, "result": result}
        )


class FakeS3:
    def __init__(self, present):
        self.present = present

    def head_object(self, Bucket, Key):  # pylint: disable=invalid-name
        if self.present is not None and (Bucket, Key) not in self.present:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}


def _jpeg(n):
    return LAYOUT.processed_jpeg(LAB, FOLDER, f"{n:032x}")


class FakeLabelProjects:
    def __init__(self):
        self.calls = []

    async def ensure_dive_project(self, tenant_id, dive_id, kind, *, suffix,
                                  labeling_config_xml):  # fmt: skip
        self.calls.append((tenant_id, dive_id, kind, suffix, labeling_config_xml))
        return PROJECT


def _activities(catalog, sdk, *, present=None, projects=None):
    """`present=None`: every JPEG is written (v1's default harness)."""
    keys = None if present is None else {(r.bucket, r.key) for r in present}
    return HeadtailLabelActivities(
        catalog=catalog,
        store=OrchestratorObjectStore(FakeS3(keys), LAYOUT),
        label_studio_factory=lambda: LabelStudioClient(sdk),
        label_projects_factory=lambda: projects or FakeLabelProjects(),
    )


async def _populate(catalog, sdk, **kwargs):
    return await ActivityEnvironment().run(
        _activities(catalog, sdk, **kwargs).populate_headtail_label_studio_project,
        TARGET,
        PROJECT,
    )


def _imported_uris(sdk):
    return [t["data"]["image"] for batch in sdk.imports for t in batch]


# -- v1's populate tests ------------------------------------------------------------------


async def test_imports_targets_and_supersedes_incomplete_old_rows():
    """Image 1's row is completed, so it is no candidate. Image 2 has an
    incomplete row IN THIS PROJECT -> re-imported (dedup finds nothing to add
    here, so it is re-recorded) and must NOT be superseded. Image 3 is fresh."""
    catalog = FakeCatalog(
        candidates=[_candidate(2), _candidate(3), _candidate(4)],
        labels=[_row(1, completed=True), _row(2)],
    )
    sdk = FakeLabelStudioSdk(task_ids=[3001, 3002, 3003])

    n = await _populate(catalog, sdk)

    assert n == 3
    assert {r[0] for r in catalog.recorded} == {_cid(2), _cid(3), _cid(4)}
    assert all(r[1] == PROJECT for r in catalog.recorded)
    assert not catalog.superseded, (
        "rows in the project being populated were just refreshed by the import; "
        "superseding them undoes this run's own work"
    )


async def test_no_valid_laser_targets_skips_import_but_still_supersedes():
    catalog = FakeCatalog(candidates=[], labels=[_row(1)])
    sdk = FakeLabelStudioSdk()

    assert await _populate(catalog, sdk) == 0
    assert not sdk.imports
    assert catalog.superseded_captures() == {_cid(1)}


async def test_publishes_project_after_import():
    catalog = FakeCatalog(candidates=[_candidate(1), _candidate(2)])
    sdk = FakeLabelStudioSdk(task_ids=[3001, 3002])

    await _populate(catalog, sdk)

    assert sdk.updates == [{"id": PROJECT, "is_published": True}]


async def test_does_not_publish_empty_project():
    catalog = FakeCatalog(candidates=[])
    sdk = FakeLabelStudioSdk()

    await _populate(catalog, sdk)

    assert not sdk.updates


async def test_a_project_whose_import_is_not_listable_yet_stays_a_draft(monkeypatch):
    """Hosted Label Studio imports asynchronously. Tasks not listable yet get
    no row this run, and the project is not shown half-populated; the next
    run reconciles them (re-importing is what duplicates)."""
    monkeypatch.setattr(populate_utils, "_IMPORT_VISIBILITY_ATTEMPTS", 1)
    catalog = FakeCatalog(candidates=[_candidate(1)], labels=[_row(2, completed=True)])
    sdk = FakeLabelStudioSdk(task_ids=[3001])
    sdk.tasks = SimpleNamespace(list=lambda project=None: [])  # still materialising

    assert await _populate(catalog, sdk) == 0
    assert len(sdk.imports) == 1
    assert not sdk.updates, "published while its task set was incomplete"


async def test_a_project_holding_only_earlier_rows_is_still_published():
    """v1 published iff the project held rows -- this run's or earlier ones."""
    catalog = FakeCatalog(candidates=[], labels=[_row(1, completed=True)])
    sdk = FakeLabelStudioSdk()

    await _populate(catalog, sdk)

    assert sdk.updates == [{"id": PROJECT, "is_published": True}]


async def test_running_twice_does_not_flip_rows_back_to_superseded():
    """The prod flip-flop (dive 341, 2026-08-04): the supersede pass must not
    retire the row this project's import refreshed."""
    catalog = FakeCatalog(candidates=[_candidate(1)], labels=[_row(1)])
    sdk = FakeLabelStudioSdk(task_ids=[4001])

    await _populate(catalog, sdk)

    assert (
        not catalog.superseded
    ), "a re-run must leave the pending row live, or the dive never drains"


async def test_stale_rows_for_non_target_images_are_still_superseded():
    """Image 2's laser is no longer valid: no candidate, so its incomplete
    row is genuinely stale."""
    catalog = FakeCatalog(candidates=[_candidate(1)], labels=[_row(1), _row(2)])
    sdk = FakeLabelStudioSdk(task_ids=[4002])

    await _populate(catalog, sdk)

    assert catalog.superseded_captures() == {_cid(2)}


async def test_defers_images_whose_jpeg_is_not_in_garage():
    """Never seed a task for an image stage 5.1 hasn't rendered: the labeler
    gets a missing image and the dive leaves the stage-5.1 cohort (prod dive
    84, 2026-08-04: 36 of 39 tasks pointed at nothing)."""
    catalog = FakeCatalog(candidates=[_candidate(1), _candidate(2)])
    sdk = FakeLabelStudioSdk(task_ids=[5001])

    n = await _populate(catalog, sdk, present=[_jpeg(1)])

    assert n == 1, "only the image with a rendered JPEG is seeded"
    assert [r[0] for r in catalog.recorded] == [_cid(1)]


async def test_deferred_image_keeps_its_live_row_in_this_project():
    """A deferral must not retire an existing live row (prod 2026-09-07:
    project 285990 erased from the landing page with its work outstanding)."""
    catalog = FakeCatalog(
        candidates=[_candidate(1), _candidate(2)], labels=[_row(1), _row(2)]
    )
    sdk = FakeLabelStudioSdk(task_ids=[6001])

    await _populate(catalog, sdk, present=[_jpeg(1)])

    assert _cid(2) not in catalog.superseded_captures()


async def test_an_unpredicted_image_keeps_its_live_row():
    """'The detector hasn't been here yet' is not 'this task is stale'."""
    catalog = FakeCatalog(
        candidates=[_candidate(1), _candidate(2)],
        labels=[_row(1), _row(2)],
        predictions=[_abstention(1)],
    )
    sdk = FakeLabelStudioSdk(task_ids=[6002])

    await _populate(catalog, sdk)

    assert _cid(2) not in catalog.superseded_captures()


async def test_all_images_deferred_does_not_wipe_the_project():
    catalog = FakeCatalog(
        candidates=[_candidate(1), _candidate(2)], labels=[_row(1), _row(2)]
    )
    sdk = FakeLabelStudioSdk()

    await _populate(catalog, sdk, present=[])

    assert not catalog.superseded, "deferred images dead-lettered every pending row"


async def test_legacy_other_project_rows_are_superseded_even_when_refreshed():
    """One image can hold both a legacy shared-project row and this project's;
    only the legacy one retires (else `headtail_labeling_complete` reads false
    forever)."""
    this, legacy = _row(1, project=PROJECT), _row(1, project=66, task=999)
    catalog = FakeCatalog(candidates=[_candidate(1)], labels=[this, legacy])
    sdk = FakeLabelStudioSdk(task_ids=[4003])

    await _populate(catalog, sdk)

    assert catalog.superseded == [legacy.id], "only the legacy-project row"


async def test_unpredicted_images_are_deferred():
    """Populating before the detector ran would starve the image of a
    prediction forever (the laser side's dive 84)."""
    catalog = FakeCatalog(
        candidates=[_candidate(1), _candidate(2)], predictions=[_abstention(2)]
    )
    sdk = FakeLabelStudioSdk(task_ids=[3001])

    n = await _populate(catalog, sdk)

    assert n == 1, "only the predicted image should be seeded"
    assert [r[0] for r in catalog.recorded] == [_cid(2)]


# -- v2 ------------------------------------------------------------------------------------


async def test_a_task_points_at_the_jpeg_where_the_store_found_it():
    """A migrated frame's JPEG is where v1 wrote it; its existing task holds
    that URL, so the dedup finds it rather than importing a twin."""
    migrated = _candidate(1, from_v1=True)
    v1_jpeg = LAYOUT.legacy_processed_jpeg(FOLDER, migrated.checksum)
    catalog = FakeCatalog(candidates=[migrated, _candidate(2)])
    sdk = FakeLabelStudioSdk(task_ids=[7001, 7002])

    await _populate(catalog, sdk, present=[v1_jpeg, _jpeg(2)])

    assert _imported_uris(sdk) == [v1_jpeg.uri, _jpeg(2).uri]


async def test_an_existing_task_is_recorded_not_imported_again():
    """v1's #343 dedup, through the foundation: a task already in the project
    (its presign wrapper decoded) is anchored, never duplicated."""
    catalog = FakeCatalog(candidates=[_candidate(1)])
    sdk = FakeLabelStudioSdk(task_ids=[8001])
    fileuri = base64.b64encode(_jpeg(1).uri.encode()).decode()
    sdk.stored.append(
        SimpleNamespace(
            id=555, data={"image": f"/tasks/555/resolve/?fileuri={fileuri}"}
        )
    )

    n = await _populate(catalog, sdk)

    assert n == 1 and not sdk.imports
    assert catalog.recorded == [(_cid(1), PROJECT, 555)]


async def test_a_predicted_image_is_imported_with_its_keypoints():
    """Populate seeds the pre-annotation inline, tagged with the row's tier."""
    catalog = FakeCatalog(candidates=[_candidate(1)], predictions=[_predicted(1, -1)])
    sdk = FakeLabelStudioSdk(task_ids=[9001])

    await _populate(catalog, sdk)

    ((task,),) = sdk.imports
    (prediction,) = task["predictions"]
    assert prediction["model_version"] == "v-1 crop=1800x1350"
    assert task["data"]["image_id"] == 1, "the capture's number: v1's image id"


# -- create ------------------------------------------------------------------------------


async def test_create_ensures_the_dives_head_tail_project():
    """v1's title `{name} #{dive} - HeadTail Labeling` and config; v2 records
    the project and looks there first (`LabelProjects`)."""
    projects = FakeLabelProjects()
    activities = _activities(FakeCatalog(), FakeLabelStudioSdk(), projects=projects)

    project_id = await ActivityEnvironment().run(
        activities.create_headtail_label_studio_project, TARGET
    )

    assert project_id == PROJECT
    assert projects.calls == [
        (LAB, DIVE, "head_tail", HEADTAIL_PROJECT_TITLE_SUFFIX,
         HEADTAIL_LABELING_CONFIG_XML)
    ]  # fmt: skip


# -- the populate cohort -------------------------------------------------------------------


async def test_every_dive_needing_population_is_listed_oldest_first_across_tenants():
    activities = _activities(FakeCatalog(), FakeLabelStudioSdk())

    targets = await ActivityEnvironment().run(
        activities.select_dives_needing_headtail_population
    )

    assert targets == [HeadtailTarget(REEF, _cid(9)), HeadtailTarget(LAB, DIVE)]


# -- the backfill ----------------------------------------------------------------------------


def _project(title, model_version=None):
    return SimpleNamespace(
        id=PROJECT, title=title, label_config=None, model_version=model_version
    )


async def _backfill(catalog, sdk):
    return await ActivityEnvironment().run(
        _activities(catalog, sdk).backfill_headtail_predictions_for_dive, TARGET
    )


async def test_backfill_attaches_placeable_predictions_to_incomplete_tasks():
    catalog = FakeCatalog(
        labels=[
            _row(1, task=901),
            _row(2, task=902),
            _row(3, task=903, completed=True),
        ],
        predictions=[_predicted(1), _abstention(2), _predicted(3)],
    )
    sdk = FakeLabelStudioSdk(
        projects={PROJECT: _project("Reef dive #42 - HeadTail Labeling")}
    )

    attached = await _backfill(catalog, sdk)

    assert attached == 1
    assert [p["task"] for p in sdk.created_predictions] == [901]
    assert sdk.created_predictions[0]["model_version"] == "v2 crop=1800x1350"
    assert sdk.updates == [
        {"id": PROJECT, "model_version": "v2 crop=1800x1350"}
    ], "without the project's model_version the attached prediction is invisible"


async def test_backfill_is_idempotent_on_task_and_tier():
    """A task already carrying this tier is skipped; the listing is once per
    project, not per task (3,147 tasks across 19 dives)."""
    catalog = FakeCatalog(
        labels=[_row(1, task=901), _row(2, task=902)],
        predictions=[_predicted(1), _predicted(2)],
    )
    already = [SimpleNamespace(task=901, model_version="v2 crop=1800x1350")]
    sdk = FakeLabelStudioSdk(
        projects={PROJECT: _project("#42 - HeadTail Labeling", "v2 crop=1800x1350")},
        predictions={PROJECT: already},
    )

    assert await _backfill(catalog, sdk) == 1
    assert [p["task"] for p in sdk.created_predictions] == [902]
    assert sdk.prediction_lists == [PROJECT]
    assert not sdk.updates, "already showing the majority tier"


async def test_an_older_tier_on_a_task_does_not_count_as_attached():
    """The upgrade: a fallback-tier prediction on the task must not hide the
    SAM 3.1 one."""
    catalog = FakeCatalog(labels=[_row(1, task=901)], predictions=[_predicted(1)])
    sdk = FakeLabelStudioSdk(
        projects={PROJECT: _project("#42 - HeadTail Labeling")},
        predictions={
            PROJECT: [SimpleNamespace(task=901, model_version="v-1 crop=1800x1350")]
        },
    )

    assert await _backfill(catalog, sdk) == 1


async def test_backfill_leaves_a_shared_project_alone():
    """Only the dive's own project (`#42` in its title) gets its
    model_version set: on a shared legacy project two dives would fight."""
    catalog = FakeCatalog(labels=[_row(1, task=901)], predictions=[_predicted(1)])
    sdk = FakeLabelStudioSdk(projects={PROJECT: _project("HeadTail canonical #420")})

    await _backfill(catalog, sdk)

    assert not sdk.updates


async def test_backfill_with_nothing_attachable_touches_no_label_studio():
    catalog = FakeCatalog(labels=[_row(1)], predictions=[_abstention(1)])
    sdk = FakeLabelStudioSdk()

    assert await _backfill(catalog, sdk) == 0
    assert not sdk.prediction_lists and not sdk.created_predictions
