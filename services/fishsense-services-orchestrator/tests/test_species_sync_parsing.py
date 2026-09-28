"""What a species Label Studio task says: the parser and the dive-link choices.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_sync_species_labels_activity.py -- the pure halves (`_parse_results`,
`_slate_type_choice`, `_calibration_target_choice`, `_reduce_winners`). Names,
bodies and reasons are v1's. `_apply_parsed`'s tests moved to the API
(test_species_label_sync_store.py), where the column-scoped update now does
what it did; the activity-shape tests are in test_species_sync_activities.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fishsense_services_contracts import taxonomy
from fishsense_services_orchestrator.labels.label_studio import LabelStudioTask
from fishsense_services_orchestrator.species import parsing as sut

# ----------------------------- pure parser -----------------------------


def test_parse_results_extracts_all_fields():
    annotation = {
        "result": [
            {"from_name": "grouping", "value": {"choices": ["Part of previous group"]}},
            {"from_name": "exclude", "value": {"choices": ["Top 3 photos of group"]}},
            {
                "from_name": "species",
                "value": {"taxonomy": [["Slate", "Laser on slate"]]},
            },
            {
                "from_name": "measurable",
                "value": {"taxonomy": [["Measurable"]]},
            },
            {"from_name": "fishAngles", "value": {"taxonomy": [["Side"]]}},
            {"from_name": "fishCurve", "value": {"taxonomy": [["No Curve"]]}},
        ]
    }

    parsed = sut.parse_results(annotation)

    assert parsed["grouping"] == "Part of previous group"
    assert parsed["top_three_photos_of_group"] is True
    assert parsed["content_of_image"] == "Slate, Laser on slate"
    assert parsed["fish_measurable_category"] == "Measurable"
    assert parsed["fish_angle_category"] == "Side"
    assert parsed["fish_curved_category"] == "No Curve"


def test_parse_results_handles_minimal_annotation():
    parsed = sut.parse_results({"result": []})

    assert parsed["grouping"] is None
    assert parsed["top_three_photos_of_group"] is None
    assert parsed["content_of_image"] is None
    assert parsed["fish_measurable_category"] is None
    assert parsed["fish_angle_category"] is None
    assert parsed["fish_curved_category"] is None


def test_parse_results_recognizes_negative_top_three_choice():
    annotation = {
        "result": [
            {"from_name": "exclude", "value": {"choices": ["Skip this group"]}},
        ]
    }
    parsed = sut.parse_results(annotation)
    # exclude is True only when literally "Top 3 photos of group" —
    # anything else is False (we still got a choice, just not the
    # affirmative one).
    assert parsed["top_three_photos_of_group"] is False


# ----------------------- the sync of one task --------------------------


def _task(*, annotations, is_labeled=True, annotators=(7,)):
    return LabelStudioTask.from_sdk(
        SimpleNamespace(
            id=101,
            annotators=list(annotators),
            annotations=annotations,
            is_labeled=is_labeled,
            updated_at="2026-05-02T10:00:00Z",
        )
    )


def test_a_task_becomes_its_species_sync():
    task = _task(
        annotations=[
            {
                "result": [
                    {
                        "from_name": "grouping",
                        "value": {"choices": ["Part of previous group"]},
                    },
                    {
                        "from_name": "species",
                        "value": {"taxonomy": [["Reef fish", "Yellowtail Snapper"]]},
                    },
                ]
            }
        ]
    )

    task = LabelStudioTask(**{**task.__dict__, "payload": {"id": 101}})

    sync = sut.species_sync_from_task(task)

    assert sync.completed is True
    assert sync.grouping == "Part of previous group"
    assert sync.content_of_image == "Reef fish, Yellowtail Snapper"
    assert sync.fish_angle_category is None
    assert sync.ls_labeler_id == 7
    assert sync.ls_updated_at == datetime(2026, 5, 2, 10, tzinfo=timezone.utc)
    assert sync.ls_payload["id"] == 101


def test_a_task_with_no_annotation_changes_no_parsed_field():
    """v1 applied the parser only when the task had annotations; completed,
    the labeler, the timestamp and the payload are still written."""
    sync = sut.species_sync_from_task(_task(annotations=[], is_labeled=False))

    assert sync.completed is False
    assert (sync.grouping, sync.content_of_image, sync.top_three_photos_of_group) == (
        None,
        None,
        None,
    )


def test_an_unmapped_annotator_leaves_the_labeler_unset():
    """v1: a task annotated by someone not yet user-synced still syncs."""
    assert (
        sut.species_sync_from_task(_task(annotations=[], annotators=())).ls_labeler_id
        is None
    )


# ----------------------- slate identification -------------------------


def test_slate_type_choice_finds_the_slate_template():
    results = [
        {
            "from_name": "species",
            "value": {
                "taxonomy": [["Slate", "Laser on slate"], ["Slate", "V-Slate 2"]]
            },
        }
    ]
    assert sut.slate_type_choice(results, {"H-Slate", "V-Slate 2"}) == "V-Slate 2"


def test_slate_type_choice_none_when_only_laser_marker():
    results = [
        {"from_name": "species", "value": {"taxonomy": [["Slate", "Laser on slate"]]}}
    ]
    assert sut.slate_type_choice(results, {"H-Slate", "V-Slate 2"}) is None


def test_slate_type_choice_none_for_fish():
    results = [{"from_name": "species", "value": {"taxonomy": [["Fish", "Hogfish"]]}}]
    assert sut.slate_type_choice(results, {"H-Slate"}) is None


def test_slate_type_choice_empty_results():
    assert sut.slate_type_choice([], {"H-Slate"}) is None


def test_reduce_winners_most_recent_per_dive():
    votes = [
        (7, datetime(2026, 5, 1, tzinfo=timezone.utc), 9),
        (7, datetime(2026, 5, 3, tzinfo=timezone.utc), 1),  # newer -> wins for dive 7
        (8, datetime(2026, 5, 2, tzinfo=timezone.utc), 5),
    ]
    assert sut.reduce_winners(votes) == {7: 1, 8: 5}


def test_reduce_winners_timestamp_beats_none_either_order():
    ts = datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert sut.reduce_winners([(7, None, 9), (7, ts, 1)]) == {7: 1}
    assert sut.reduce_winners([(7, ts, 1), (7, None, 9)]) == {7: 1}


# --- the "slate not in list" sentinel --------------------------------------


def test_slate_type_choice_never_returns_the_sentinel():
    """The safety property, pinned directly.

    A slate the labeler cannot identify must not resolve to a slate template.
    Today that holds because the sentinel is not a template row and so is not
    in `valid_names` — but a future operator seeding a template with that
    literal name would silently turn "I don't know which slate this is" into
    a confident, wrong calibration.
    """
    results = [
        {
            "from_name": "species",
            "value": {"taxonomy": [["Slate", taxonomy.SLATE_NOT_IN_LIST_LEAF]]},
        }
    ]
    # Even if the sentinel somehow appears among the valid template names.
    valid = {"H-Slate", "V-Slate 2", taxonomy.SLATE_NOT_IN_LIST_LEAF}

    assert sut.slate_type_choice(results, valid) is None


def test_slate_type_choice_still_finds_a_real_slate_alongside_the_sentinel():
    """The sentinel must not shadow a genuine answer on another path."""
    results = [
        {
            "from_name": "species",
            "value": {
                "taxonomy": [
                    ["Slate", taxonomy.SLATE_NOT_IN_LIST_LEAF],
                    ["Slate", "V-Slate 2"],
                ]
            },
        }
    ]

    assert sut.slate_type_choice(results, {"V-Slate 2"}) == "V-Slate 2"


def test_slate_not_in_list_is_only_the_explicit_sentinel():
    """Distinct from "no slate type": only the sentinel is evidence."""
    sentinel = [
        {
            "from_name": "species",
            "value": {"taxonomy": [["Slate", taxonomy.SLATE_NOT_IN_LIST_LEAF]]},
        }
    ]
    fish = [{"from_name": "species", "value": {"taxonomy": [["Fish", "Hogfish"]]}}]

    assert sut.slate_not_in_list(sentinel) is True
    assert sut.slate_not_in_list(fish) is False
    assert sut.slate_not_in_list([]) is False


# --------------- the calibration-target pass (checkerboard) ---------------


def _checkerboard_annotation(*, extra: list | None = None) -> dict:
    return {
        "result": [
            {
                "from_name": "species",
                "value": {
                    "taxonomy": [
                        ["Calibration Targets", "E4E Checkerboard"],
                        *(extra or []),
                    ]
                },
            }
        ]
    }


def test_calibration_target_choice_finds_the_board():
    results = _checkerboard_annotation()["result"]
    assert sut.calibration_target_choice(results, {"E4E Checkerboard"}) == (
        "E4E Checkerboard"
    )


def test_calibration_target_choice_refuses_the_ruler():
    """The ruler is the validation set, never a calibration source.

    Refused even when a calibration target is called "Ruler" — which is the
    whole point of guarding it by name. A ruler appears in ordinary fish
    dives, so resolving it here would pull them into the calibration cohort
    and fit their extrinsics against a plane nobody intended.
    """
    results = [
        {
            "from_name": "species",
            "value": {"taxonomy": [["Calibration Targets", "Ruler"]]},
        }
    ]
    assert sut.calibration_target_choice(results, {"Ruler", "E4E Checkerboard"}) is None


def test_calibration_target_choice_none_for_an_unseeded_board():
    """A leaf naming no row resolves to nothing rather than guessing."""
    results = _checkerboard_annotation()["result"]
    assert sut.calibration_target_choice(results, set()) is None


def test_calibration_target_choice_none_for_fish():
    results = [{"from_name": "species", "value": {"taxonomy": [["Fish", "Hogfish"]]}}]
    assert sut.calibration_target_choice(results, {"E4E Checkerboard"}) is None


def test_calibration_target_choice_survives_the_laser_on_slate_marker():
    """Labelers mark these frames "Slate, Laser on slate" as well as the board.
    It is only harmless because this reads ALL the taxonomy paths."""
    for order in (
        [["Slate", "Laser on slate"], ["Calibration Targets", "E4E Checkerboard"]],
        [["Calibration Targets", "E4E Checkerboard"], ["Slate", "Laser on slate"]],
    ):
        results = [{"from_name": "species", "value": {"taxonomy": order}}]
        assert (
            sut.calibration_target_choice(results, {"E4E Checkerboard"})
            == "E4E Checkerboard"
        ), order


def test_the_laser_on_slate_marker_does_not_become_a_slate_type():
    """The stage-9 marker is not a slate *template* answer."""
    results = [
        {
            "from_name": "species",
            "value": {
                "taxonomy": [
                    ["Slate", "Laser on slate"],
                    ["Calibration Targets", "E4E Checkerboard"],
                ]
            },
        }
    ]
    assert sut.slate_type_choice(results, {"H-Slate", "V-Slate 2"}) is None
