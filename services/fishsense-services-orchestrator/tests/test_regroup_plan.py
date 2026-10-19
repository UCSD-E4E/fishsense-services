"""Planning a regroup: stage 6.1's rule in capture order, and which fish each
new cluster is.

New in v2 (2026-10-14). The three field animals v1 grouped wrongly, in
miniature (frame and fish numbers are prod's, dropping the common prefix):

* dive 341: frame 101302 says "Part of previous group"; the frame before it
  is Hogfish 307's, but v1's unordered walk put it with Stoplight Parrotfish
  305 half an hour later;
* dive 465: frame 129276 says "Part of previous group" after 129275, but v1
  made them animals 330 and 331;
* dive 383: frame 111926 (a Nassau Grouper) says "Not part of current group"
  and 111927 (the Hogfish again) says the same once relabelled; v1 bound both
  to animal 323, a Hogfish.

What the plan promises:

* the clusters are exactly `regroup_by_species_labels` over the prediction
  clusters in capture order -- the planner adds no grouping rule of its own;
* a fish is kept by the new cluster holding most of its measured frames
  (ties: the cluster whose labelled species is the fish's, then the earlier);
  each fish goes to at most one cluster;
* a cluster with measured frames and no fish left gets a new fish of the
  species its first measured frame is labelled; one with none is unbound;
* every measured frame whose fish changes is listed;
* the plan has a digest that changes with the clusters it was made from, so
  an apply can insist on the plan that was reviewed.
"""

from __future__ import annotations

import uuid

from fishsense_services_api.regroup_store import (
    FishInfo,
    LabelStudioCluster,
    MeasuredCapture,
    NewCluster,
    RegroupFacts,
    clusters_fingerprint,
)
from fishsense_services_api.species_store import SpeciesLabelRow
from fishsense_services_orchestrator.ops.regroup.plan import plan_regroup

HOG = "Fish, Hogfish (Lachnolaimus maximus)"
PARROT = "Fish, Stoplight Parrotfish (Sparisoma viride)"
GROUPER = "Fish, Nassau Grouper (Epinephelus striatus)"
SCIENTIFIC = {HOG: "Lachnolaimus maximus", PARROT: "Sparisoma viride",
              GROUPER: "Epinephelus striatus"}  # fmt: skip
CONTINUE, BREAK = "Part of previous group", "Not part of current group"


def cap(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def fish(n: int) -> uuid.UUID:
    return uuid.UUID(int=1_000_000 + n)


def _label(n, content, grouping=None, *, project=1):
    return SpeciesLabelRow(
        id=uuid.UUID(int=2_000_000 + n), number=n, capture_id=cap(n),
        ls_project_id=project, ls_task_id=n, completed=True, superseded=False,
        needs_reprocess=False, grouping=grouping, top_three_photos_of_group=True,
        content_of_image=content, fish_measurable_category=None,
        fish_angle_category=None, fish_curved_category=None,
    )  # fmt: skip


def _facts(prediction, labels, measured, current, *, priority="high",
           species=None, duplicates=()):  # fmt: skip
    """`measured`: {frame: (fish number, the fish's species content)};
    `current`: [(fish number or None, [frames])], later clusters numbered
    higher; `species`: {fish number: content} for fish no measurement names;
    `duplicates`: frames that are not canonical."""
    clusters = [
        LabelStudioCluster(
            uuid.UUID(int=3_000_000 + i), i, None if f is None else fish(f),
            tuple(cap(n) for n in members),
        )
        for i, (f, members) in enumerate(current)
    ]  # fmt: skip
    frames = {n for c in prediction for n in c}
    contents = {f: content for f, content in measured.values()} | dict(species or {})
    return RegroupFacts(
        priority=priority,
        prediction_clusters=[[cap(n) for n in c] for c in prediction],
        species_labels=[_label(*spec) for spec in labels],
        label_studio_clusters=clusters,
        measured=[
            MeasuredCapture(cap(n), n, fish(f), f, SCIENTIFIC[content], 0.2)
            for n, (f, content) in sorted(measured.items())
        ],
        fingerprint=clusters_fingerprint(clusters),
        capture_numbers={cap(n): n for n in frames},
        canonical=frozenset(cap(n) for n in frames - set(duplicates)),
        fish={fish(f): FishInfo(f, SCIENTIFIC[c]) for f, c in contents.items()},
    )


def _mission_341(current=((307, [300, 301]), (305, [302, 303, 434, 437])), stray=305):
    return _facts(
        prediction=[[300, 301], [302, 303], [434, 437]],
        labels=[(300, HOG), (301, HOG), (302, HOG, CONTINUE), (303, HOG),
                (434, PARROT), (437, PARROT)],
        measured={301: (307, HOG), 302: (stray, PARROT if stray == 305 else HOG),
                  434: (305, PARROT), 437: (305, PARROT)},
        current=list(current),
    )  # fmt: skip


def test_mission_341_the_stray_hogfish_frame_rejoins_hogfish_307():
    plan = plan_regroup(_mission_341())

    assert plan.clusters == [
        NewCluster([cap(300), cap(301), cap(302), cap(303)], fish_id=fish(307)),
        NewCluster([cap(434), cap(437)], fish_id=fish(305)),
    ]
    assert [(m.capture_number, m.from_fish, m.to_fish) for m in plan.moves] == [
        (302, 305, 307)
    ]
    assert not plan.noop


def test_mission_465_one_parrotfish_not_two():
    plan = plan_regroup(
        _facts(
            prediction=[[274, 275], [276, 277, 278]],
            labels=[(274, PARROT), (275, PARROT), (276, PARROT, CONTINUE),
                    (277, PARROT), (278, PARROT)],
            measured={275: (330, PARROT), 276: (331, PARROT), 278: (331, PARROT)},
            current=[(330, [274, 275]), (331, [276, 277, 278])],
        )
    )  # fmt: skip

    assert plan.clusters == [
        NewCluster([cap(n) for n in (274, 275, 276, 277, 278)], fish_id=fish(331))
    ]
    assert [(m.capture_number, m.from_fish, m.to_fish) for m in plan.moves] == [
        (275, 330, 331)
    ]


def test_mission_383_the_grouper_gets_an_animal_of_its_own():
    plan = plan_regroup(
        _facts(
            prediction=[[922, 923, 924, 925, 926, 927]],
            labels=[(922, HOG), (923, HOG), (924, HOG), (925, HOG),
                    (926, GROUPER, BREAK), (927, HOG, BREAK)],
            measured={923: (322, HOG), 926: (323, HOG), 927: (323, HOG)},
            current=[(322, [922, 923, 924, 925]), (323, [926, 927])],
        )
    )  # fmt: skip

    assert plan.clusters == [
        NewCluster([cap(n) for n in (922, 923, 924, 925)], fish_id=fish(322)),
        NewCluster(
            [cap(926)], new_fish_names=("Nassau Grouper", "Epinephelus striatus")
        ),
        NewCluster([cap(927)], fish_id=fish(323)),
    ]
    assert [(m.capture_number, m.from_fish, m.to_fish, m.to_species)
            for m in plan.moves] == [(926, 323, None, "Epinephelus striatus")]  # fmt: skip


def test_a_cluster_with_no_measured_frame_is_unbound():
    plan = plan_regroup(
        _facts(
            prediction=[[1, 2], [3]],
            labels=[(1, HOG), (2, HOG), (3, PARROT)],
            measured={1: (7, HOG)},
            current=[(7, [1, 2])],
        )
    )

    assert plan.clusters == [
        NewCluster([cap(1), cap(2)], fish_id=fish(7)),
        NewCluster([cap(3)]),
    ]
    assert plan.moves == []


def test_a_mission_already_grouped_this_way_is_a_noop():
    plan = plan_regroup(
        _mission_341(
            current=((307, [300, 301, 302, 303]), (305, [434, 437])), stray=307
        )
    )

    assert plan.noop
    assert plan.moves == []


def test_the_digest_is_stable_and_follows_the_clusters_planned_from():
    assert plan_regroup(_mission_341()).sha256 == plan_regroup(_mission_341()).sha256
    moved = _mission_341(current=((307, [300, 301, 302]), (305, [303, 434, 437])))
    assert plan_regroup(moved).sha256 != plan_regroup(_mission_341()).sha256


def test_a_mission_stage_14_will_not_measure_is_flagged():
    plan = plan_regroup(
        _facts(
            prediction=[[1]], labels=[(1, HOG)], measured={1: (7, HOG)},
            current=[(7, [1])], priority="low",
        )
    )  # fmt: skip

    assert any("priority" in w for w in plan.warnings)


# -- review findings (PR #72) ---------------------------------------------------


def test_a_cluster_bound_since_the_apply_keeps_its_fish_before_stage_14():
    """After an apply, the moved frame's old measurement is a stale binding,
    so it is not current: the frame's vote is its cluster's fish, and a
    re-plan is a no-op rather than an unbind."""
    facts = _facts(
        prediction=[[300, 301], [302, 303], [434, 437]],
        labels=[(300, HOG), (301, HOG), (302, HOG, CONTINUE), (303, HOG),
                (434, PARROT), (437, PARROT)],
        measured={301: (307, HOG), 434: (305, PARROT), 437: (305, PARROT)},
        current=[(307, [300, 301, 302, 303]), (305, [434, 437])],
    )  # fmt: skip

    plan = plan_regroup(facts)

    assert plan.noop
    assert plan.clusters[0].fish_id == fish(307)


def test_a_recalibrated_mission_keeps_every_binding():
    """A new calibration leaves no measurement current until stage 14 runs;
    the clusters' fish are still who the frames are."""
    plan = plan_regroup(
        _facts(
            prediction=[[1, 2], [3]],
            labels=[(1, HOG), (2, HOG), (3, PARROT)],
            measured={},
            current=[(7, [1, 2]), (8, [3])],
            species={7: HOG, 8: PARROT},
        )
    )

    assert plan.noop
    assert [k.fish_id for k in plan.clusters] == [fish(7), fish(8)]


def test_a_frame_in_two_prediction_clusters_is_planned_once():
    plan = plan_regroup(
        _facts(
            prediction=[[1, 2], [2, 3]],
            labels=[(1, HOG), (2, HOG), (3, PARROT)],
            measured={2: (7, HOG)},
            current=[(7, [1, 2]), (None, [3])],
        )
    )

    planned = [c for k in plan.clusters for c in k.captures]
    assert planned.count(cap(2)) == 1
    assert any("more than one prediction cluster" in w for w in plan.warnings)


def test_no_move_is_reported_when_the_cluster_cannot_be_bound():
    """No fish left and no species to make one: the cluster is unbound, which
    invalidates nothing, so the frame does not move."""
    plan = plan_regroup(
        _facts(
            prediction=[[1], [2]],
            labels=[(1, HOG), (2, "Fish, Unknown", BREAK)],
            measured={1: (7, HOG), 2: (7, HOG)},
            current=[(7, [1, 2])],
        )
    )

    assert plan.clusters[1] == NewCluster([cap(2)])
    assert plan.moves == []
    assert any("no species name" in w for w in plan.warnings)


def test_a_duplicate_frame_is_left_out_so_a_regrouped_mission_is_a_noop():
    """The store drops a dive's non-canonical duplicates from the clusters it
    writes (as stage 6.1 does), so the plan must too."""
    plan = plan_regroup(
        _facts(
            prediction=[[1, 2, 3]],
            labels=[(1, HOG), (2, HOG), (3, HOG)],
            measured={1: (7, HOG)},
            current=[(7, [1, 3])],
            duplicates=[2],
        )
    )

    assert plan.noop
    assert plan.clusters == [NewCluster([cap(1), cap(3)], fish_id=fish(7))]
