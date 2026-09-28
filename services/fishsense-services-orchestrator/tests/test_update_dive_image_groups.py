"""Stage 6.1: species labels regroup the prediction clusters into the
label-studio clusters stage 14 measures.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_update_dive_image_groups_activity.py (15). Test names, bodies and
reasons are v1's. v2 adaptations: frames are captures (v1's image ids become
`capture(n)`); the catalog answers the reads in one call and persists the
groups itself; the tie between two real rows breaks on the higher `number`
(v1: the higher id -- the most recently written).

v2 changes, pinned last: the groups are handed to the catalog in one call,
which writes all or nothing (v1 posted cluster by cluster, so a failure left a
partial set that blocked every re-run); and a race that grouped the dive
between the read and the write reports the skip.
"""

from __future__ import annotations

from temporalio.testing import ActivityEnvironment

from fishsense_services_api.species_store import SpeciesGroupingFacts
from fishsense_services_orchestrator.species import grouping as sut
from fishsense_services_orchestrator.species.activities import SpeciesActivities
from fishsense_services_orchestrator.species.contracts import SpeciesTarget

from ._species import (
    DIVE,
    TENANT,
    FakeSpeciesCatalog,
    FakeStore,
    capture,
    species,
    with_,
)

CONTINUE = "Part of previous group"
BREAK = "Not part of current group"


def _label(n: int, *, grouping: str | None = None):
    return species(n, completed=True, grouping=grouping)


def _clusters(*clusters):
    return [[capture(n) for n in cluster] for cluster in clusters]


def _by_capture(*labels):
    return {label.capture_id: label for label in labels}


def _ids(groups):
    return [[c.int for c in group] for group in groups]


# ----------------------------- pure regrouping ----------------------------


def test_regroup_first_label_of_each_cluster_starts_new_group_by_default():
    clusters = _clusters([101, 101, 102], [103, 104])
    labels = _by_capture(_label(101), _label(102), _label(103), _label(104))

    groups = sut.regroup_by_species_labels(clusters, labels)

    # First cluster's first label opens group A; second cluster's first
    # label opens group B (no continuity marker).
    assert _ids(groups) == [[101, 101, 102], [103, 104]]


def test_regroup_part_of_previous_group_continues_across_cluster_boundary():
    clusters = _clusters([101, 102], [103, 104])
    labels = _by_capture(
        _label(101), _label(102), _label(103, grouping=CONTINUE), _label(104)
    )

    groups = sut.regroup_by_species_labels(clusters, labels)

    # Cluster 2's first label continues into the previous group; 104
    # then continues too (idx != 0, default grouping).
    assert _ids(groups) == [[101, 102, 103, 104]]


def test_regroup_not_part_of_current_group_breaks_mid_cluster():
    clusters = _clusters([101, 102, 103, 104])
    labels = _by_capture(
        _label(101), _label(102), _label(103, grouping=BREAK), _label(104)
    )

    groups = sut.regroup_by_species_labels(clusters, labels)

    # 103 flushes [101, 102] and starts a new group containing itself
    # and 104.
    assert _ids(groups) == [[101, 102], [103, 104]]


def test_regroup_skips_image_ids_without_a_species_label():
    clusters = _clusters([101, 102, 103])
    labels = _by_capture(_label(101), _label(103))

    groups = sut.regroup_by_species_labels(clusters, labels)

    # 102 has no label entry — quietly skipped, doesn't open a group.
    assert _ids(groups) == [[101, 103]]


def test_regroup_empty_inputs():
    assert not sut.regroup_by_species_labels([], {})
    assert not sut.regroup_by_species_labels(_clusters([101]), {})


def test_regroup_first_cluster_starts_with_part_of_previous_does_not_open_extra_group():
    # Edge: dive starts with "Part of previous group" — there is no
    # previous group, but the marker means "don't insert a boundary."
    clusters = _clusters([101, 102])
    labels = _by_capture(_label(101, grouping=CONTINUE), _label(102))

    groups = sut.regroup_by_species_labels(clusters, labels)

    assert _ids(groups) == [[101, 102]]


# ------------------------------- activity --------------------------------


async def _run(grouping):
    catalog = FakeSpeciesCatalog(grouping=grouping)
    activities = SpeciesActivities(catalog=catalog, store=FakeStore())
    result = await ActivityEnvironment().run(
        activities.update_dive_image_groups, SpeciesTarget(TENANT, DIVE)
    )
    return result, catalog


async def test_activity_skips_when_label_studio_clusters_already_exist():
    result, catalog = await _run(
        SpeciesGroupingFacts(
            already_grouped=True,
            prediction_clusters=_clusters([101, 102]),
            species_labels=[_label(101), _label(102)],
        )
    )

    assert result.skipped_already_grouped is True
    assert result.new_clusters_created == 0
    assert catalog.persisted == []


async def test_activity_creates_one_cluster_per_group():
    result, catalog = await _run(
        SpeciesGroupingFacts(
            already_grouped=False,
            prediction_clusters=_clusters([101, 102], [103, 104]),
            species_labels=[
                _label(101),
                _label(102),
                _label(103, grouping=CONTINUE),
                _label(104, grouping=BREAK),
            ],
        )
    )

    # Expected groups:
    # - [101, 102, 103]  (103 continues previous group)
    # - [104]            (104 explicitly breaks into a new group)
    assert result.skipped_already_grouped is False
    assert result.new_clusters_created == 2
    assert result.species_labels_seen == 4
    ((dive, groups),) = catalog.persisted
    assert dive == DIVE
    assert _ids(groups) == [[101, 102, 103], [104]]


async def test_activity_no_groups_is_not_a_skip():
    # No prediction clusters → nothing to group, but this is "no work
    # possible" not "already done."
    result, catalog = await _run(
        SpeciesGroupingFacts(
            already_grouped=False, prediction_clusters=[], species_labels=[]
        )
    )

    assert result.skipped_already_grouped is False
    assert result.new_clusters_created == 0
    assert catalog.persisted == []


# --- sentinel species rows must not drive grouping --------------------------
#
# A species row with no Label Studio project is a SENTINEL: not a labeler's
# answer. Prod dive 5 (2026-09-16): image 1399's only row was an imported
# sentinel with `grouping = NULL`; 1399 LEADS a prediction cluster, so a new
# group started there and ONE hogfish became TWO Fish rows.


def _sentinel(n: int, *, grouping: str | None = None):
    """An imported judgement: carries a species, belongs to no LS project."""
    return species(
        n,
        project=None,
        grouping=grouping,
        content_of_image="Fish, Hogfish (Lachnolaimus maximus)",
    )


def test_a_frame_whose_only_species_row_is_a_sentinel_is_not_grouped():
    """No human judged that frame, so it must not join a measurement cluster
    and must not start one either."""
    assert not sut.select_species_label_per_image([_sentinel(5)])


def test_a_sentinel_never_overrides_a_real_answer_whatever_the_order():
    real = _label(5, grouping=CONTINUE)
    sentinel = _sentinel(5)
    for order in ([real, sentinel], [sentinel, real]):
        chosen = sut.select_species_label_per_image(order)
        assert chosen[capture(5)].ls_project_id == 70
        assert chosen[capture(5)].grouping == CONTINUE


def test_a_superseded_row_is_ignored():
    """`superseded` is the dead-letter for every label kind; a dead-lettered
    answer must not decide a grouping."""
    dead = with_(_label(5, grouping=CONTINUE), superseded=True)
    assert not sut.select_species_label_per_image([dead])


def test_a_live_row_wins_over_a_superseded_one():
    dead = with_(_label(5, grouping=BREAK), superseded=True)
    live = _label(5, grouping=CONTINUE)
    for order in ([dead, live], [live, dead]):
        chosen = sut.select_species_label_per_image(order)
        assert chosen[capture(5)].grouping == CONTINUE


def test_the_choice_among_several_real_rows_is_deterministic():
    """A frame can carry rows in two real projects (the per-dive project plus a
    grandfathered one). The highest number -- the most recently written -- is
    a stated rule instead of API row order (v1: the highest id)."""
    older = with_(_label(5, grouping=BREAK), number=100, ls_project_id=70)
    newer = with_(_label(5, grouping=CONTINUE), number=200, ls_project_id=99)
    for order in ([older, newer], [newer, older]):
        chosen = sut.select_species_label_per_image(order)
        assert chosen[capture(5)].number == 200


def test_the_prod_dive_5_shape_yields_one_group_not_two():
    """The regression this exists for, end to end through the real regrouper."""
    clusters = _clusters(
        [1393, 1394],
        [1395, 1396],
        [1397, 1398],
        [1399, 1400],
        [1401, 1402],
        [1403, 1404],
        [1405, 1406, 1407],
        [1408, 1409],
    )
    cont = CONTINUE
    labels = [
        _label(1393), _label(1394),
        _label(1395, grouping=cont), _label(1396),
        _label(1397, grouping=cont), _label(1398),
        _sentinel(1399),                      # <- no human answer for this frame
        _label(1400, grouping=cont),
        _label(1401, grouping=cont), _label(1402),
        _label(1403, grouping=cont), _label(1404),
        _label(1405, grouping=cont), _label(1406), _label(1407),
        _label(1408, grouping=cont), _label(1409),
    ]  # fmt: skip

    groups = sut.regroup_by_species_labels(
        clusters, sut.select_species_label_per_image(labels)
    )

    assert len(groups) == 1, f"one hogfish must be one group, got {groups}"
    assert capture(1399) not in groups[0], "an unjudged frame must not be measured"
    assert len(groups[0]) == 16


# --- v2 --------------------------------------------------------------------------


async def test_a_dive_grouped_since_the_read_is_reported_as_a_skip():
    """The catalog re-checks under its lock and writes nothing when another
    run grouped the dive first; the activity says so rather than claiming
    the clusters it didn't write."""

    class Racing(FakeSpeciesCatalog):
        async def persist_label_studio_clusters(self, tenant_id, dive_id, groups):
            return None

    catalog = Racing(
        grouping=SpeciesGroupingFacts(
            already_grouped=False,
            prediction_clusters=_clusters([101]),
            species_labels=[_label(101)],
        )
    )
    result = await ActivityEnvironment().run(
        SpeciesActivities(catalog=catalog, store=FakeStore()).update_dive_image_groups,
        SpeciesTarget(TENANT, DIVE),
    )

    assert result.skipped_already_grouped is True
    assert result.new_clusters_created == 0
