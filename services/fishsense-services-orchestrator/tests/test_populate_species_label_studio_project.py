"""Species populate: which frames get a task, what the task carries, what is
superseded, and when the project is published.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_populate_species_label_studio_project_activity.py (18). Test names,
bodies and reasons are v1's. v2 adaptations: the valid-laser filter is the
catalog's query (tested on Postgres in the API's test_species_store.py), so
the candidates here are already laser-valid; frames are captures; the JPEG
gate is the object store's `locate_processed_jpeg`, whose answer is also the
task's image URL; and the Label Studio fake is the SDK under the real
`LabelStudioClient`.

v2 changes, pinned last: a task's `image_id` is the capture's `number` (v1's
image id for a migrated frame), a migrated frame's task points at v1's JPEG,
and the seeded row's `source` is `human`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import List
from unittest.mock import MagicMock

from temporalio.testing import ActivityEnvironment

from fishsense_services_api.species_store import SpeciesPopulationFacts
from fishsense_services_orchestrator.labels.label_studio import LabelStudioClient
from fishsense_services_orchestrator.labels.populate import TaskImage
from fishsense_services_orchestrator.species import populate as sut
from fishsense_services_orchestrator.species.activities import SpeciesActivities
from fishsense_services_orchestrator.species.contracts import SpeciesTarget

from ._species import (
    DIVE,
    LAYOUT,
    TENANT,
    FakeSpeciesCatalog,
    FakeStore,
    capture,
    checksum_of,
    image as _image,
    jpeg_ref,
    species,
    with_,
)

TARGET = SpeciesTarget(TENANT, DIVE)


def _species_label(n, *, completed, project=70, superseded=False):
    return species(n, completed=completed, project=project, superseded=superseded)


class FakeSdk:
    """Hosted Label Studio: an import creates tasks (ids from
    `returned_task_ids`), listable at once."""

    def __init__(self, returned_task_ids: List[int], existing=()):
        self._ids = iter(returned_task_ids)
        self.listed = [SimpleNamespace(id=i, data=d) for i, d in existing]
        self.imported: List[dict] = []
        self.projects = MagicMock(
            import_tasks=MagicMock(side_effect=self._import),
            update=MagicMock(),
        )
        self.tasks = MagicMock(list=MagicMock(side_effect=self._list))

    def _import(self, project_id, request, return_task_ids=False):
        # pylint: disable=unused-argument
        for task in request:
            self.imported.append(task)
            self.listed.append(SimpleNamespace(id=next(self._ids), data=task["data"]))

    def _list(self, project):  # pylint: disable=unused-argument
        return list(self.listed)


def _activities(catalog, sdk, store=None):
    return SpeciesActivities(
        catalog=catalog,
        store=store or FakeStore(),
        label_studio_factory=lambda: LabelStudioClient(sdk),
    )


async def _populate(candidates, existing, sdk, store=None):
    catalog = FakeSpeciesCatalog(
        population=SpeciesPopulationFacts(
            candidates=list(candidates), species_labels=list(existing)
        )
    )
    count = await ActivityEnvironment().run(
        _activities(catalog, sdk, store).populate_species_label_studio_project,
        TARGET,
        70,
    )
    return count, catalog


def _images(*names):
    return [_image(i, name) for i, name in enumerate(names, start=1)]


def test_select_targets_filters_by_valid_laser_and_drops_completed():
    """The candidates are the laser-valid frames (v1's matrix: 1 and 3)."""
    candidates = [_image(1, "a"), _image(3, "c")]
    existing = [_species_label(1, completed=True)]

    selected = sut.select_target_captures(candidates, existing, 70)

    assert [c.capture_id for c in selected] == [capture(3)]


def test_select_targets_drops_a_frame_with_a_completed_sentinel():
    """v1's rule, now shared with both cohorts and the stage-2 resolver: a
    completed sentinel is done work, so its frame is never tasked (and so the
    cohorts must not keep selecting its dive)."""
    candidates = [_image(1, "a"), _image(3, "c")]
    existing = [_species_label(1, completed=True, project=None)]

    selected = sut.select_target_captures(candidates, existing, 70)

    assert [c.capture_id for c in selected] == [capture(3)]


def test_select_targets_skips_images_already_in_this_project():
    """Idempotency filter: an image with a non-superseded species row for
    the *target* project is not re-selected, but one whose only row is in
    a different (stale) project still is."""
    candidates = [_image(1, "a"), _image(2, "b")]
    existing = [
        _species_label(1, completed=False, project=70),  # already in target -> skip
        _species_label(2, completed=False, project=99),  # stale old project -> keep
    ]

    selected = sut.select_target_captures(candidates, existing, 70)

    assert [c.capture_id for c in selected] == [capture(2)]


def test_build_task_uses_groups_jpeg_folder_and_dual_keys():
    """Pinned: dual-key `image` + `img` shape (legacy LS project XML uses
    both conventions; emitting one fails import_tasks)."""
    ref = jpeg_ref("abc")
    task = sut.build_species_task(TaskImage(number=7, image=ref, captured_at=None))

    assert task["data"]["image"] == ref.uri
    assert task["data"]["img"] == ref.uri
    assert "/preprocess_groups_jpeg/" in ref.uri
    assert not task["predictions"]
    assert not task["annotations"]


async def test_imports_targets_and_writes_new_labels():
    """Migration path onto a fresh project (70), with stale rows in an
    old project (99). Image 1 has a completed old row -> skip. Image 2
    has an incomplete stale-project row -> new task + the stale row is
    superseded. Image 3 is fresh -> new task. (v1's fourth case -- a row
    with no id -- cannot exist here: every row the catalog reads is stored.)"""
    existing = [
        _species_label(1, completed=True, project=99),
        _species_label(2, completed=False, project=99),
    ]
    sdk = FakeSdk([3001, 3002])

    n, catalog = await _populate(_images("a", "b", "c"), existing, sdk)

    assert n == 2
    assert {r[0] for r in catalog.recorded} == {capture(2), capture(3)}
    # Supersede pass retires the pre-existing incomplete row (image 2); the
    # completed row (image 1) is untouched.
    assert catalog.superseded == [existing[1].id]


async def test_no_valid_laser_targets_skips_import_but_supersedes_stale():
    """No laser-valid images -> no task import, but the supersede pass still
    retires a pre-existing incomplete species row in a stale project."""
    existing = [_species_label(1, completed=False, project=99)]
    sdk = FakeSdk([])

    n, catalog = await _populate([], existing, sdk)

    assert n == 0
    sdk.projects.import_tasks.assert_not_called()
    assert catalog.recorded == []
    assert catalog.superseded == [existing[0].id]


async def test_defers_images_whose_jpeg_is_not_in_garage():
    """JPEG gate: an image whose species JPEG isn't in Garage yet is not
    imported (deferred to a later run), so a scheduled populate never
    seeds a species row ahead of preprocess writing the JPEG — which
    would strand the image outside the preprocess cohort."""
    sdk = FakeSdk([9001])  # only image 1 imports
    store = FakeStore({checksum_of("aaa"): jpeg_ref("aaa")})  # bbb is missing

    n, catalog = await _populate(
        [_image(1, "aaa"), _image(2, "bbb")], [], sdk, store=store
    )

    assert n == 1
    assert [r[0] for r in catalog.recorded] == [capture(1)]  # image 2 deferred


async def test_rerun_is_idempotent_for_same_project():
    """Scheduling invariant: a re-run where every laser-valid image
    already has a non-superseded task row *for this project* imports
    nothing and supersedes nothing."""
    existing = [
        _species_label(1, completed=False, project=70),
        _species_label(2, completed=False, project=70),
    ]
    sdk = FakeSdk([])

    n, catalog = await _populate(_images("a", "b"), existing, sdk)

    assert n == 0
    sdk.projects.import_tasks.assert_not_called()
    # the project's own in-progress rows are left untouched (no supersede churn)
    assert catalog.superseded == []
    assert catalog.recorded == []


async def test_writes_label_with_image_url_and_groups_jpeg_folder():
    """The row's image_url is the task's URL -- so downstream sync can
    recover the JPEG from the row."""
    sdk = FakeSdk([5001])

    _, catalog = await _populate([_image(1, "abc")], [], sdk)

    ((capture_id, project, task, image_url),) = catalog.recorded
    assert (capture_id, project, task) == (capture(1), 70, 5001)
    assert "preprocess_groups_jpeg" in image_url
    assert checksum_of("abc") in image_url
    assert image_url == sdk.imported[0]["data"]["image"]


async def test_publishes_when_no_images_deferred():
    # All laser-valid images have their JPEG -> nothing deferred -> project
    # task set complete -> publish.
    sdk = FakeSdk([1, 2])

    await _populate(_images("a", "b"), [], sdk)

    sdk.projects.update.assert_called_once_with(id=70, is_published=True)


async def test_does_not_publish_when_an_image_is_deferred():
    # Image 2's JPEG isn't in Garage yet -> deferred -> project incomplete ->
    # stay a hidden draft even though image 1's task imported.
    sdk = FakeSdk([9001])
    store = FakeStore({checksum_of("aaa"): jpeg_ref("aaa")})

    await _populate([_image(1, "aaa"), _image(2, "bbb")], [], sdk, store=store)

    sdk.projects.update.assert_not_called()


async def test_does_not_publish_empty_project():
    # No laser-valid images and no existing rows -> nothing to task ->
    # stay a hidden draft.
    sdk = FakeSdk([])

    await _populate([], [], sdk)

    sdk.projects.update.assert_not_called()


async def test_publishes_a_complete_project_whose_tasks_were_all_imported_before():
    """v1's `already_in_project` half: nothing new to import, nothing
    deferred, and the project holds live rows -> it is published."""
    sdk = FakeSdk([])

    await _populate(_images("a"), [_species_label(1, completed=False)], sdk)

    sdk.projects.update.assert_called_once_with(id=70, is_published=True)


# --- pre-annotation from stored judgements ----------------------------------


def _judgement(n: int, content: str, **extra):
    """A sentinel row (no project) carrying a species judgement — what a bulk
    import of hand-labelled work looks like."""
    return species(n, project=None, content_of_image=content, **extra)


def test_build_task_carries_a_pre_annotation_when_a_judgement_exists():
    judgement = _judgement(
        7, "Fish, Hogfish (Lachnolaimus maximus)", fish_measurable_category="no"
    )
    task = sut.build_species_task(
        TaskImage(number=7, image=jpeg_ref("abc"), captured_at=None), judgement
    )

    assert len(task["predictions"]) == 1
    result = task["predictions"][0]["result"]
    assert {r["from_name"] for r in result} == {"species", "measurable"}
    # It must never land in `annotations`: that would read as completed human
    # work and the sync activity would write it back as a labeler's answer.
    assert not task["annotations"]


def test_sentinel_judgements_ignores_rows_belonging_to_a_real_project():
    """A row with a project id is a labeler's own row, not an import. Feeding it
    back as a prediction would show a labeler their own answer as a suggestion."""
    labels = [
        species(5, project=70, content_of_image="Fish, Hogfish (Lachnolaimus maximus)")
    ]
    assert not sut.sentinel_judgements(labels)


def test_sentinel_judgements_ignores_a_sentinel_that_says_nothing():
    """Prod carries ~2,000 legacy NULL-project sentinels with no species set.
    They must stay inert rather than producing empty predictions."""
    assert not sut.sentinel_judgements([species(5, project=None)])


def test_sentinel_judgements_keys_by_image():
    labels = [
        _judgement(5, "Fish, Hogfish (Lachnolaimus maximus)"),
        _judgement(6, "Fish, Grey Snapper (Lutjanus griseus)"),
        species(7, project=None),
    ]
    found = sut.sentinel_judgements(labels)
    assert set(found) == {capture(5), capture(6)}
    assert found[capture(6)].content_of_image == "Fish, Grey Snapper (Lutjanus griseus)"


def test_a_stored_judgement_does_not_take_the_image_out_of_population():
    """The property that makes the whole approach safe: only a *non-sentinel*
    row drops an image, so a judgement leaves its frame in the labelling
    flow."""
    judgements = [_judgement(5, "Fish, Hogfish (Lachnolaimus maximus)")]

    selected = sut.select_target_captures([_image(5, "abc")], judgements, 70)

    assert [c.capture_id for c in selected] == [capture(5)]


async def test_the_supersede_pass_leaves_judgement_sentinels_alone():
    """A sentinel's NULL project is not equal to the target, so v1's pass fell
    through and dead-lettered the judgement the very run that read it."""
    judgement = _judgement(1, "Fish, Hogfish (Lachnolaimus maximus)")
    sdk = FakeSdk([3001])

    await_result = await _populate([_image(1, "a")], [judgement], sdk)
    catalog = await_result[1]

    assert catalog.superseded == [], "a judgement sentinel was dead-lettered"
    # And it did its job: the image was still populated, carrying the
    # pre-annotation.
    assert [r[0] for r in catalog.recorded] == [capture(1)]
    assert sdk.imported[0]["predictions"][0]["model_version"].startswith("species")


def test_a_completed_judgement_sentinel_is_refused_rather_than_honoured():
    """`completed` on a sentinel takes its image out of population for good
    (the completed-id set has no project filter), so it is not treated as a
    judgement."""
    completed = with_(
        _judgement(5, "Fish, Hogfish (Lachnolaimus maximus)"), completed=True
    )

    assert not sut.sentinel_judgements([completed])


# --- v2 --------------------------------------------------------------------------------


async def test_a_task_carries_the_captures_number_and_time_in_capture_order():
    """Label Studio fixes task order at import; `taken` and `image_id` let a
    project be sorted back into capture order. `image_id` is the capture's
    number -- v1's image id for a migrated frame."""
    sdk = FakeSdk([11, 12])

    await _populate([_image(1, "a", number=4001), _image(2, "b", number=4002)], [], sdk)

    assert [t["data"]["image_id"] for t in sdk.imported] == [4001, 4002]
    assert sdk.imported[0]["data"]["taken"].startswith("2025-01-01T00:00:01")


async def test_a_migrated_frames_task_points_at_v1s_jpeg():
    """The URL is wherever the JPEG was located, never rebuilt: for a migrated
    frame, v1's key -- which its existing task already holds, so the import
    dedup finds that task instead of making a second."""
    legacy = LAYOUT.legacy_processed_jpeg("preprocess_groups_jpeg", checksum_of("a"))
    store = FakeStore({checksum_of("a"): legacy})
    sdk = FakeSdk([], existing=[(777, {"image": legacy.uri})])

    n, catalog = await _populate([_image(1, "a", from_v1=True)], [], sdk, store=store)

    sdk.projects.import_tasks.assert_not_called()
    assert n == 1
    assert catalog.recorded == [(capture(1), 70, 777, legacy.uri)]
    assert store.located == [(TENANT, "preprocess_groups_jpeg", checksum_of("a"), True)]
