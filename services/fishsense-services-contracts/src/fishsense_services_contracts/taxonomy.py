"""The `SpeciesLabel.content_of_image` taxonomy vocabulary.

Ported from fishsense-lite@77e8f8e5 libs/fishsense-shared/src/fishsense_shared/
taxonomy.py (last changed there in 7c4e38e8, #914). The Python half only: both
the orchestrator and the processor import this package, and the processor never
touches the database, so the SQL forms of these predicates live with the stores
that run them, in `fishsense_services_api.taxonomy_sql`.

Label Studio writes the labeler's taxonomy selection into
`SpeciesLabel.content_of_image` as a ", "-joined path:

    "Fish, Hogfish (Lachnolaimus maximus)"  -> a real (wild) fish
    "Fish Model, Weasly Fish"               -> a rigid model
    "Calibration Targets, Ruler"            -> the ruler
    "Calibration Targets, Box"              -> the 0.15 m box
    "Slate, Laser on slate"                 -> a slate frame (stage 9)

Four consumers read that string, and they used to spell the markers
independently: `measure_fish_activity` (Python, data-worker), the stage-14
cohort selector (SQLAlchemy, api), the `dive_pipeline_status` view (raw SQL,
api), and the stage-9 slate cohort. Keeping them in step was a comment-only
contract — and it had already failed. Both the controller and the view
carried a worked example claiming `"Fish Model, …"` and
`"Calibration Targets, Ruler"` were *skipped*, six lines above code that
matched both as measurable; a maintainer trusting it would have deleted the
ruler clause and silently broken ruler validation.

Now the literals live here once, and every consumer derives from them.

**Python vs SQL.** `is_measurable` is the definition of record: measurable
means `measure_fish_activity` will actually bind a Measurement. The SQL
predicates are `LIKE`-based *approximations* of it. v1 chose `LIKE` because
its view had to run on SQLite under test; v2 is Postgres-only and could say
more, but keeps v1's forms so its cohorts select exactly the dives v1's did
(PLAN.md §6.2). `MEASURABILITY_CORPUS` is the shared fixture that keeps the
approximation honest: the API's `test_taxonomy_sql.py` runs the real SQL over
it on Postgres and asserts agreement with `is_measurable`. The known ceiling
is that the real-fish `LIKE` tests the whole string for "contains `(` … ends
with `)`" while `parse_species_names` tests only the final chunk, so a value
whose only parens sit in an *earlier* chunk and which still ends in `)` would
diverge ("Fish (a), Hogfish)"). No taxonomy branch produces that shape; if one
ever does, add it to the corpus and the parity test will fail rather than let
the cohort and the activity disagree.
"""

from __future__ import annotations

# --- Literals, exactly as the Label Studio labeling config emits them ------

# Prefix for a physical fish model. The leaf after it is the model's identity
# — the `name` natural key on Fish.
FISH_MODEL_PREFIX = "Fish Model,"

# The top-level taxonomy branch for the physical targets that are not fish.
# Its leaves mean two opposite things to the pipeline: a known *length* to
# validate against (the ruler, the box) or a known *plane* to calibrate from
# (the checkerboard). `MEASURABLE_CALIBRATION_TARGETS` and
# `NON_PLANAR_CALIBRATION_LEAVES` are the two halves of that split, and the
# second is derived from the first. See `calibration_target_leaf`.
CALIBRATION_TARGETS_BRANCH = "Calibration Targets"

# The ruler is a rigid known-length target like the models, so it measures
# through the same name-keyed path. Unlike a fish model its endpoints are
# unambiguous — no tip-vs-fork landmark uncertainty — so it isolates
# calibration error from labeling convention.
RULER_NAME = "Ruler"
RULER_CONTENT = f"{CALIBRATION_TARGETS_BRANCH}, {RULER_NAME}"

# The E4E-branded printed checkerboard (15 x 11 squares, 14 x 10 interior
# corners) the 2023 pool-test `LaserCalibration` folders were shot against.
# It identifies a dive as calibratable from a *plane* rather than from a
# `DiveSlate` template - the second producer of `LaserExtrinsics`, see
# `docs/plans/checkerboard-laser-calibration.md`.
#
# The name is the join key: a `CalibrationTarget` row carries this string as
# its `name`, exactly as `DiveSlate.name` carries the slate-type leaves. So
# adding a second board means adding a taxonomy choice AND a row whose name
# matches it, and nothing else.
CHECKERBOARD_NAME = "E4E Checkerboard"
CHECKERBOARD_CONTENT = f"{CALIBRATION_TARGETS_BRANCH}, {CHECKERBOARD_NAME}"

# A rigid box carrying a duct-tape patch on one face, added to the taxonomy
# 2026-09-07. The known span is 0.15 m between two corners of that TAPE — not a
# box edge — so the reference cannot be re-derived from the box's dimensions;
# it lives in `views.KNOWN_FISH_MODELS` with the rest of the reference lengths.
# Measures through the same name-keyed path as the ruler.
BOX_CONTENT = "Calibration Targets, Box"
BOX_NAME = "Box"

# `Calibration Targets, <leaf>` -> the `Fish.name` it measures as.
#
# An explicit allowlist, NOT a prefix rule, and that is the whole design of
# this constant. The `Fish Model` branch can use a prefix because every leaf
# under it is a model with a length; `Calibration Targets` is mixed — the
# ruler and the box span a single known distance a head/tail pair marks, while
# `E4E Checkerboard` does not. A `LIKE 'Calibration Targets,%'` rule would
# sweep the checkerboard in, it would have no `fishmodelreference` row, and
# the stage-14 cohort would offer frames `measure_fish_activity` skips —
# the never-goes-false wedge, reachable the moment a labeler picks it.
#
# Adding a target here means adding it to the species labeling XML and to
# `views.KNOWN_FISH_MODELS`; the parity tests on both sides name whichever you
# forget.
MEASURABLE_CALIBRATION_TARGETS: dict[str, str] = {
    RULER_CONTENT: RULER_NAME,
    BOX_CONTENT: BOX_NAME,
}

# Leaves under `Calibration Targets` that must never resolve to a planar
# calibration target.
#
# **Derived from `MEASURABLE_CALIBRATION_TARGETS`, not listed again**, because
# the two answer the same question from opposite ends and the branch is mixed:
# a leaf that measures as a known LENGTH (the ruler, the box) is a validation
# object, and a leaf that supplies a known PLANE (the checkerboard) is a
# calibration source. Nothing is both, so deriving one from the other means
# adding a fifth leaf lands on the safe side by default — excluded from
# calibration until someone deliberately says otherwise — rather than becoming
# a calibration source the moment a matching `CalibrationTarget` row exists.
#
# Same posture as `SLATE_NOT_IN_LIST_LEAF`, and the ruler shows why it is not
# theoretical: it appears in ordinary fish dives, so a `CalibrationTarget` row
# named "Ruler" seeded later would silently pull those dives into the
# checkerboard-calibration cohort and fit their extrinsics against a plane
# nobody intended. Calibrating from a validation object would also make every
# accuracy number self-confirming.
NON_PLANAR_CALIBRATION_LEAVES = frozenset(MEASURABLE_CALIBRATION_TARGETS.values())

# Every `Fish Model, <name>` leaf a labeler can pick, in species-XML order.
#
# Lives here rather than in either service because the two halves are split
# across packages: the labeling XML that offers these choices is in the
# api-worker, and the `fishmodelreference` rows that make a measurement
# *gradeable* are in the API. Nothing connected them, and the gap is silent —
# `fish_model_measurement_accuracy` inner-joins on `Fish.name`, so a model with
# no reference row produces no rows at all. No error, no NULL, just absence.
#
# `Weasly Fish` sat in exactly that state in prod: pickable, measurable, and
# graded by nobody.
#
# Kept in step from both sides: the api-worker asserts the XML matches this
# list, and the API asserts every name here has a reference row. Adding a model
# means editing the XML, this list, and `views.KNOWN_FISH_MODELS` — the tests
# name whichever you forget.
LABELED_FISH_MODELS = (
    "Weasly Fish",
    "Snook",
    "Grouper",
    "Shark",
    "Gray Anthias",
    "Purple Angel",
    "Yellow Anthias",
)

# Stage-9 marker: the frame shows the slate with the laser on it.
SLATE_CONTENT_MARKER = "Slate, Laser on slate"

# Its negative counterpart. Previously spelled only in the species labeling
# XML, which is why the sync parser could not refer to it.
SLATE_NO_LASER_CONTENT = "Slate, Laser not on slate"

# The two CONTENT answers under `Slate`, as opposed to the slate *type*
# (`H-Slate`, `V-Slate 2`, ...) that species sync maps to `Dive.dive_slate_id`.
#
# **Both live under `Slate` as sibling paths, and a labeler is meant to pick
# one of each.** Label Studio returns the picked paths in selection order, so
# `content_of_image` cannot be `taxonomy[0]`: whether the frame reads as
# stage-9 eligible would depend on which choice was clicked first. It did, and
# 34 prod rows across 6 dives lost their laser answer that way -- dive 22 lost
# all ten of its frames and with them any route to a calibration.
# `_content_of_image` therefore prefers a path in this set.
SLATE_LASER_CONTENT: frozenset[str] = frozenset(
    {SLATE_CONTENT_MARKER, SLATE_NO_LASER_CONTENT}
)

# The slate-type answer for "I can see a slate, and it is not one of the
# templates you are offering me."
#
# Not a nicety. The slate-type choices in the species taxonomy are exactly the
# `DiveSlate` template rows, and a slate can be absent from that table
# permanently -- V-Slate 7 was lost during a dive, so it can never be scanned
# and can never be added. A labeler facing it previously had two options, both
# bad: pick a wrong neighbour, or say nothing. The wrong neighbour is the
# dangerous one, because a wrong slate template yields a wrong *scale*, and
# scale error is the one thing reprojection residual cannot see -- it would
# calibrate cleanly and measure every fish in the dive wrong.
#
# It is deliberately NOT a `DiveSlate` row: it must never resolve to a
# `dive_slate_id`, which is what keeps such a dive out of stages 9 and 13
# rather than calibrating it against a slate it isn't.
SLATE_NOT_IN_LIST_LEAF = "Slate not in list"

__all__ = [
    "BOX_CONTENT",
    "BOX_NAME",
    "CALIBRATION_TARGETS_BRANCH",
    "CHECKERBOARD_CONTENT",
    "CHECKERBOARD_NAME",
    "FISH_MODEL_PREFIX",
    "LABELED_FISH_MODELS",
    "MEASURABILITY_CORPUS",
    "MEASURABLE_CALIBRATION_TARGETS",
    "NON_PLANAR_CALIBRATION_LEAVES",
    "RULER_CONTENT",
    "RULER_NAME",
    "SLATE_CONTENT_MARKER",
    "SLATE_LASER_CONTENT",
    "SLATE_NO_LASER_CONTENT",
    "SLATE_NOT_IN_LIST_LEAF",
    "SQL_BROADER_THAN_PYTHON",
    "calibration_target_leaf",
    "is_measurable",
    "parse_model_name",
    "parse_species_names",
]


def parse_species_names(content_of_image: str | None) -> tuple[str, str] | None:
    """Pull `(common_name, scientific_name)` out of a real fish's taxonomy path.

    Format: `"..., Common Name (Scientific name)"`. Returns None if the field
    is empty or off-shape — we skip rather than write a malformed Species row.

    The shape test is a bare `"("`, deliberately, even though the extraction
    below splits on `" ("`. Tightening it to `" ("` looks like an improvement
    — it would skip `"Fish, Hogfish(Lachnolaimus maximus)"` instead of
    splitting it into nonsense — but the SQL's `REAL_FISH_LIKE` cannot express
    "space before the paren" over the whole string, so the tightening made
    Python reject rows the SQL still matched. That direction is the
    never-drains wedge: cohort offers, activity skips, no Measurement, dive
    re-selected forever. Agreeing with the SQL matters more than rejecting a
    shape the Label Studio taxonomy cannot emit, so the guard stays loose and
    `MEASURABILITY_CORPUS` pins the agreement.
    """
    if not content_of_image:
        return None
    last_chunk = content_of_image.split(", ")[-1]
    if "(" not in last_chunk or not last_chunk.endswith(")"):
        return None
    common = last_chunk.split(" (")[0].strip()
    scientific = last_chunk.split(" (")[-1][:-1].strip()
    if not common or not scientific:
        return None
    return common, scientific


def parse_model_name(content_of_image: str | None) -> str | None:
    """Return the target name for a rigid known-length target, else None.

    Covers `"Fish Model, <name>"` and the measurable calibration targets
    (`"Calibration Targets, Ruler"` -> `"Ruler"`,
    `"Calibration Targets, Box"` -> `"Box"`). Real fish, the checkerboard, and
    every other branch return None. An empty leaf (`"Fish Model,"` with nothing after)
    returns None — nothing to identify — matching the "skip rather than write
    a malformed row" posture of `parse_species_names`.
    """
    if not content_of_image:
        return None
    target = MEASURABLE_CALIBRATION_TARGETS.get(content_of_image.strip())
    if target is not None:
        return target
    if not content_of_image.startswith(FISH_MODEL_PREFIX):
        return None
    # `.strip(" ")`, not `.strip()`: the SQL guard is `TRIM(col)`, which removes
    # spaces only. Stripping all whitespace here would make Python reject
    # "Fish Model,\t" while the SQL still matched it — the wedge again.
    name = content_of_image[len(FISH_MODEL_PREFIX) :].strip(" ")
    return name or None


def calibration_target_leaf(taxonomy_path) -> str | None:
    """The planar calibration target a Label Studio taxonomy path names.

    `taxonomy_path` is one LS taxonomy selection, e.g.
    `["Calibration Targets", "E4E Checkerboard"]`. Returns the leaf when that
    path names a target a plane can be fitted to, else None.

    Reads the whole path rather than the leaf alone, unlike the slate-type
    scan it is modelled on. A slate leaf ("V-Slate 2") is unambiguous wherever
    it appears; "E4E Checkerboard" off its branch is not necessarily the
    calibration target, and this answer sets a dive's calibration source, so
    it should be the narrow reading.

    The ruler is refused here rather than downstream — see
    `NON_PLANAR_CALIBRATION_LEAVES`.

    Returning the *name* rather than a row id keeps this package free of the
    database: the caller matches it against the `CalibrationTarget` rows that
    exist, and a leaf naming no row simply resolves to nothing, which is the
    same "refuse rather than guess" direction as `SLATE_NOT_IN_LIST_LEAF`.
    """
    path = list(taxonomy_path or [])
    if len(path) < 2 or path[0] != CALIBRATION_TARGETS_BRANCH:
        return None
    leaf = (path[-1] or "").strip()
    if not leaf or leaf in NON_PLANAR_CALIBRATION_LEAVES:
        return None
    return leaf


def is_measurable(content_of_image: str | None) -> bool:
    """True iff `measure_fish_activity` can bind this row to a Measurement.

    This is the definition of record; the `LIKE` predicates approximate it.
    Anything looser makes the stage-14 cohort offer an image the activity
    always skips: no Measurement is written, `NOT EXISTS (measurement)` stays
    true, and the dive is re-selected every hour forever.
    """
    return (
        parse_species_names(content_of_image) is not None
        or parse_model_name(content_of_image) is not None
    )


# --- Shared parity fixture -------------------------------------------------

# `(content_of_image, is_measurable)` over every branch that actually occurs
# plus the boundary cases. Used by this package's unit tests AND by the API's
# `test_taxonomy_sql.py`, which runs the real SQL predicate over it on
# Postgres — that cross-check is what stops the Python and SQL representations
# drifting apart again.
MEASURABILITY_CORPUS: tuple[tuple[str | None, bool], ...] = (
    # Real fish — the `Common (Scientific)` leaf.
    ("Fish, Hogfish (Lachnolaimus maximus)", True),
    ("Fish, Stoplight Parrotfish (Sparisoma viride)", True),
    ("Fish, Bar Jack (Caranx ruber)", True),
    # Rigid known-length targets — name-keyed, no parens.
    ("Fish Model, Weasly Fish", True),
    ("Fish Model, Snook", True),
    ("Fish Model, Purple Angel", True),
    ("Calibration Targets, Ruler", True),
    ("Calibration Targets, Box", True),
    # Shapes the taxonomy cannot emit, kept because they are where the SQL
    # approximation and the Python parser are most likely to drift apart. All
    # of these parse into junk names — that is accepted deliberately: the two
    # representations agreeing matters more than rejecting an unreachable
    # value, because "SQL matches, Python skips" is the never-drains wedge
    # while "both accept junk" is merely a junk row on input that never comes.
    ("Fish, Hogfish(Lachnolaimus maximus)", True),  # no space before the paren
    ("Fish, ()", True),
    ("Fish Model,\t", True),  # tab leaf: SQL TRIM removes spaces only
    # Not measurable.
    ("Slate, Laser on slate", False),
    # A plane, not a known length. The checkerboard identifies a dive as
    # calibratable; it is never something stage 14 measures.
    ("Calibration Targets, E4E Checkerboard", False),
    ("Calibration Targets, Slate", False),
    # Same branch as the ruler and the box; deliberately not a known-length
    # target — see `MEASURABLE_CALIBRATION_TARGETS`.
    ("Calibration Targets, E4E Checkerboard", False),
    ("Fish Model,", False),  # empty leaf — parent node, no model picked
    ("Fish Model,   ", False),  # whitespace-only leaf
    ("", False),
    (None, False),
)


# Shapes where the SQL approximation is BROADER than `is_measurable`, i.e. the
# view/cohort would call them measurable and `measure_fish_activity` would skip
# them. That direction is the dangerous one — it is the never-goes-false wedge
# — so the set is pinned rather than left to chance:
# the API's `test_taxonomy_sql.py` asserts the SQL matches exactly these and
# nothing else, so a *new* divergence fails the build instead of reaching prod.
#
# The whole class comes from one thing the `LIKE` patterns cannot express: the
# empty-name guard in `parse_species_names`. `%(%)` sees "contains ( and ends
# with )" and has no way to check that the pieces either side of the paren are
# non-empty. Closing it would need Postgres-only string functions, which v1's
# view couldn't use because its tests ran on SQLite. v2 runs only on Postgres
# but keeps the pinned divergence, so its cohorts stay v1's (PLAN.md §6.2).
#
# None of these are producible by the Label Studio taxonomy config — every leaf
# is a fixed choice with a non-empty common and scientific name. If one ever
# becomes reachable, fix the guard rather than extending this tuple.
SQL_BROADER_THAN_PYTHON: tuple[str, ...] = (
    "Fish,  (Lachnolaimus maximus)",  # empty common name
    "Fish, Hogfish ()",  # empty scientific name
)
