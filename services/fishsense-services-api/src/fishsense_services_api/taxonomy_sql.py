"""The SQL forms of the `content_of_image` taxonomy predicates.

Ported from fishsense-lite@77e8f8e5 libs/fishsense-shared/src/fishsense_shared/
taxonomy.py (`REAL_FISH_LIKE`, `FISH_MODEL_LIKE`, `rigid_target_sql`,
`calibration_target_name_sql`, `measurable_species_sql`), which v1's views
(`_MEASURABLE_SPECIES_SQL`, `_IS_FISH_MODEL_SQL`, the mislabel view's
`_NOT_A_CALIBRATION_TARGET_SQL`) and its stage-9 and stage-14 cohort selectors
rendered. The vocabulary itself -- `is_measurable`, the parsers, the corpus --
is `fishsense_services_contracts.taxonomy`; this is its database side, next to
the stores and views that run it.

`is_measurable` is the definition of record. These `LIKE` forms approximate it,
exactly as v1's did, so v2's cohorts select the rows v1's selected (PLAN.md
§6.2); `tests/test_taxonomy_sql.py` runs them on Postgres over the shared
`MEASURABILITY_CORPUS` and pins the one known divergence
(`SQL_BROADER_THAN_PYTHON`).

v2 changes:

* **the literals are a copy.** The API image installs only the API package, not
  the contracts, so the handful of literals the SQL renders are spelled here
  again. The copy is pinned to the contracts' vocabulary by a test, so a
  taxonomy change made on one side only fails the build;
* v1's SQLAlchemy form of the cohort predicate (`_measurable_species_conditions`)
  is not ported: v2's stores write raw SQL, so these strings are the only form.
"""

__all__ = [
    "FISH_MODEL_LIKE",
    "FISH_MODEL_PREFIX",
    "MEASURABLE_CALIBRATION_TARGETS",
    "REAL_FISH_LIKE",
    "SLATE_CONTENT_MARKER",
    "calibration_target_name_sql",
    "measurable_species_sql",
    "rigid_target_sql",
]

# --- The literals, as the contracts' taxonomy spells them --------------------

#: Prefix for a physical fish model; the leaf after it is the model's name.
FISH_MODEL_PREFIX = "Fish Model,"

#: `Calibration Targets, <leaf>` -> the name it measures as. An allowlist, not a
#: prefix rule: the branch also holds the E4E Checkerboard, a plane with no
#: length, which the stage-14 cohort must never offer (see the contracts'
#: `MEASURABLE_CALIBRATION_TARGETS`).
MEASURABLE_CALIBRATION_TARGETS: dict[str, str] = {
    "Calibration Targets, Ruler": "Ruler",
    "Calibration Targets, Box": "Box",
}

#: Stage-9 marker: the frame shows the slate with the laser on it. Stores bind
#: it as a parameter (`content_of_image = :marker`).
SLATE_CONTENT_MARKER = "Slate, Laser on slate"

# --- SQL LIKE patterns --------------------------------------------------------

# A real fish carries a `Common Name (Scientific name)` leaf. `(` and `)` are
# not LIKE wildcards, so this reads "contains ( and ends with )".
REAL_FISH_LIKE = "%(%)"
FISH_MODEL_LIKE = f"{FISH_MODEL_PREFIX}%"


def rigid_target_sql(col: str) -> str:
    """SQL for "this row is a fish model or a measurable calibration target".

    The `TRIM(...) <> prefix` half is not decoration. `LIKE 'Fish Model,%'`
    matches the *empty leaf* `"Fish Model,"` — a labeler selecting the parent
    taxonomy node without picking a model — because `%` matches the empty
    string. `parse_model_name` returns None for it, so the measure activity
    skips the image. Cohort says measurable, activity says skip: no
    Measurement is ever written, `NOT EXISTS (measurement)` stays true, and
    the dive is re-selected every hour forever. Exactly the never-goes-false
    wedge that blocked scheduling stage 14 in v1 before 2026-07-17, reachable
    by one labeler mis-click.

    `TRIM` also covers a space-only leaf (`"Fish Model,   "`), which
    `parse_model_name` rejects via the matching `.strip(" ")`. Note SQL
    `TRIM(x)` removes **spaces only**, not all whitespace — which is why
    `parse_model_name` strips spaces only too, rather than calling bare
    `.strip()`.

    The calibration-target half is an `IN` over the literal keys of
    `MEASURABLE_CALIBRATION_TARGETS` rather than a second `LIKE`, because that
    branch is mixed — the checkerboard must stay out.
    """
    targets = ", ".join(f"'{c}'" for c in MEASURABLE_CALIBRATION_TARGETS)
    return (
        f"(({col} LIKE '{FISH_MODEL_LIKE}' "
        f"AND TRIM({col}) <> '{FISH_MODEL_PREFIX}') "
        f"OR {col} IN ({targets}))"
    )


def calibration_target_name_sql(col: str) -> str:
    """SQL for "this reference name is a calibration target".

    v1's `fish_model_species_mislabel_suspects` asks "does this frame's length
    fit some OTHER model better than its own label?", and negates this
    predicate on both sides of that question. A ruler and a box are not
    candidate species labels — nobody mislabels a grouper as a ruler — so
    offering one as the better fit produces a relabel prompt no labeler can act
    on. The box at 0.150 m sits squarely in the band foreshortened frames of
    the ~0.195 m models land in, so without this a correctly-labelled Gray
    Anthias measuring 0.160 m is flagged against the box.

    Deliberately NOT `is_provisional`: that flag means the length is an
    estimate rather than a caliper reading. The reason a target does not belong
    in that search is what it IS, not how well its length is known.
    """
    names = ", ".join(f"'{n}'" for n in MEASURABLE_CALIBRATION_TARGETS.values())
    return f"{col} IN ({names})"


def measurable_species_sql(col: str) -> str:
    """SQL approximation of `is_measurable` — real fish OR rigid target."""
    return f"({col} LIKE '{REAL_FISH_LIKE}' OR {rigid_target_sql(col)})"
