"""Which taxonomy path becomes `content_of_image` when a labeler picks two.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-
worker/tests/test_species_slate_content_path_order.py (v2: the parser is
`species.parsing.parse_results`, v1's `_parse_results`).

The species Taxonomy puts two orthogonal answers under `Slate`: the CONTENT
answer (`Laser on slate` / `Laser not on slate`, which stage 9 keys its cohort
on) and the SLATE TYPE (`H-Slate`, `V-Slate 2`, ..., which species sync maps to
`Dive.dive_slate_id`). A labeler on a slate frame is meant to pick both.

Label Studio returns the paths in **selection order**, and `_content_of_image`
took `taxonomy[0]`. So whether stage 9 ever saw the frame depended on which
choice the labeler happened to click first — silently, with the dropped path
still sitting in `label_studio_json`.

Measured in prod 2026-09-16: 34 rows across 6 dives whose stored annotation
contains a laser-on/off path while `content_of_image` holds a slate type. Dive
22's ten frames were all of them, which left it with `dive_slate_id` set, zero
stage-9 eligibility and therefore no route to a calibration at all.
"""

from fishsense_services_orchestrator.species import (
    parsing as sut,
)
from fishsense_services_contracts import taxonomy

# pylint: disable=protected-access


def _annotation(*paths):
    return {
        "result": [
            {
                "from_name": "species",
                "to_name": "image",
                "type": "taxonomy",
                "value": {"taxonomy": list(paths)},
            }
        ]
    }


def test_the_laser_marker_wins_when_the_slate_type_was_clicked_first():
    """The exact prod shape: dive 22, ten frames, H-Slate clicked first."""
    parsed = sut.parse_results(
        _annotation(["Slate", "H-Slate"], ["Slate", "Laser on slate"])
    )
    assert parsed["content_of_image"] == taxonomy.SLATE_CONTENT_MARKER


def test_the_laser_marker_still_wins_when_it_was_clicked_first():
    parsed = sut.parse_results(
        _annotation(["Slate", "Laser on slate"], ["Slate", "H-Slate"])
    )
    assert parsed["content_of_image"] == taxonomy.SLATE_CONTENT_MARKER


def test_laser_not_on_slate_is_also_a_content_answer():
    """`Laser not on slate` is a real content statement -- it says the frame
    shows the slate without a usable dot. It must not lose to a slate type
    either, or the frame reads as having no content answer at all."""
    parsed = sut.parse_results(
        _annotation(["Slate", "Tic-Tac-Toe 3"], ["Slate", "Laser not on slate"])
    )
    assert parsed["content_of_image"] == "Slate, Laser not on slate"


def test_a_slate_type_alone_is_still_reported_verbatim():
    """No content answer was given, so there is nothing to prefer. Reporting
    the type unchanged keeps the 24 prod rows that legitimately look like this
    reading the way they always have."""
    parsed = sut.parse_results(_annotation(["Slate", "Tic-Tac-Toe 1"]))
    assert parsed["content_of_image"] == "Slate, Tic-Tac-Toe 1"


def test_the_not_in_list_sentinel_is_untouched():
    """53 prod rows carry `Slate, Slate not in list` -- the labeler saying the
    slate is unidentifiable. It is a content answer in its own right and must
    not be mistaken for a type path and skipped."""
    parsed = sut.parse_results(_annotation(["Slate", taxonomy.SLATE_NOT_IN_LIST_LEAF]))
    assert parsed["content_of_image"] == "Slate, Slate not in list"


def test_the_sentinel_still_loses_to_an_explicit_laser_answer():
    parsed = sut.parse_results(
        _annotation(
            ["Slate", taxonomy.SLATE_NOT_IN_LIST_LEAF],
            ["Slate", "Laser on slate"],
        )
    )
    assert parsed["content_of_image"] == taxonomy.SLATE_CONTENT_MARKER


def test_non_slate_branches_keep_taking_the_first_path():
    """Fish / Fish Model / Calibration Targets carry no laser-marker path, so
    nothing about them changes -- this is the regression guard for the 2,900+
    prod rows on those branches."""
    for path in (
        ["Fish", "Hogfish (Lachnolaimus maximus)"],
        ["Fish Model", "Weasly Fish"],
        ["Calibration Targets", "Ruler"],
    ):
        assert sut.parse_results(_annotation(path))["content_of_image"] == ", ".join(
            path
        )


def test_a_fish_path_is_not_displaced_by_a_stray_slate_path():
    """Defensive: a labeler who picked a fish and then a slate type still gets
    the fish, because the fish path is first and no laser marker is present."""
    parsed = sut.parse_results(
        _annotation(["Fish", "Hogfish (Lachnolaimus maximus)"], ["Slate", "H-Slate"])
    )
    assert parsed["content_of_image"] == "Fish, Hogfish (Lachnolaimus maximus)"


def test_the_slate_type_is_still_extracted_independently():
    """The fix must not disturb `_slate_type_choice`, which is what writes
    `dive_slate_id` -- it already scans every path."""
    parsed = sut.parse_results(
        _annotation(["Slate", "H-Slate"], ["Slate", "Laser on slate"])
    )
    assert parsed["content_of_image"] == taxonomy.SLATE_CONTENT_MARKER
    # and the raw paths are still both present for the type extractor
    assert parsed["fish_measurable_category"] is None


def test_the_marker_pair_lives_in_shared_taxonomy():
    """Spelled once. `Laser not on slate` previously existed only in the
    labeling-config XML, so the parser could not refer to it."""
    assert taxonomy.SLATE_CONTENT_MARKER in taxonomy.SLATE_LASER_CONTENT
    assert "Slate, Laser not on slate" in taxonomy.SLATE_LASER_CONTENT
