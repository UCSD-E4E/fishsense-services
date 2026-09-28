"""Unit tests for the `content_of_image` taxonomy vocabulary.

Ported from fishsense-lite@77e8f8e5 libs/fishsense-shared/tests/test_taxonomy.py.
The tests of the SQL fragment builders moved with the builders, to the API
package (`fishsense_services_api.taxonomy_sql`, `tests/test_taxonomy_sql.py`),
because the SQL runs on the database side and this package is pure Python.

`SpeciesLabel.content_of_image` is a ", "-joined Label Studio taxonomy path,
and four consumers read it: the stage-14 measure activity (Python), the
stage-14 cohort selector, the `dive_pipeline_status` view (raw SQL), and the
stage-9 slate cohort. They used to spell the markers independently, and the
docstrings describing them had drifted so far that the worked example in two
files said `"Fish Model, …"` and `"Calibration Targets, Ruler"` were skipped
while the code six lines below matched both as measurable.

`MEASURABILITY_CORPUS` is the shared fixture: every branch that actually
occurs, plus the boundary cases. The API's `test_taxonomy_sql.py` runs the
*SQL* predicate over the same corpus on real Postgres and asserts it agrees
with `is_measurable` here, so the two representations can't drift silently
again.
"""

from __future__ import annotations

import pytest

from fishsense_services_contracts import taxonomy as sut


def test_markers_match_the_label_studio_config():
    assert sut.FISH_MODEL_PREFIX == "Fish Model,"
    assert sut.RULER_CONTENT == "Calibration Targets, Ruler"
    assert sut.RULER_NAME == "Ruler"
    assert sut.BOX_CONTENT == "Calibration Targets, Box"
    assert sut.BOX_NAME == "Box"
    assert sut.SLATE_CONTENT_MARKER == "Slate, Laser on slate"


def test_measurable_calibration_targets_is_an_allowlist_not_a_prefix():
    """`Calibration Targets` is a mixed branch, so membership has to be named.

    The ruler and the box are rigid known-length targets and measure through
    the name-keyed path; `E4E Checkerboard` sits in the same branch and has no
    single length a head/tail pair spans. A `LIKE 'Calibration Targets,%'`
    rule — the shape the fish-model half uses — would sweep the checkerboard
    in, and it would then have no `fishmodelreference` row, so the cohort
    would offer frames `measure_fish_activity` skips forever. Hence a literal
    allowlist, which is also what `rigid_target_sql` renders.
    """
    assert set(sut.MEASURABLE_CALIBRATION_TARGETS) == {
        sut.RULER_CONTENT,
        sut.BOX_CONTENT,
    }
    assert "Calibration Targets, E4E Checkerboard" not in (
        sut.MEASURABLE_CALIBRATION_TARGETS
    )


# --------------------------------------------------------------------
# parse_species_names — real (wild) fish
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            "Fish, Hogfish (Lachnolaimus maximus)",
            ("Hogfish", "Lachnolaimus maximus"),
        ),
        (
            "Fish, Stoplight Parrotfish (Sparisoma viride)",
            ("Stoplight Parrotfish", "Sparisoma viride"),
        ),
        # Only the LAST ", "-chunk is the species; earlier chunks are the
        # taxonomy path the labeler drilled through.
        ("Fish, Reef, Bar Jack (Caranx ruber)", ("Bar Jack", "Caranx ruber")),
    ],
)
def test_parse_species_names_reads_the_last_chunk(content, expected):
    assert sut.parse_species_names(content) == expected


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "   ",
        "Fish Model, Weasly Fish",  # no parens
        "Calibration Targets, Ruler",  # no parens
        "Slate, Laser on slate",  # no parens
        "Fish, Hogfish (",  # unbalanced
        "Fish, Hogfish )",  # no opening paren
        "Fish,  (Lachnolaimus maximus)",  # empty common name
        "Fish, Hogfish ()",  # empty scientific name
    ],
)
def test_parse_species_names_returns_none_off_shape(content):
    """We skip rather than write a malformed Species row."""
    assert sut.parse_species_names(content) is None


# --------------------------------------------------------------------
# parse_model_name — rigid known-length targets
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("Fish Model, Weasly Fish", "Weasly Fish"),
        ("Fish Model, Snook", "Snook"),
        ("Fish Model, Purple Angel", "Purple Angel"),
        # The ruler is a rigid known-length target like the models, so it
        # resolves through the same name-keyed path.
        ("Calibration Targets, Ruler", "Ruler"),
        # So is the box (0.15 m), added 2026-09-07.
        ("Calibration Targets, Box", "Box"),
    ],
)
def test_parse_model_name(content, expected):
    assert sut.parse_model_name(content) == expected


@pytest.mark.parametrize(
    "content",
    [
        None,
        "",
        "Fish Model,",  # empty leaf — nothing to identify
        "Fish Model,   ",
        "Fish, Hogfish (Lachnolaimus maximus)",  # a real fish
        "Calibration Targets, Slate",  # not a known-length target
        # Same branch as the ruler and the box, deliberately NOT measurable:
        # no single span a head/tail pair marks.
        "Calibration Targets, E4E Checkerboard",
        "Slate, Laser on slate",
    ],
)
def test_parse_model_name_returns_none_off_shape(content):
    assert sut.parse_model_name(content) is None


def test_a_value_is_never_both_a_species_and_a_model():
    """`measure_fish_activity` computes both and branches on which is set, so
    an overlap would make the binding order-dependent."""
    for content, _ in sut.MEASURABILITY_CORPUS:
        both = (
            sut.parse_species_names(content) is not None
            and sut.parse_model_name(content) is not None
        )
        assert not both, f"{content!r} parses as both a species and a model"


# --------------------------------------------------------------------
# is_measurable
# --------------------------------------------------------------------


@pytest.mark.parametrize(("content", "expected"), sut.MEASURABILITY_CORPUS)
def test_is_measurable_over_the_corpus(content, expected):
    assert sut.is_measurable(content) is expected


def test_is_measurable_is_exactly_what_the_activity_can_bind():
    """The definition that matters: measurable == the activity will produce a
    Measurement. Any looser and the stage-14 cohort offers an image the
    activity always skips, no Measurement is ever written, and the dive is
    re-selected every hour forever — the never-goes-false shape that blocked
    scheduling stage 14 before 2026-07-17."""
    for content, _ in sut.MEASURABILITY_CORPUS:
        bindable = (
            sut.parse_species_names(content) is not None
            or sut.parse_model_name(content) is not None
        )
        assert sut.is_measurable(content) is bindable


def test_corpus_covers_both_measurable_and_unmeasurable():
    """A corpus that drifted to all-True would make the SQL parity test
    vacuous."""
    outcomes = {expected for _, expected in sut.MEASURABILITY_CORPUS}
    assert outcomes == {True, False}


# --------------------------------------------------------------------
# The pinned SQL divergence (the SQL itself is tested in the API)
# --------------------------------------------------------------------


def test_sql_broader_rows_are_unmeasurable_in_python():
    """The pinned divergence set must actually be Python-unmeasurable — that
    is the whole claim being tracked."""
    for content in sut.SQL_BROADER_THAN_PYTHON:
        assert sut.is_measurable(content) is False


def test_sql_broader_rows_are_not_also_in_the_corpus():
    """The corpus asserts exact agreement; the divergence tuple asserts a known
    mismatch. A value in both would make one of the two tests a lie."""
    corpus_values = {c for c, _ in sut.MEASURABILITY_CORPUS}
    assert not corpus_values & set(sut.SQL_BROADER_THAN_PYTHON)


# --- the "slate not in list" sentinel --------------------------------------
#
# Added 2026-08-27. V-Slate 7 was lost during a dive, so it can never be
# scanned and can never become a `DiveSlate` template row. Before the
# sentinel, a labeler looking at it was offered only V-Slate 1..4 and had to
# either pick a wrong neighbour or say nothing -- and a wrong slate produces a
# wrong *scale*, which reprojection error is blind to. The sentinel is the
# honest third answer.


def test_slate_not_in_list_is_distinct_from_the_stage9_marker():
    """They live on separate taxonomy paths and mean different things.

    `SLATE_CONTENT_MARKER` is taxonomy[0] ("this frame shows a slate with the
    laser on it"); the sentinel is the slate-*type* answer. Collapsing them
    would make an unidentifiable slate drop out of stage 9 as well.
    """
    assert sut.SLATE_NOT_IN_LIST_LEAF != sut.SLATE_CONTENT_MARKER


def test_slate_not_in_list_is_not_measurable():
    """It must never reach stage 14 as a measurable target."""
    assert not sut.is_measurable(sut.SLATE_NOT_IN_LIST_LEAF)
    assert not sut.is_measurable(f"Slate, {sut.SLATE_NOT_IN_LIST_LEAF}")


def test_slate_not_in_list_is_exported():
    assert "SLATE_NOT_IN_LIST_LEAF" in sut.__all__


# --- the calibration-target branch -----------------------------------------
#
# `Calibration Targets` has held `Ruler` and `E4E Checkerboard` since #371,
# but only as a *label*: nothing read the branch. It is now the hook that
# tells a dive it was shot against a planar calibration target, the way the
# slate-type leaf tells it which `DiveSlate` it was shot against.
#
# The two leaves under that branch mean opposite things to the pipeline, which
# is the whole reason this parser exists rather than a bare `path[0] ==` test.
# The checkerboard supplies a *plane* to fit laser extrinsics against. The
# ruler supplies a known *length* to validate the resulting measurements —
# it is the validation set, and calibrating against it would make every
# validation trivially self-confirming.


def test_calibration_target_leaf_reads_the_checkerboard():
    assert (
        sut.calibration_target_leaf(["Calibration Targets", "E4E Checkerboard"])
        == "E4E Checkerboard"
    )


@pytest.mark.parametrize("leaf", ["Ruler", "Box"])
def test_calibration_target_leaf_refuses_a_measurable_target(leaf):
    """A known-LENGTH target is a validation object, never a calibration source.

    Guarded by name rather than left to "no CalibrationTarget row is called
    that", for the same reason `SLATE_NOT_IN_LIST_LEAF` is guarded explicitly:
    seeding such a row later would silently turn every ruler frame in an
    ordinary fish dive into a calibration plane. Calibrating from a validation
    object would also make every accuracy number self-confirming.
    """
    assert sut.calibration_target_leaf(["Calibration Targets", leaf]) is None


def test_the_two_halves_of_the_branch_do_not_overlap():
    """`Calibration Targets` is mixed, and the split has to be exhaustive.

    A leaf is either a known length to validate against or a known plane to
    calibrate from — never both, and never neither by accident. The exclusion
    set is DERIVED from the measurable allowlist rather than restated, so a
    fifth leaf added to that branch lands on the safe side by default:
    excluded from calibration until someone deliberately says otherwise,
    rather than becoming a calibration source the moment a matching
    `CalibrationTarget` row exists.
    """
    assert sut.NON_PLANAR_CALIBRATION_LEAVES == frozenset(
        sut.MEASURABLE_CALIBRATION_TARGETS.values()
    )
    assert sut.CHECKERBOARD_NAME not in sut.NON_PLANAR_CALIBRATION_LEAVES


@pytest.mark.parametrize(
    "path",
    [
        [],
        ["Calibration Targets"],  # parent node, no target picked
        ["Fish Model", "Weasly Fish"],
        ["Slate", "Laser on slate"],
        ["Fish", "Hogfish (Lachnolaimus maximus)"],
        # The leaf name alone, off its branch. A `SpeciesLabel` whose taxonomy
        # says nothing about calibration targets must not be read as one.
        ["E4E Checkerboard"],
    ],
)
def test_calibration_target_leaf_returns_none_off_branch(path):
    assert sut.calibration_target_leaf(path) is None


def test_calibration_target_leaf_ignores_blank_leaves():
    assert sut.calibration_target_leaf(["Calibration Targets", "   "]) is None


def test_the_checkerboard_is_not_measurable():
    """It is a plane, not a known-length target — stage 14 must skip it."""
    assert not sut.is_measurable(sut.CHECKERBOARD_CONTENT)


def test_checkerboard_content_is_the_branch_joined_to_the_leaf():
    """`content_of_image` is the ", "-joined path, so these must agree."""
    assert sut.CHECKERBOARD_CONTENT == (
        f"{sut.CALIBRATION_TARGETS_BRANCH}, {sut.CHECKERBOARD_NAME}"
    )
    assert sut.RULER_CONTENT == f"{sut.CALIBRATION_TARGETS_BRANCH}, {sut.RULER_NAME}"


def test_calibration_target_symbols_are_exported():
    for name in (
        "CALIBRATION_TARGETS_BRANCH",
        "CHECKERBOARD_CONTENT",
        "CHECKERBOARD_NAME",
        "NON_PLANAR_CALIBRATION_LEAVES",
        "calibration_target_leaf",
    ):
        assert name in sut.__all__


# --- v2: where the vocabulary lives ----------------------------------------


def test_this_module_carries_no_sql():
    """v2 split v1's module in two: the vocabulary here, the SQL in the API.

    Both the orchestrator and the processor import this, and the processor
    never touches the database (docs/port-plan.md), so a SQL fragment here
    would be a builder with no caller on one side. The SQL lives with the
    stores that run it, and the API's parity test pins it to these literals.
    """
    sql_names = [
        name for name in sut.__all__ if name.endswith("_sql") or name.endswith("_LIKE")
    ]
    assert sql_names == []
    assert not hasattr(sut, "measurable_species_sql")


def test_every_exported_name_exists():
    for name in sut.__all__:
        assert hasattr(sut, name), name
