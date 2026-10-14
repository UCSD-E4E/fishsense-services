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


def _facts(prediction, labels, measured, current, *, priority="high"):
    """`measured`: {frame: (fish number, the fish's species content)};
    `current`: [(fish number or None, [frames])]."""
    clusters = [
        LabelStudioCluster(
            uuid.UUID(int=3_000_000 + i), i, None if f is None else fish(f),
            tuple(cap(n) for n in members),
        )
        for i, (f, members) in enumerate(current)
    ]  # fmt: skip
    frames = {n for c in prediction for n in c}
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
    )


def _dive_341(current=((307, [300, 301]), (305, [302, 303, 434, 437])), stray=305):
    return _facts(
        prediction=[[300, 301], [302, 303], [434, 437]],
        labels=[(300, HOG), (301, HOG), (302, HOG, CONTINUE), (303, HOG),
                (434, PARROT), (437, PARROT)],
        measured={301: (307, HOG), 302: (stray, PARROT if stray == 305 else HOG),
                  434: (305, PARROT), 437: (305, PARROT)},
        current=list(current),
    )  # fmt: skip


def test_dive_341_the_stray_hogfish_frame_rejoins_hogfish_307():
    plan = plan_regroup(_dive_341())

    assert plan.clusters == [
        NewCluster([cap(300), cap(301), cap(302), cap(303)], fish_id=fish(307)),
        NewCluster([cap(434), cap(437)], fish_id=fish(305)),
    ]
    assert [(m.capture_number, m.from_fish, m.to_fish) for m in plan.moves] == [
        (302, 305, 307)
    ]
    assert not plan.noop


def test_dive_465_one_parrotfish_not_two():
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


def test_dive_383_the_grouper_gets_an_animal_of_its_own():
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


def test_a_dive_already_grouped_this_way_is_a_noop():
    plan = plan_regroup(
        _dive_341(current=((307, [300, 301, 302, 303]), (305, [434, 437])), stray=307)
    )

    assert plan.noop
    assert plan.moves == []


def test_the_digest_is_stable_and_follows_the_clusters_planned_from():
    assert plan_regroup(_dive_341()).sha256 == plan_regroup(_dive_341()).sha256
    moved = _dive_341(current=((307, [300, 301, 302]), (305, [303, 434, 437])))
    assert plan_regroup(moved).sha256 != plan_regroup(_dive_341()).sha256


def test_a_dive_stage_14_will_not_measure_is_flagged():
    plan = plan_regroup(
        _facts(
            prediction=[[1]], labels=[(1, HOG)], measured={1: (7, HOG)},
            current=[(7, [1])], priority="low",
        )
    )  # fmt: skip

    assert any("priority" in w for w in plan.warnings)
