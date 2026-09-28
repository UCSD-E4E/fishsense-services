"""The species label sync's activities: which projects, syncing one, and the
dive links it alone writes.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_sync_species_labels_activity.py (the activity half) and
get_species_label_studio_project_ids_activity. Test names, bodies and reasons
are v1's. v2 adaptations: projects are listed across every tenant the
orchestrator serves, each with its tenant; a task is applied through the
label-sync catalog (`apply_species_sync`, which answers with the label's
capture and dive, as v1's `images.get` did); the dive links go through the
species catalog, by name -> id maps. The concurrency cap and one heartbeat
per task are the shared `labels.sync.sync_label_studio_project`'s, pinned in
test_label_sync.py.

v2 change, pinned in the API (test_species_store.py): a link write expires a
standing calibration refusal by stamping the dive, where v1 nulled the
refusal columns.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from temporalio.testing import ActivityEnvironment

from fishsense_services_api.label_sync_store import SyncedLabel
from fishsense_services_contracts import taxonomy
from fishsense_services_orchestrator.labels.label_studio import LabelStudioTask
from fishsense_services_orchestrator.labels.sync import LabelProject
from fishsense_services_orchestrator.species.activities import SpeciesActivities

from ._species import TENANT, FakeSpeciesCatalog, FakeStore

REEF = uuid.uuid4()
DIVE_7 = uuid.UUID(int=7)
H_SLATE, V_SLATE_2, BOARD = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _task(task_id, *, annotations=(), annotators=(), is_labeled=False,
          updated_at="2026-05-01T00:00:00Z"):  # fmt: skip
    return LabelStudioTask.from_sdk(
        SimpleNamespace(
            id=task_id,
            annotators=list(annotators),
            annotations=list(annotations),
            is_labeled=is_labeled,
            updated_at=updated_at,
        )
    )


class FakeLabelStudio:
    def __init__(self, tasks):
        self.tasks = tasks

    async def project_exists(self, project_id):
        return True

    async def list_tasks(self, project_id):
        return self.tasks


class FakeSyncCatalog:
    """`fishsense_services_api.label_sync_store.LabelSyncCatalog`, in memory.
    `labels` maps a task id to the image (capture number) and dive it labels."""

    def __init__(self, labels=None):
        self.labels = dict(labels or {})
        self.applied = []
        self.advanced = []

    async def member_tenants(self):
        return [TENANT, REEF]

    async def label_studio_projects(self, tenant_id, kind):
        assert kind == "species"
        return {TENANT: [70], REEF: [57, 58]}[tenant_id]

    async def sync_cursor(self, tenant_id, kind, project_id):
        return None

    async def advance_sync_cursor(self, tenant_id, kind, project_id, at):
        self.advanced.append((tenant_id, kind, project_id))

    async def apply_species_sync(self, tenant_id, ls_task_id, sync):
        if ls_task_id not in self.labels:
            return None
        image, dive = self.labels[ls_task_id]
        self.applied.append((tenant_id, ls_task_id, sync))
        return SyncedLabel(uuid.UUID(int=image), dive)


def _species_catalog():
    return FakeSpeciesCatalog(
        slates={"H-Slate": H_SLATE, "V-Slate 2": V_SLATE_2},
        targets={"E4E Checkerboard": BOARD},
    )


async def _sync(tasks, labels, species_catalog=None):
    sync_catalog = FakeSyncCatalog(labels)
    species_catalog = species_catalog or _species_catalog()
    activities = SpeciesActivities(
        catalog=species_catalog,
        store=FakeStore(),
        sync_catalog=sync_catalog,
        label_studio_factory=lambda: FakeLabelStudio(list(tasks)),
    )
    await ActivityEnvironment().run(
        activities.sync_species_labels, LabelProject(TENANT, 70)
    )
    return sync_catalog, species_catalog


def _species_annotation(*paths):
    return {"result": [{"from_name": "species", "value": {"taxonomy": list(paths)}}]}


async def test_lists_every_served_tenants_species_projects():
    activities = SpeciesActivities(
        catalog=_species_catalog(), store=FakeStore(), sync_catalog=FakeSyncCatalog()
    )

    projects = await ActivityEnvironment().run(activities.species_label_projects)

    assert projects == [
        LabelProject(TENANT, 70),
        LabelProject(REEF, 57),
        LabelProject(REEF, 58),
    ]


async def test_skips_tasks_with_no_existing_label():
    sync_catalog, _ = await _sync([_task(i) for i in range(3)], labels={})

    assert sync_catalog.applied == []
    assert sync_catalog.advanced == [(TENANT, "species", 70)]


async def test_unmapped_annotator_does_not_crash_sync():
    """A task annotated by someone v2 doesn't know still syncs; the labeler id
    is recorded as Label Studio gives it (v2 keeps Label Studio ids)."""
    sync_catalog, _ = await _sync(
        [_task(1, annotators=[999])], labels={1: (11, DIVE_7)}
    )

    ((_, _, sync),) = sync_catalog.applied
    assert sync.ls_labeler_id == 999


async def test_writes_parsed_fields_when_annotation_present():
    annotation = {
        "result": [
            {"from_name": "grouping", "value": {"choices": ["Part of previous group"]}},
            {
                "from_name": "species",
                "value": {"taxonomy": [["Reef fish", "Yellowtail Snapper"]]},
            },
        ]
    }
    sync_catalog, _ = await _sync(
        [_task(101, annotations=[annotation])], labels={101: (42, DIVE_7)}
    )

    ((_, task_id, sync),) = sync_catalog.applied
    assert task_id == 101
    assert sync.grouping == "Part of previous group"
    assert sync.content_of_image == "Reef fish, Yellowtail Snapper"


# ----------------------- slate identification -------------------------


async def test_sync_sets_dive_slate_from_slate_choice():
    task = _task(
        101,
        annotations=[
            _species_annotation(["Slate", "Laser on slate"], ["Slate", "V-Slate 2"])
        ],
        is_labeled=True,
        updated_at="2026-05-02T10:00:00Z",
    )

    _, catalog = await _sync([task], labels={101: (42, DIVE_7)})

    # V-Slate 2 -> its template, image 42 -> dive 7.
    assert catalog.links == [("slate", DIVE_7, V_SLATE_2)]


async def test_sync_does_not_touch_dive_slate_without_a_slate_choice():
    task = _task(
        101,
        annotations=[_species_annotation(["Fish", "Hogfish"])],
        is_labeled=True,
    )

    _, catalog = await _sync([task], labels={101: (42, DIVE_7)})

    assert catalog.links == []


async def test_sync_ignores_incomplete_slate_choice():
    """A slate type on a not-yet-completed task must not set the slate."""
    task = _task(
        101, annotations=[_species_annotation(["Slate", "V-Slate 2"])], is_labeled=False
    )

    _, catalog = await _sync([task], labels={101: (42, DIVE_7)})

    assert catalog.links == []


async def test_sync_most_recent_completed_slate_choice_wins():
    t_old = _task(
        101,
        annotations=[_species_annotation(["Slate", "V-Slate 2"])],
        is_labeled=True,
        updated_at="2026-05-01T00:00:00Z",
    )
    t_new = _task(
        102,
        annotations=[_species_annotation(["Slate", "H-Slate"])],
        is_labeled=True,
        updated_at="2026-05-03T00:00:00Z",
    )

    _, catalog = await _sync(
        [t_old, t_new], labels={101: (41, DIVE_7), 102: (42, DIVE_7)}  # same dive
    )

    # H-Slate is the most-recent completed choice for dive 7.
    assert catalog.links == [("slate", DIVE_7, H_SLATE)]


async def test_a_label_filed_under_no_dive_votes_for_nothing():
    """v1 looked the image's dive up and skipped a vote with none."""
    task = _task(
        101, annotations=[_species_annotation(["Slate", "V-Slate 2"])], is_labeled=True
    )

    _, catalog = await _sync([task], labels={101: (42, None)})

    assert catalog.links == []


# --- the "slate not in list" sentinel --------------------------------------


async def test_sync_notes_the_dive_when_the_slate_is_not_in_the_list():
    task = _task(
        101,
        annotations=[
            _species_annotation(
                ["Slate", "Laser on slate"], ["Slate", taxonomy.SLATE_NOT_IN_LIST_LEAF]
            )
        ],
        is_labeled=True,
        updated_at="2026-08-27T10:00:00Z",
    )

    _, catalog = await _sync([task], labels={101: (42, DIVE_7)})

    # No slate assigned...
    assert catalog.links == []
    # ...and the reason recorded on the dive.
    assert taxonomy.SLATE_NOT_IN_LIST_LEAF.lower() in catalog.notes[DIVE_7].lower()


async def test_sync_does_not_clobber_an_existing_note():
    """`notes` is an operator field; an hourly sync must not overwrite it.
    (The catalog writes only an empty note: test_species_store.py.)"""
    catalog = _species_catalog()
    catalog.notes[DIVE_7] = "operator: re-shoot scheduled"
    task = _task(
        101,
        annotations=[_species_annotation(["Slate", taxonomy.SLATE_NOT_IN_LIST_LEAF])],
        is_labeled=True,
    )

    await _sync([task], labels={101: (42, DIVE_7)}, species_catalog=catalog)

    assert catalog.notes[DIVE_7] == "operator: re-shoot scheduled"


async def test_a_dive_identified_on_another_frame_is_not_noted():
    """A dive that got a real slate on some *other* frame is identified; only
    the ones left with no answer at all are noted."""
    unsure = _task(
        101,
        annotations=[_species_annotation(["Slate", taxonomy.SLATE_NOT_IN_LIST_LEAF])],
        is_labeled=True,
    )
    sure = _task(
        102, annotations=[_species_annotation(["Slate", "H-Slate"])], is_labeled=True
    )

    _, catalog = await _sync(
        [unsure, sure], labels={101: (41, DIVE_7), 102: (42, DIVE_7)}
    )

    assert catalog.links == [("slate", DIVE_7, H_SLATE)]
    assert DIVE_7 not in catalog.notes


# --------------- the calibration-target pass (checkerboard) ---------------


def _checkerboard_annotation(*, extra=()):
    return _species_annotation(["Calibration Targets", "E4E Checkerboard"], *extra)


async def test_sync_links_the_board_on_a_frame_also_marked_laser_on_slate():
    """End to end, in the shape the frames are actually being labeled."""
    task = _task(
        101,
        annotations=[_checkerboard_annotation(extra=[["Slate", "Laser on slate"]])],
        is_labeled=True,
    )

    _, catalog = await _sync([task], labels={101: (42, DIVE_7)})

    # And no slate template was invented from the marker.
    assert catalog.links == [("target", DIVE_7, BOARD)]


async def test_sync_sets_calibration_target_from_the_board_choice():
    task = _task(
        101,
        annotations=[_checkerboard_annotation()],
        is_labeled=True,
        updated_at="2026-05-02T10:00:00Z",
    )

    _, catalog = await _sync([task], labels={101: (42, DIVE_7)})

    # E4E Checkerboard -> its current target row, image 42 -> dive 7.
    assert catalog.links == [("target", DIVE_7, BOARD)]


async def test_sync_does_not_touch_the_link_without_a_board_choice():
    task = _task(
        101, annotations=[_species_annotation(["Fish", "Hogfish"])], is_labeled=True
    )

    _, catalog = await _sync([task], labels={101: (42, DIVE_7)})

    assert catalog.links == []


async def test_sync_ignores_an_incomplete_board_choice():
    """Only completed annotations vote — a labeler mid-task has not decided."""
    task = _task(101, annotations=[_checkerboard_annotation()], is_labeled=False)

    _, catalog = await _sync([task], labels={101: (42, DIVE_7)})

    assert catalog.links == []


async def test_the_slate_pass_and_the_target_pass_are_independent():
    """A frame showing both writes both links."""
    task = _task(
        101,
        annotations=[_checkerboard_annotation(extra=[["Slate", "V-Slate 2"]])],
        is_labeled=True,
    )

    _, catalog = await _sync([task], labels={101: (42, DIVE_7)})

    assert sorted(catalog.links) == sorted(
        [("slate", DIVE_7, V_SLATE_2), ("target", DIVE_7, BOARD)]
    )


async def test_nothing_is_read_for_the_links_when_nothing_completed():
    """v1 returned before its reference-data reads when no task completed."""

    class Counting(FakeSpeciesCatalog):
        reads = 0

        async def slate_templates_by_name(self, tenant_id):
            Counting.reads += 1
            return {}

    await _sync([_task(1)], labels={1: (1, DIVE_7)}, species_catalog=Counting())

    assert Counting.reads == 0
