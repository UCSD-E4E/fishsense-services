"""The SQL forms of the `content_of_image` taxonomy predicates.

Ported from fishsense-lite@77e8f8e5: the SQL-builder tests of
libs/fishsense-shared/tests/test_taxonomy.py, and the SQL <-> Python parity tests
of services/fishsense-api/tests/test_dive_pipeline_status_view.py, which ran the
view's predicate over `taxonomy.MEASURABILITY_CORPUS`.

`is_measurable` (in `fishsense_services_contracts.taxonomy`) is the definition
of record: "can the measure activity bind this row to a Measurement". The SQL
here approximates it with `LIKE`, as v1's did. An approximation is fine; an
*unverified* one is how the cohort starts offering images the activity always
skips, which never resolves and re-selects the dive every hour forever. So the
real SQL runs on real Postgres over the same corpus and is compared row by row.

v2 changes, each pinned here:

* the API keeps its own copy of the literals the SQL needs, because the API
  image does not install the contracts package; a test pins the copy to the
  contracts' vocabulary, so the one meaning still lives in one place;
* v1's third parity test (the view's raw SQL vs the cohort's SQLAlchemy
  conditions) has no v2 counterpart: v2's stores write raw SQL, so there is
  one SQL form, not two;
* the rigid-target predicate gets its own parity test: the stage-14 cohort
  waives the cluster gate on it, so it must agree with `parse_model_name`
  exactly, not merely as part of the measurable union;
* the divergence test also probes the unbalanced-paren shapes v1's parser
  tests reject (`OFF_SHAPE_PROBES`). The shared corpus has none, so on its own
  it could not tell `%(%)` from `%(%`: a real-fish pattern that dropped its
  closing paren would pass every Postgres test here.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from fishsense_services_api import taxonomy_sql as sut
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.ingest_store import create_dive, register_capture
from fishsense_services_contracts import taxonomy

T0 = datetime(2026, 4, 10, tzinfo=UTC)

#: Off-shape values v1's `parse_species_names` tests reject, which the SQL must
#: reject too. Not in the shared corpus (kept as v1's, so a v1 change re-ports
#: by diff), but without them nothing on Postgres checks the closing paren.
OFF_SHAPE_PROBES = ("Fish, Hogfish (", "Fish, Hogfish )", "   ")


# --------------------------------------------------------------------
# The literals: the contracts' vocabulary, spelled for SQL
# --------------------------------------------------------------------


def test_the_sql_literals_are_the_contracts_vocabulary():
    """The API image does not install the contracts package, so the SQL keeps
    its own copy of the few literals it renders. This is what stops the copy
    drifting: a taxonomy change made on one side only fails here."""
    assert sut.FISH_MODEL_PREFIX == taxonomy.FISH_MODEL_PREFIX
    assert sut.MEASURABLE_CALIBRATION_TARGETS == taxonomy.MEASURABLE_CALIBRATION_TARGETS
    assert sut.SLATE_CONTENT_MARKER == taxonomy.SLATE_CONTENT_MARKER


def test_no_literal_can_break_out_of_its_sql_string():
    """The builders render the literals inside single quotes, as v1's did. A
    quote in a taxonomy leaf would end the string early, so refuse it here
    rather than in a view definition."""
    literals = [
        sut.FISH_MODEL_PREFIX,
        sut.SLATE_CONTENT_MARKER,
        *sut.MEASURABLE_CALIBRATION_TARGETS,
        *sut.MEASURABLE_CALIBRATION_TARGETS.values(),
    ]
    assert not [lit for lit in literals if "'" in lit]


# --------------------------------------------------------------------
# SQL fragment builders
# --------------------------------------------------------------------


def test_rigid_target_sql_excludes_the_empty_leaf():
    """The guard that stops a labeler mis-click wedging the stage-14 cohort:
    `LIKE 'Fish Model,%'` alone matches `"Fish Model,"`, which
    `parse_model_name` rejects."""
    sql = sut.rigid_target_sql("sl.content_of_image")
    assert "LIKE 'Fish Model,%'" in sql
    assert "TRIM(sl.content_of_image) <> 'Fish Model,'" in sql
    assert "'Calibration Targets, Ruler'" in sql
    assert "'Calibration Targets, Box'" in sql


def test_rigid_target_sql_leaves_the_unmeasurable_calibration_targets_out():
    """The checkerboard shares the branch and must not be swept in — a row the
    cohort offers and `parse_model_name` rejects is the never-drains wedge."""
    sql = sut.rigid_target_sql("sl.content_of_image")

    assert "E4E Checkerboard" not in sql


def test_measurable_species_sql_is_real_fish_or_rigid_target():
    sql = sut.measurable_species_sql("sl.content_of_image")
    assert "LIKE '%(%)'" in sql
    assert sut.rigid_target_sql("sl.content_of_image") in sql


def test_sql_builders_take_the_column_name():
    """The view aliases specieslabel as `sl`; a caller with a different alias
    must not have to string-replace."""
    assert "x.content" in sut.measurable_species_sql("x.content")
    assert "sl.content_of_image" not in sut.measurable_species_sql("x.content")


def test_calibration_target_name_sql_lists_every_target_name():
    """The mislabel view uses this to keep calibration targets out of the
    "which model is this really?" search.

    They are not candidate species labels: nobody mislabels a grouper as a
    ruler. Before the box existed the smallest reference was 0.192 m, so no
    calibration target sat in the band foreshortened frames land in; the box
    at 0.15 m does, which is what made this predicate necessary rather than
    merely tidy.
    """
    sql = sut.calibration_target_name_sql("r.name")

    assert "'Ruler'" in sql
    assert "'Box'" in sql
    assert "r.name" in sql


def test_calibration_target_name_sql_names_no_fish_models():
    """It must select the targets and nothing else — a fish model swept in
    here would silently stop being offered as an alternative label."""
    sql = sut.calibration_target_name_sql("r.name")

    for model in taxonomy.LABELED_FISH_MODELS:
        assert f"'{model}'" not in sql


# --------------------------------------------------------------------
# SQL <-> Python parity, on real Postgres
# --------------------------------------------------------------------


async def _species_labels(owner_engine, app_engine, contents) -> tuple[uuid.UUID, dict]:
    """One species label per value, on one capture, as the stores see them:
    through the app role, under RLS. Returns the tenant and {project: value}."""
    async with owner_engine.begin() as conn:
        tenant = (
            await conn.execute(
                text("INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id"),
                {"s": "lab"},
            )
        ).scalar_one()
    by_project = {}
    async with tenant_transaction(app_engine, tenant) as conn:
        dive = await create_dive(conn, tenant, source_path="d", name="d", dived_at=T0)
        capture = (
            await register_capture(
                conn, tenant, dive_id=dive, device_id=None,
                source_path="d/P.ORF", captured_at=T0, checksum=uuid.uuid4().hex,
            )  # fmt: skip
        ).capture_id
        # A project per row: a capture holds one label per project.
        for project, content in enumerate(contents, start=1):
            await conn.execute(
                text(
                    "INSERT INTO species_labels (tenant_id, capture_id, source, "
                    "ls_project_id, content_of_image) "
                    "VALUES (:t, :c, 'human', :p, :content)"
                ),
                {"t": tenant, "c": capture, "p": project, "content": content},
            )
            by_project[project] = content
    return tenant, by_project


async def _matched(app_engine, tenant, predicate: str) -> set[int]:
    async with tenant_transaction(app_engine, tenant) as conn:
        rows = await conn.execute(
            text(f"SELECT sl.ls_project_id FROM species_labels sl WHERE {predicate}")
        )
        return {row[0] for row in rows}


async def test_measurable_species_sql_agrees_with_taxonomy_is_measurable(
    owner_engine, app_engine
):
    """The one predicate that exists in two languages must mean one thing.

    `taxonomy.is_measurable` is the definition of record — it is literally
    "can the measure activity bind this row to a Measurement". The SQL is a
    `LIKE`-based approximation, and both are run over the same corpus and
    compared row by row.
    """
    corpus = taxonomy.MEASURABILITY_CORPUS
    tenant, by_project = await _species_labels(
        owner_engine, app_engine, [content for content, _ in corpus]
    )

    matched_by_sql = await _matched(
        app_engine, tenant, sut.measurable_species_sql("sl.content_of_image")
    )

    expected = {
        project for project, (_, measurable) in enumerate(corpus, start=1) if measurable
    }
    assert (
        matched_by_sql == expected
    ), "SQL and taxonomy.is_measurable disagree on: " + repr(
        sorted(by_project[p] for p in matched_by_sql ^ expected)
    )


async def test_sql_is_broader_than_python_only_where_pinned(owner_engine, app_engine):
    """A *new* SQL/Python divergence must fail the build.

    The dangerous direction is SQL-broader: the cohort offers an image the
    activity skips, no Measurement is written, and the dive is re-selected
    every hour forever. `taxonomy.SQL_BROADER_THAN_PYTHON` is the pinned,
    known-unreachable set (the empty-name guard the LIKE patterns can't
    express). This asserts the real SQL matches exactly those and no others,
    so widening the predicate — or tightening the Python parser, which is how
    this regressed once already — is caught here rather than in prod.
    """
    probes = [c for c, _ in taxonomy.MEASURABILITY_CORPUS if c is not None]
    probes += list(taxonomy.SQL_BROADER_THAN_PYTHON)
    probes += list(OFF_SHAPE_PROBES)
    tenant, by_project = await _species_labels(owner_engine, app_engine, probes)

    matched_by_sql = await _matched(
        app_engine, tenant, sut.measurable_species_sql("sl.content_of_image")
    )
    sql_broader = {
        by_project[p]
        for p in matched_by_sql
        if not taxonomy.is_measurable(by_project[p])
    }
    assert sql_broader == set(taxonomy.SQL_BROADER_THAN_PYTHON)

    # And the reverse direction — Python measurable, SQL not — must be empty:
    # that would silently under-measure a dive while reporting it complete.
    python_broader = {
        content
        for p, content in by_project.items()
        if taxonomy.is_measurable(content) and p not in matched_by_sql
    }
    assert python_broader == set()


async def test_rigid_target_sql_agrees_with_parse_model_name(owner_engine, app_engine):
    """v2: the rigid half checked on its own.

    The stage-14 cohort waives the cluster requirement for a rigid target (a
    model, the ruler, the box carry no grouping labels), keyed on this
    predicate, while the activity branches on `parse_model_name`. If the two
    disagreed, a real fish could skip the cluster gate or a model could wait
    on a cluster that never comes. The pinned divergence is all real-fish
    shapes, so here the agreement is exact.
    """
    probes = [c for c, _ in taxonomy.MEASURABILITY_CORPUS]
    probes += list(taxonomy.SQL_BROADER_THAN_PYTHON)
    tenant, by_project = await _species_labels(owner_engine, app_engine, probes)

    matched_by_sql = await _matched(
        app_engine, tenant, sut.rigid_target_sql("sl.content_of_image")
    )

    assert matched_by_sql == {
        p for p, content in by_project.items() if taxonomy.parse_model_name(content)
    }


@pytest.mark.parametrize(
    ("name", "is_target"),
    [
        *[(n, True) for n in taxonomy.MEASURABLE_CALIBRATION_TARGETS.values()],
        *[(m, False) for m in taxonomy.LABELED_FISH_MODELS],
    ],
)
async def test_calibration_target_name_sql_on_postgres(app_engine, name, is_target):
    """The mislabel view negates this over reference names; on Postgres it
    must select the calibration targets and no labeled fish model."""
    async with app_engine.connect() as conn:
        selected = (
            await conn.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM (VALUES (CAST(:n AS text))) AS r(name) "
                    f"WHERE {sut.calibration_target_name_sql('r.name')})"
                ),
                {"n": name},
            )
        ).scalar_one()
    assert selected is is_target
