# pylint: disable=too-many-lines
"""The `dive_pipeline_status` view: one row per dive, v1's shape, v2's cohorts.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api/tests/
test_dive_pipeline_status_view.py (60 tests; names, cases and reasons are
v1's) and tests_support/stage14_fixtures.py, on real Postgres under RLS as the
app role, seeded as the schema owner. v1's rules, kept and pinned here:

* one row per dive, with no WHERE: a duplicate dive has a row too;
* "complete" is never vacuous: zero rows of a kind reads False, and every
  `*_preprocessed` flag needs at least one qualifying capture;
* every capture correlation is gated on `is_canonical`, the predicate the
  cohorts use (half of v1's image rows are duplicates);
* a sentinel (no Label Studio project) is not preprocessed work;
* `measured` counts a capture only when it is measured under the calibration
  the dive resolves to today.

v2 changes, each pinned here:

* `dive_id` is the dive's `number` (v1's id for a migrated dive), `priority`
  is upper-cased (v1's enum names; the Superset SQL filters `'HIGH'`), and
  `dive_slate_id` is the template's number;
* **each stage column is v2's cohort's**, not a v1 restatement: the "done"
  flags negate the stage's work predicate (the store's named SQL), and a
  `*_pending` column per stage is exactly its selector's cohort. The parity
  tests at the end iterate every selector over a seeded corpus and compare;
* divergences from v1's column, where v2's cohort differs: a laser sentinel is
  not preprocessed and a flagged frame is not (0.1); a superseded head/tail
  row is not done and a flagged one is not (5.1); a completed species sentinel
  is done and a flagged frame is not (2); a superseded slate marker or slate
  label does not count (9); `calibrated` is the *effective* calibration (a
  refused or implausible fit is none, 0018); `measured` is 0026's
  `measurement_work` over `current_measurements` (§9.13: a stale binding is
  not current, and a refusal of the very inputs is not work);
* the `*_pending` columns carry the terms v2's cohorts add over v1's (a
  pinhole camera for 0.1, prediction, 2 and 5.1; a resolvable template for
  9; a camera and template for 13), so a dive can read "not preprocessed" and
  not pending: it is blocked, not queued.

v1's three taxonomy parity tests live in tests/test_taxonomy_sql.py (the SQL
vs `is_measurable`, and SQL_BROADER_THAN_PYTHON), and `measurement_work`'s
use of them in tests/test_depth_measure_schema.py; v1's third (the view's raw
SQL vs the cohort's SQLAlchemy) has no v2 counterpart -- there is one SQL form.
"""

from __future__ import annotations

import hashlib
import importlib
import itertools
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    REAL_FISH,
    calibrate,
    capture,
    cluster,
    depth,
    exec_,
    fish,
    forget_identities,
    measurable_capture,
    measurement,
)
from fishsense_services_api import (
    clustering_store,
    headtail_store,
    laser_calibration_store,
    laser_depth_store,
    laser_store,
    measurement_store,
    slate_store,
    species_store,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_contracts import headtail as headtail_contract

VIEW = "dive_pipeline_status"


def _migration():
    return importlib.import_module(
        "fishsense_services_api.migrations.versions."
        "pipeline_status_01_dive_pipeline_status"
    )


K = [[3000.0, 0.0, 2048.0], [0.0, 3000.0, 1536.0], [0.0, 0.0, 1.0]]
TEMPLATE_POINTS = [[0.0, 0.0], [2400.0, 0.0], [0.0, 3000.0], [2400.0, 3000.0]]
SLATE_MARKER = "Slate, Laser on slate"
T0 = datetime(2026, 9, 1, tzinfo=UTC)

_n = itertools.count(1)

#: v1's boolean columns, in v1's order.
V1_FLAGS = (
    "laser_preprocessed",
    "laser_labeling_complete",
    "headtail_preprocessed",
    "headtail_labeling_complete",
    "has_prediction_clusters",
    "dive_images_preprocessed",
    "species_labeling_complete",
    "slate_applicable",
    "slate_preprocessed",
    "slate_labeling_complete",
    "calibrated",
    "measured",
)
#: v2's columns: one per selector, true exactly when it would pick the dive.
PENDING = (
    "laser_preprocess_pending",
    "laser_prediction_pending",
    "clustering_pending",
    "species_preprocess_pending",
    "headtail_preprocess_pending",
    "headtail_prediction_pending",
    "slate_preprocess_pending",
    "laser_calibration_pending",
    "checkerboard_calibration_pending",
    "laser_depth_pending",
    "measurement_pending",
)


# --- seeding, as the schema owner ------------------------------------------------


@dataclass(frozen=True)
class Dive:
    id: uuid.UUID
    number: int


async def _one(owner_engine, sql: str, **params):
    async with owner_engine.begin() as conn:
        return (await conn.execute(text(sql), params)).one()


async def _tenant(owner_engine, slug: str = "lab") -> uuid.UUID:
    return (
        await _one(
            owner_engine,
            "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id",
            s=slug,
        )
    ).id


async def _device(owner_engine, tenant_id, *, model="pinhole", camera=True):
    device_id = (
        await _one(
            owner_engine,
            "INSERT INTO devices (tenant_id, kind, serial) VALUES (:t, 'lite', :s) "
            "RETURNING id",
            t=tenant_id,
            s=f"TG6-{next(_n)}",
        )
    ).id
    if camera:
        await exec_(
            owner_engine,
            "INSERT INTO camera_calibrations (tenant_id, device_id, camera_model, "
            "port_model, camera_matrix, distortion_coefficients) "
            "VALUES (:t, :d, :m, :p, :k, '[0, 0, 0, 0, 0]')",
            t=tenant_id,
            d=device_id,
            m=model,
            p=None if model == "pinhole" else "flat",
            k=json.dumps(K),
        )
    return device_id


async def _dive(
    owner_engine,
    tenant_id,
    *,
    priority="high",
    name=None,
    device_id=None,
    slate_template_id=None,
    calibration_target_id=None,
    source_dive=None,
) -> Dive:
    n = next(_n)
    row = await _one(
        owner_engine,
        "INSERT INTO dives (tenant_id, source_path, name, dived_at, priority, "
        "device_id, slate_template_id, calibration_target_id, "
        "calibration_source_dive_id, created_at) VALUES (:t, :p, :name, :at, "
        ":priority, :device, :slate, :target, :src, :created) RETURNING id, number",
        t=tenant_id,
        p=f"/dives/{n}",
        name=name,
        at=T0,
        priority=priority,
        device=device_id,
        slate=slate_template_id,
        target=calibration_target_id,
        src=None if source_dive is None else source_dive.id,
        created=T0 + timedelta(seconds=n),
    )
    return Dive(row.id, row.number)


async def _camera_dive(owner_engine, tenant_id, *, model="pinhole", **kwargs) -> Dive:
    """A dive whose device has a current camera calibration."""
    device_id = await _device(owner_engine, tenant_id, model=model)
    return await _dive(owner_engine, tenant_id, device_id=device_id, **kwargs)


async def _capture(owner_engine, tenant_id, dive: Dive, *, canonical=True):
    return await capture(owner_engine, tenant_id, dive.id, canonical=canonical)


async def _label(
    owner_engine,
    table: str,
    tenant_id,
    capture_id,
    *,
    sentinel=False,
    completed=False,
    superseded=False,
    needs_reprocess=False,
    **columns,
):
    """A label row of any kind. A sentinel has no Label Studio project."""
    names = ", ".join(columns)
    values = ", ".join(f":{c}" for c in columns)
    return (
        await _one(
            owner_engine,
            f"INSERT INTO {table} (tenant_id, capture_id, source, ls_project_id, "
            f"completed, superseded, needs_reprocess{', ' if columns else ''}{names}) "
            f"VALUES (:t, :c, :source, :p, :done, :gone, :flag"
            f"{', ' if columns else ''}{values}) RETURNING id",
            t=tenant_id,
            c=capture_id,
            source="import" if sentinel else "human",
            p=None if sentinel else next(_n),
            done=completed,
            gone=superseded,
            flag=needs_reprocess,
            **columns,
        )
    ).id


async def _laser(owner_engine, tenant_id, capture_id, *, x=100.0, y=200.0, **kw):
    return await _label(
        owner_engine, "laser_labels", tenant_id, capture_id, x=x, y=y, **kw
    )


async def _valid_laser(owner_engine, tenant_id, capture_id):
    return await _laser(owner_engine, tenant_id, capture_id, completed=True)


async def _head_tail(owner_engine, tenant_id, capture_id, **kw):
    return await _label(owner_engine, "head_tail_labels", tenant_id, capture_id, **kw)


async def _species(owner_engine, tenant_id, capture_id, content=None, **kw):
    return await _label(
        owner_engine,
        "species_labels",
        tenant_id,
        capture_id,
        content_of_image=content,
        **kw,
    )


async def _slate_label(owner_engine, tenant_id, capture_id, **kw):
    return await _label(owner_engine, "slate_labels", tenant_id, capture_id, **kw)


async def _slate_template(owner_engine, *, dpi=300, source_path="slates/H.pdf"):
    return (
        await _one(
            owner_engine,
            "INSERT INTO slate_templates (name, dpi, source_path, reference_points) "
            "VALUES (:n, :dpi, :p, :r) RETURNING id",
            n=f"H-Slate {uuid.uuid4()}",
            dpi=dpi,
            p=source_path,
            r=json.dumps(TEMPLATE_POINTS),
        )
    ).id


async def _calibration_target(owner_engine):
    return (
        await _one(
            owner_engine,
            "INSERT INTO calibration_targets (name, interior_rows, interior_cols, "
            "pitch_x_m, pitch_y_m) VALUES (:n, 10, 14, 0.042, 0.042) RETURNING id",
            n=f"E4E Checkerboard {uuid.uuid4()}",
        )
    ).id


async def _laser_prediction(owner_engine, tenant_id, capture_id, *, version):
    await exec_(
        owner_engine,
        "INSERT INTO laser_predictions (tenant_id, capture_id, x, y, "
        "predictor_version) VALUES (:t, :c, 10, 20, :v)",
        t=tenant_id,
        c=capture_id,
        v=version,
    )


async def _head_tail_prediction(owner_engine, tenant_id, capture_id, *, version):
    await exec_(
        owner_engine,
        "INSERT INTO head_tail_predictions (tenant_id, capture_id, status, "
        "predictor_version) VALUES (:t, :c, 'no_detections', :v)",
        t=tenant_id,
        c=capture_id,
        v=version,
    )


async def _slate_observation(owner_engine, tenant_id, dive: Dive):
    """A canonical frame stage 13 fits on: a completed live slate label and a
    live laser dot."""
    capture_id = await _capture(owner_engine, tenant_id, dive)
    await _slate_label(owner_engine, tenant_id, capture_id, completed=True)
    await _laser(owner_engine, tenant_id, capture_id)
    return capture_id


# --- reading, as the app role under RLS ------------------------------------------


async def _row(app_engine, tenant_id, dive: Dive) -> dict:
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return dict(
            (
                await conn.execute(
                    text(f"SELECT * FROM {VIEW} WHERE dive_id = :n"),
                    {"n": dive.number},
                )
            )
            .mappings()
            .one()
        )


async def _pending(app_engine, tenant_id, column: str) -> set[int]:
    async with tenant_transaction(app_engine, tenant_id) as conn:
        rows = await conn.execute(text(f"SELECT dive_id FROM {VIEW} WHERE {column}"))
        return set(rows.scalars())


async def _selected(owner_engine, app_engine, tenant_id, select) -> set[int]:
    """Every dive the selector would pick, in turn: it returns the oldest, so
    each pick is taken out of the cohort (priority low) and it is asked again.
    Priorities are restored after."""
    picked: list[uuid.UUID] = []
    try:
        while True:
            async with tenant_transaction(app_engine, tenant_id) as conn:
                found = await select(conn, tenant_id)
            if found is None:
                break
            assert found.dive_id not in picked, "a selector picked a low dive"
            picked.append(found.dive_id)
            await exec_(
                owner_engine,
                "UPDATE dives SET priority = 'low' WHERE id = :d",
                d=found.dive_id,
            )
    finally:
        if picked:
            await exec_(
                owner_engine,
                "UPDATE dives SET priority = 'high' WHERE id = ANY(:ds)",
                ds=picked,
            )
    if not picked:
        return set()
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT number FROM dives WHERE id = ANY(:ds)"), {"ds": picked}
        )
        return set(rows.scalars())


# --- the view's definition -------------------------------------------------------


async def test_the_view_runs_as_its_caller(owner_engine):
    """security_invoker, or the view would read every tenant's rows as its
    owner (0006; the schema audit enforces it)."""
    async with owner_engine.connect() as conn:
        options = (
            await conn.execute(
                text("SELECT reloptions FROM pg_class WHERE relname = :v"), {"v": VIEW}
            )
        ).scalar_one()
    assert "security_invoker=true" in options


async def test_v1s_columns_come_first_in_v1s_order(owner_engine):
    """Superset's datasets and the portal read v1's column names; v2's
    `*_pending` columns are appended after them."""
    async with owner_engine.connect() as conn:
        columns = list(
            (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = :v ORDER BY ordinal_position"
                    ),
                    {"v": VIEW},
                )
            ).scalars()
        )
    v1 = ["dive_id", "dive_name", "priority", "dive_slate_id", *V1_FLAGS]
    v1.insert(v1.index("measured"), "calibration_source")
    assert columns == v1 + list(PENDING)


def test_the_predictor_versions_frozen_in_the_view_are_the_stages():
    """A view is frozen when it is created, so the predictor versions its
    prediction columns compare against are spelled in the migration. A version
    bump must ship a migration that recreates the view; this is what says so."""
    migration = _migration()
    assert migration.LASER_PREDICTOR_VERSION == laser_store.LASER_PREDICTOR_VERSION
    assert (
        migration.HEADTAIL_PREDICTOR_VERSION
        == headtail_contract.HEADTAIL_PREDICTOR_VERSION
    )


def test_the_view_is_what_todays_cohorts_render():
    """The migration renders the view's rows (`dive_pipeline_status_rows()`)
    from the stores' named cohort SQL, and Postgres freezes what it rendered.
    If a store's predicate changes, the function in every existing database
    still holds the old one, and the parity tests below (which run on a fresh
    database) cannot see it. This pins the rendering: when it fails, ship a
    migration that recreates the function from today's predicates, then move
    this pin to it."""
    migration = _migration()
    rendered = hashlib.sha256(migration.FUNCTION_SQL.encode()).hexdigest()
    assert rendered == migration.RENDERED_SHA256


# --- what it costs: measured on the production rehearsal (525 dives, 134k
# captures), where a first cut took 35 s a read and Superset reads it five
# times per chart. Each of these took a piece of that away.


async def _indexes(owner_engine, table: str) -> set[str]:
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE tablename = :t"), {"t": table}
        )
        return {r.split(" USING ", 1)[1] for r in rows.scalars()}


async def test_a_dives_captures_are_found_by_index(owner_engine):
    """Every column asks about the dive's canonical captures, and so does
    every cohort. Without an index on the dive each asked by scanning all of
    `captures`: 8 s for `laser_preprocessed` alone, 0.1 s with it."""
    assert "btree (tenant_id, dive_id)" in await _indexes(owner_engine, "captures")


async def test_a_captures_current_depth_is_found_by_index(owner_engine):
    """`laser_depth_work` asked "is the capture's current depth still
    good?" as an anti join against all of `current_laser_depths`, which
    Postgres rescanned per capture: 16 s for the depth column, as 0028 found
    for `current_measurements`. The work now looks the depth up per capture,
    and the view's `DISTINCT ON` leads with the tenant so the lookup reaches
    this index (77 ms)."""
    assert "btree (tenant_id, capture_id)" in await _indexes(
        owner_engine, "laser_depths"
    )
    async with owner_engine.connect() as conn:
        definition = (
            await conn.execute(text("SELECT pg_get_viewdef('current_laser_depths')"))
        ).scalar_one()
    assert "DISTINCT ON (tenant_id, capture_id)" in definition


async def test_the_rows_are_computed_without_jit(owner_engine):
    """The plan is a few hundred subplans, so its estimated cost is far past
    `jit_above_cost`, and Postgres spent 12-17 s compiling it for a 2 s read.
    The rows come from a function that turns JIT off for itself (a view
    cannot carry a setting); it runs as its caller, so RLS still holds."""
    async with owner_engine.connect() as conn:
        function = (
            await conn.execute(
                text(
                    "SELECT p.proconfig, p.prosecdef FROM pg_proc p "
                    "JOIN pg_depend dep ON dep.refobjid = p.oid "
                    "JOIN pg_rewrite rw ON rw.oid = dep.objid "
                    "WHERE rw.ev_class = CAST(:v AS regclass) AND p.proname = :f"
                ),
                {"v": VIEW, "f": f"{VIEW}_rows"},
            )
        ).one()
    assert function.proconfig == ["jit=off"]
    assert function.prosecdef is False, "it must run as its caller, under RLS"


# --- baseline / identity ---------------------------------------------------------


async def test_empty_dive_emits_a_row_with_every_flag_false(owner_engine, app_engine):
    """Edge: dive with zero images. Every flag must be False, not vacuously
    True via empty subqueries -- v2's pending columns too."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)

    row = await _row(app_engine, tenant, dive)
    for column in (*V1_FLAGS, *PENDING):
        assert row[column] is False, f"{column} should be False for an empty dive"
    assert row["calibration_source"] == "none"


async def test_identity_columns_pass_through(owner_engine, app_engine):
    """v2: dive_id is the dive's number (v1's id for a migrated dive), the
    priority is v1's upper-case enum name, and dive_slate_id is the template's
    number (v1's diveslate id)."""
    tenant = await _tenant(owner_engine)
    template = await _slate_template(owner_engine)
    dive = await _dive(
        owner_engine,
        tenant,
        priority="low",
        slate_template_id=template,
        name="083023_FishModels_FSL05",
    )
    async with owner_engine.connect() as conn:
        template_number = (
            await conn.execute(
                text("SELECT number FROM slate_templates WHERE id = :t"),
                {"t": template},
            )
        ).scalar_one()

    row = await _row(app_engine, tenant, dive)
    assert row["dive_id"] == dive.number
    # Superset dashboards key on dive_id but display the readable name.
    assert row["dive_name"] == "083023_FishModels_FSL05"
    assert row["priority"] == "LOW"
    assert row["dive_slate_id"] == template_number


async def test_a_migrated_dive_keeps_v1s_id(owner_engine, app_engine):
    """Research and the portal join on v1's dive id: a migrated dive's number
    is its v1 id (0019), so the view shows v1's id."""
    tenant = await _tenant(owner_engine)
    await exec_(
        owner_engine,
        "INSERT INTO dives (tenant_id, v1_id, source_path, dived_at) "
        "VALUES (:t, 90417, '/v1/dive', :at)",
        t=tenant,
        at=T0,
    )
    async with tenant_transaction(app_engine, tenant) as conn:
        ids = list((await conn.execute(text(f"SELECT dive_id FROM {VIEW}"))).scalars())
    assert ids == [90417]


@pytest.mark.parametrize(("stored", "shown"), [("high", "HIGH"), ("none", "NONE")])
async def test_priority_is_v1s_upper_case_name(owner_engine, app_engine, stored, shown):
    """v2 stores the priority lower-case; the imported Superset datasets filter
    `priority = 'HIGH'` and break silently on any other spelling."""
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant, priority=stored)

    assert (await _row(app_engine, tenant, dive))["priority"] == shown


async def test_dive_name_null_passes_through_as_none(owner_engine, app_engine):
    """A dive with no name (NULL) still emits a row; dive_name is None."""
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant, name=None)

    row = await _row(app_engine, tenant, dive)
    assert row["dive_id"] == dive.number
    assert row["dive_name"] is None


async def test_every_dive_has_a_row_and_only_the_tenants(owner_engine, app_engine):
    """v1: FROM dive with no WHERE, so a duplicate dive has a row too. v2: the
    view runs as its caller, so RLS shows one tenant's dives only."""
    lab = await _tenant(owner_engine)
    partner = await _tenant(owner_engine, "partner")
    mine = {(await _dive(owner_engine, lab)).number for _ in range(3)}
    await _dive(owner_engine, partner)

    async with tenant_transaction(app_engine, lab) as conn:
        shown = set((await conn.execute(text(f"SELECT dive_id FROM {VIEW}"))).scalars())
    assert shown == mine


# --- laser_preprocessed (stage 0.1) ----------------------------------------------


async def test_laser_preprocessed_true_when_every_image_has_laser_row(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    for _ in range(2):
        await _laser(owner_engine, tenant, await _capture(owner_engine, tenant, dive))

    assert (await _row(app_engine, tenant, dive))["laser_preprocessed"] is True


async def test_laser_preprocessed_false_when_one_image_lacks_label(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    await _laser(owner_engine, tenant, await _capture(owner_engine, tenant, dive))
    await _capture(owner_engine, tenant, dive)

    row = await _row(app_engine, tenant, dive)
    assert row["laser_preprocessed"] is False
    assert row["laser_preprocess_pending"] is True


async def test_laser_preprocessed_ignores_a_sentinel(owner_engine, app_engine):
    """v2 (the stage-0.1 cohort's `NEEDS_LASER_JPEG`): a project-less sentinel
    is not a laser task, so its frame still needs its JPEG. v1's view counted
    any laserlabel row."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _laser(owner_engine, tenant, capture_id, sentinel=True)

    assert (await _row(app_engine, tenant, dive))["laser_preprocessed"] is False


async def test_laser_preprocessed_false_while_a_frame_is_flagged(
    owner_engine, app_engine
):
    """v2 (the cohort's second way in): a live label flagged needs_reprocess
    is a redraw stage 0.1 still owes."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _laser(owner_engine, tenant, capture_id, needs_reprocess=True)

    row = await _row(app_engine, tenant, dive)
    assert row["laser_preprocessed"] is False
    assert row["laser_preprocess_pending"] is True


async def test_laser_preprocessed_ignores_duplicate_captures(owner_engine, app_engine):
    """is_canonical gating: a duplicate frame of another dive is never stage
    0.1's work, so its missing label must not hold the dive back."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    await _laser(owner_engine, tenant, await _capture(owner_engine, tenant, dive))
    await _capture(owner_engine, tenant, dive, canonical=False)

    assert (await _row(app_engine, tenant, dive))["laser_preprocessed"] is True


async def test_laser_preprocessed_false_for_a_dive_of_duplicates_only(
    owner_engine, app_engine
):
    """Never vacuous: a dive whose every frame is a duplicate has nothing to
    preprocess, and reads False (v1: 207 of 479 dives)."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    await _laser(
        owner_engine,
        tenant,
        await _capture(owner_engine, tenant, dive, canonical=False),
    )

    assert (await _row(app_engine, tenant, dive))["laser_preprocessed"] is False


async def test_an_unrectifiable_dive_is_not_preprocessed_and_not_pending(
    owner_engine, app_engine
):
    """v2 (`camera_sql.RECTIFIABLE_DIVE`): stage 0.1 rectifies with pinhole
    maths, so a flat-port camera's dive is not offered. It reads
    not-preprocessed and not-pending: blocked, not queued."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant, model="axial_refractive")
    await _capture(owner_engine, tenant, dive)

    row = await _row(app_engine, tenant, dive)
    assert (row["laser_preprocessed"], row["laser_preprocess_pending"]) == (
        False,
        False,
    )


# --- laser_labeling_complete ------------------------------------------------------


async def test_laser_labeling_complete_true_when_all_completed_and_none_incomplete(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    for _ in range(2):
        await _valid_laser(
            owner_engine, tenant, await _capture(owner_engine, tenant, dive)
        )

    assert (await _row(app_engine, tenant, dive))["laser_labeling_complete"] is True


async def test_laser_labeling_complete_false_when_zero_labels(owner_engine, app_engine):
    """Vacuous-truth guard: zero labels must NOT read as complete."""
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    await _capture(owner_engine, tenant, dive)

    assert (await _row(app_engine, tenant, dive))["laser_labeling_complete"] is False


async def test_laser_labeling_complete_false_when_any_incomplete(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    await _valid_laser(owner_engine, tenant, await _capture(owner_engine, tenant, dive))
    await _laser(owner_engine, tenant, await _capture(owner_engine, tenant, dive))

    assert (await _row(app_engine, tenant, dive))["laser_labeling_complete"] is False


async def test_laser_labeling_complete_ignores_superseded_incomplete(
    owner_engine, app_engine
):
    """Superseded incomplete rows are dead; they must not block completion.
    Mirrors the laser-validate flow's behavior."""
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _valid_laser(owner_engine, tenant, capture_id)
    await _laser(owner_engine, tenant, capture_id, superseded=True)

    assert (await _row(app_engine, tenant, dive))["laser_labeling_complete"] is True


async def test_laser_labeling_complete_ignores_duplicate_captures(
    owner_engine, app_engine
):
    """is_canonical gating, as v1's view gated it: an incomplete label on a
    duplicate frame does not hold the dive's labeling open."""
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    await _valid_laser(owner_engine, tenant, await _capture(owner_engine, tenant, dive))
    await _laser(
        owner_engine,
        tenant,
        await _capture(owner_engine, tenant, dive, canonical=False),
    )

    assert (await _row(app_engine, tenant, dive))["laser_labeling_complete"] is True


# --- headtail_preprocessed (stage 5.1) --------------------------------------------


async def test_headtail_preprocessed_true_when_every_valid_laser_image_has_headtail(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _valid_laser(owner_engine, tenant, capture_id)
    await _head_tail(owner_engine, tenant, capture_id)

    assert (await _row(app_engine, tenant, dive))["headtail_preprocessed"] is True


async def test_headtail_preprocessed_false_when_no_valid_laser_images(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    # Laser is incomplete -> no valid lasers in the dive -> nothing to
    # preprocess yet.
    await _laser(owner_engine, tenant, await _capture(owner_engine, tenant, dive))

    assert (await _row(app_engine, tenant, dive))["headtail_preprocessed"] is False


async def test_headtail_preprocessed_false_when_valid_laser_image_lacks_headtail(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    await _valid_laser(owner_engine, tenant, await _capture(owner_engine, tenant, dive))

    row = await _row(app_engine, tenant, dive)
    assert row["headtail_preprocessed"] is False
    assert row["headtail_preprocess_pending"] is True


async def test_headtail_preprocessed_ignores_sentinel_headtail_rows(
    owner_engine, app_engine
):
    """A sentinel HeadTailLabel (label_studio_project_id NULL) does NOT count
    as preprocessed -- matches the cohort selector."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _valid_laser(owner_engine, tenant, capture_id)
    await _head_tail(owner_engine, tenant, capture_id, sentinel=True)

    assert (await _row(app_engine, tenant, dive))["headtail_preprocessed"] is False


async def test_headtail_preprocessed_ignores_a_superseded_headtail_row(
    owner_engine, app_engine
):
    """v2 (the stage-5.1 cohort): a dead-lettered row is not done work."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _valid_laser(owner_engine, tenant, capture_id)
    await _head_tail(owner_engine, tenant, capture_id, superseded=True)

    assert (await _row(app_engine, tenant, dive))["headtail_preprocessed"] is False


async def test_headtail_preprocessed_false_while_a_frame_is_flagged(
    owner_engine, app_engine
):
    """v2 (the cohort's second way in): a flagged live row is a redraw owed."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _valid_laser(owner_engine, tenant, capture_id)
    await _head_tail(owner_engine, tenant, capture_id, needs_reprocess=True)

    assert (await _row(app_engine, tenant, dive))["headtail_preprocessed"] is False


# --- headtail_labeling_complete ----------------------------------------------------


async def test_headtail_labeling_complete_true_when_all_completed_and_none_incomplete(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _head_tail(owner_engine, tenant, capture_id, completed=True)

    assert (await _row(app_engine, tenant, dive))["headtail_labeling_complete"] is True


async def test_headtail_labeling_complete_false_when_any_incomplete(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    await _head_tail(
        owner_engine,
        tenant,
        await _capture(owner_engine, tenant, dive),
        completed=True,
    )
    await _head_tail(owner_engine, tenant, await _capture(owner_engine, tenant, dive))

    row = await _row(app_engine, tenant, dive)
    assert row["headtail_labeling_complete"] is False


async def test_headtail_labeling_complete_false_when_zero_labels(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)

    row = await _row(app_engine, tenant, dive)
    assert row["headtail_labeling_complete"] is False


async def test_species_labeling_complete_ignores_superseded(owner_engine, app_engine):
    """A superseded incomplete species row must not block completion."""
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    await _species(
        owner_engine, tenant, await _capture(owner_engine, tenant, dive), completed=True
    )
    await _species(
        owner_engine,
        tenant,
        await _capture(owner_engine, tenant, dive),
        superseded=True,
    )

    assert (await _row(app_engine, tenant, dive))["species_labeling_complete"] is True


async def test_slate_labeling_complete_ignores_superseded(owner_engine, app_engine):
    """A superseded incomplete slate row must not block completion."""
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    await _slate_label(
        owner_engine, tenant, await _capture(owner_engine, tenant, dive), completed=True
    )
    await _slate_label(
        owner_engine,
        tenant,
        await _capture(owner_engine, tenant, dive),
        superseded=True,
    )

    assert (await _row(app_engine, tenant, dive))["slate_labeling_complete"] is True


# --- has_prediction_clusters / dive_images_preprocessed (stage 2) --------------------


async def test_has_prediction_clusters_reflects_data_source(owner_engine, app_engine):
    tenant = await _tenant(owner_engine)
    predicted = await _dive(owner_engine, tenant)
    regrouped = await _dive(owner_engine, tenant)
    await cluster(owner_engine, tenant, predicted.id, formed_by="prediction")
    await cluster(owner_engine, tenant, regrouped.id, formed_by="label_studio")

    assert (await _row(app_engine, tenant, predicted))["has_prediction_clusters"]
    # Only Label Studio clusters -> stage 1 hasn't run.
    assert not (await _row(app_engine, tenant, regrouped))["has_prediction_clusters"]


async def _stage_2_frame(
    owner_engine, tenant, dive, *, valid=True, clustered=True, species=True
):
    capture_id = await _capture(owner_engine, tenant, dive)
    if valid:
        await _valid_laser(owner_engine, tenant, capture_id)
    else:
        await _laser(owner_engine, tenant, capture_id)
    if clustered:
        await cluster(
            owner_engine, tenant, dive.id, [capture_id], formed_by="prediction"
        )
    if species:
        await _species(owner_engine, tenant, capture_id)
    return capture_id


async def test_dive_images_preprocessed_requires_clusters_and_laser_valid_species_rows(
    owner_engine, app_engine
):
    """PREDICTION cluster present AND every laser-valid image has a
    non-sentinel SpeciesLabel row."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    for _ in range(2):
        await _stage_2_frame(owner_engine, tenant, dive)

    assert (await _row(app_engine, tenant, dive))["dive_images_preprocessed"] is True


async def test_dive_images_preprocessed_false_without_prediction_cluster(
    owner_engine, app_engine
):
    """No PREDICTION cluster -> False even if every laser-valid image has a
    species row."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    await _stage_2_frame(owner_engine, tenant, dive, clustered=False)

    assert (await _row(app_engine, tenant, dive))["dive_images_preprocessed"] is False


async def test_dive_images_preprocessed_false_when_laser_valid_image_lacks_species_row(
    owner_engine, app_engine
):
    """Image 12 lacks a species row -> False. Image 11 having one isn't
    enough."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    await _stage_2_frame(owner_engine, tenant, dive)
    await _stage_2_frame(owner_engine, tenant, dive, species=False)

    row = await _row(app_engine, tenant, dive)
    assert row["dive_images_preprocessed"] is False
    assert row["species_preprocess_pending"] is True


async def test_dive_images_preprocessed_false_when_no_laser_valid_images(
    owner_engine, app_engine
):
    """Vacuous-truth guard: PREDICTION clusters but no laser-valid image reads
    False, not True."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    await _stage_2_frame(owner_engine, tenant, dive, valid=False, species=False)

    assert (await _row(app_engine, tenant, dive))["dive_images_preprocessed"] is False


async def test_dive_images_preprocessed_ignores_images_without_valid_laser(
    owner_engine, app_engine
):
    """An image with no valid laser doesn't enter the predicate, so its
    missing species row doesn't fail the flag."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    await _stage_2_frame(owner_engine, tenant, dive)
    await _stage_2_frame(owner_engine, tenant, dive, valid=False, species=False)

    assert (await _row(app_engine, tenant, dive))["dive_images_preprocessed"] is True


async def test_dive_images_preprocessed_ignores_unclustered_and_superseded(
    owner_engine, app_engine
):
    """Only *processable* images count -- valid laser AND in a PREDICTION
    cluster -- so the flag stays in step with the stage-2 selector. An
    unclustered image must not read "stage 2 stuck" forever (the prod poison
    pill)."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    await _stage_2_frame(owner_engine, tenant, dive)
    await _stage_2_frame(owner_engine, tenant, dive, clustered=False, species=False)

    assert (await _row(app_engine, tenant, dive))["dive_images_preprocessed"] is True


async def test_dive_images_preprocessed_false_when_only_species_row_superseded(
    owner_engine, app_engine
):
    """A dead-lettered species row is not evidence the work is done."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    capture_id = await _stage_2_frame(owner_engine, tenant, dive, species=False)
    await _species(owner_engine, tenant, capture_id, superseded=True)

    assert (await _row(app_engine, tenant, dive))["dive_images_preprocessed"] is False


async def test_dive_images_preprocessed_counts_a_completed_sentinel_as_done(
    owner_engine, app_engine
):
    """v2 (species_store `HAS_LIVE_SPECIES_TASK`): a completed sentinel is
    done work -- populate never tasks its frame. v1's view and cohort ignored
    it, so its dive read "stage 2 stuck" while being re-staged hourly."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    capture_id = await _stage_2_frame(owner_engine, tenant, dive, species=False)
    await _species(owner_engine, tenant, capture_id, sentinel=True, completed=True)

    row = await _row(app_engine, tenant, dive)
    assert (row["dive_images_preprocessed"], row["species_preprocess_pending"]) == (
        True,
        False,
    )


async def test_dive_images_preprocessed_false_while_a_frame_is_flagged(
    owner_engine, app_engine
):
    """v2 (the stage-2 cohort's second way in): a flagged live species row is
    a redraw stage 2 still owes."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    capture_id = await _stage_2_frame(owner_engine, tenant, dive, species=False)
    await _species(owner_engine, tenant, capture_id, needs_reprocess=True)

    assert (await _row(app_engine, tenant, dive))["dive_images_preprocessed"] is False


# --- species_labeling_complete ------------------------------------------------------


async def test_species_labeling_complete_true_when_all_completed(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    await _species(
        owner_engine, tenant, await _capture(owner_engine, tenant, dive), completed=True
    )

    assert (await _row(app_engine, tenant, dive))["species_labeling_complete"] is True


async def test_species_labeling_complete_false_when_any_incomplete(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    await _species(
        owner_engine, tenant, await _capture(owner_engine, tenant, dive), completed=True
    )
    await _species(owner_engine, tenant, await _capture(owner_engine, tenant, dive))

    assert (await _row(app_engine, tenant, dive))["species_labeling_complete"] is False


# --- slate_applicable / slate_preprocessed / slate_labeling_complete ------------------


async def test_slate_applicable_tracks_dive_slate_id(owner_engine, app_engine):
    tenant = await _tenant(owner_engine)
    with_slate = await _dive(
        owner_engine, tenant, slate_template_id=await _slate_template(owner_engine)
    )
    without = await _dive(owner_engine, tenant)

    assert (await _row(app_engine, tenant, with_slate))["slate_applicable"] is True
    assert (await _row(app_engine, tenant, without))["slate_applicable"] is False


async def _slate_dive(owner_engine, tenant, **template):
    template_id = await _slate_template(owner_engine, **template)
    return await _camera_dive(owner_engine, tenant, slate_template_id=template_id)


async def test_slate_preprocessed_true_when_marked_images_have_slate_rows(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _slate_dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _species(owner_engine, tenant, capture_id, SLATE_MARKER)
    await _slate_label(owner_engine, tenant, capture_id)

    assert (await _row(app_engine, tenant, dive))["slate_preprocessed"] is True


async def test_slate_preprocessed_false_when_no_slate_marked_images(
    owner_engine, app_engine
):
    """No species label says 'Slate, Laser on slate' -> nothing to preprocess
    yet -> False, not vacuously True."""
    tenant = await _tenant(owner_engine)
    dive = await _slate_dive(owner_engine, tenant)
    await _species(
        owner_engine, tenant, await _capture(owner_engine, tenant, dive), "Fish"
    )

    assert (await _row(app_engine, tenant, dive))["slate_preprocessed"] is False


async def test_slate_preprocessed_false_when_the_only_marker_is_superseded(
    owner_engine, app_engine
):
    """v2: a dead-lettered marker is not a slate frame, so a dive whose only
    marker is superseded has nothing to preprocess -- False, not vacuously
    True. v1's view counted it, and read the dive preprocessed."""
    tenant = await _tenant(owner_engine)
    dive = await _slate_dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _species(owner_engine, tenant, capture_id, SLATE_MARKER, superseded=True)
    await _slate_label(owner_engine, tenant, capture_id)

    assert (await _row(app_engine, tenant, dive))["slate_preprocessed"] is False


async def test_slate_preprocessed_false_when_dive_lacks_slate_id(
    owner_engine, app_engine
):
    """Even if labels exist, no slate template means the slate path doesn't
    apply to this dive at all."""
    tenant = await _tenant(owner_engine)
    dive = await _camera_dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _species(owner_engine, tenant, capture_id, SLATE_MARKER)
    await _slate_label(owner_engine, tenant, capture_id)

    assert (await _row(app_engine, tenant, dive))["slate_preprocessed"] is False


async def test_slate_preprocessed_ignores_a_superseded_marker(owner_engine, app_engine):
    """v2 (the stage-9 cohort): a dead-lettered species marker is not a slate
    frame. v1's view and cohort read every species row, so a superseded marker
    kept a dive in a cohort its resolver found nothing in."""
    tenant = await _tenant(owner_engine)
    dive = await _slate_dive(owner_engine, tenant)
    marked = await _capture(owner_engine, tenant, dive)
    await _species(owner_engine, tenant, marked, SLATE_MARKER)
    await _slate_label(owner_engine, tenant, marked)
    dead = await _capture(owner_engine, tenant, dive)
    await _species(owner_engine, tenant, dead, SLATE_MARKER, superseded=True)

    row = await _row(app_engine, tenant, dive)
    assert (row["slate_preprocessed"], row["slate_preprocess_pending"]) == (
        True,
        False,
    )


async def test_slate_preprocessed_false_while_a_frame_is_flagged(
    owner_engine, app_engine
):
    """v2 (the cohort's second way in): a flagged live slate label is a redraw
    stage 9 still owes."""
    tenant = await _tenant(owner_engine)
    dive = await _slate_dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    await _species(owner_engine, tenant, capture_id, SLATE_MARKER)
    await _slate_label(owner_engine, tenant, capture_id, needs_reprocess=True)

    row = await _row(app_engine, tenant, dive)
    assert (row["slate_preprocessed"], row["slate_preprocess_pending"]) == (
        False,
        True,
    )


async def test_a_template_stage_9_cannot_stage_is_not_pending(owner_engine, app_engine):
    """v2 (slate_store `_RESOLVABLE`): a template with no NAS path cannot be
    staged, so the dive is not offered: not preprocessed, not pending."""
    tenant = await _tenant(owner_engine)
    dive = await _slate_dive(owner_engine, tenant, source_path=None)
    await _species(
        owner_engine, tenant, await _capture(owner_engine, tenant, dive), SLATE_MARKER
    )

    row = await _row(app_engine, tenant, dive)
    assert (row["slate_preprocessed"], row["slate_preprocess_pending"]) == (
        False,
        False,
    )


async def test_slate_labeling_complete_true_when_all_completed(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _slate_dive(owner_engine, tenant)
    await _slate_label(
        owner_engine, tenant, await _capture(owner_engine, tenant, dive), completed=True
    )

    assert (await _row(app_engine, tenant, dive))["slate_labeling_complete"] is True


# --- calibrated / calibration_source (stage 13) ---------------------------------------


async def test_calibrated_true_when_laser_extrinsics_row_exists(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    await calibrate(owner_engine, tenant, dive.id)

    assert (await _row(app_engine, tenant, dive))["calibrated"] is True


async def test_calibrated_false_when_no_laser_extrinsics(owner_engine, app_engine):
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)

    assert (await _row(app_engine, tenant, dive))["calibrated"] is False


async def test_calibrated_true_when_borrowed_via_calibration_dive_id(
    owner_engine, app_engine
):
    """A fish-only dive linked to a slate dive that owns a calibration reads
    calibrated even though it has none of its own."""
    tenant = await _tenant(owner_engine)
    slate_dive = await _dive(owner_engine, tenant)
    fish_dive = await _dive(owner_engine, tenant, source_dive=slate_dive)
    await calibrate(owner_engine, tenant, slate_dive.id)

    assert (await _row(app_engine, tenant, slate_dive))["calibrated"] is True
    assert (await _row(app_engine, tenant, fish_dive))["calibrated"] is True


async def test_calibrated_false_when_linked_source_has_no_extrinsics(
    owner_engine, app_engine
):
    """A link to a source that isn't itself calibrated doesn't fabricate
    calibration."""
    tenant = await _tenant(owner_engine)
    source = await _dive(owner_engine, tenant)
    dive = await _dive(owner_engine, tenant, source_dive=source)

    assert (await _row(app_engine, tenant, dive))["calibrated"] is False


async def test_calibrated_false_after_a_refusal(owner_engine, app_engine):
    """v2 (0008, §9.13): a refusal after a fit is the dive's current
    calibration, so it has none. v1 kept the fitted extrinsics row and read
    calibrated even while refused."""
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    await calibrate(owner_engine, tenant, dive.id)
    await calibrate(owner_engine, tenant, dive.id, outcome="refused")

    row = await _row(app_engine, tenant, dive)
    assert (row["calibrated"], row["calibration_source"]) == (False, "none")


async def test_calibrated_false_for_an_implausible_fit(owner_engine, app_engine):
    """v2 (0018): a fit whose baseline is outside 0.097-0.145 m is no
    calibration anywhere (v1's `_plausible_extrinsics`, which v1's view did
    not apply)."""
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    await calibrate(owner_engine, tenant, dive.id, position=(0.0141, 0.0188, 0.0))

    assert (await _row(app_engine, tenant, dive))["calibrated"] is False


async def test_calibration_source_own_when_the_dive_has_its_own_extrinsics(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)
    await calibrate(owner_engine, tenant, dive.id)

    assert (await _row(app_engine, tenant, dive))["calibration_source"] == "own"


async def test_calibration_source_borrowed_when_only_the_link_has_extrinsics(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    source = await _dive(owner_engine, tenant)
    dive = await _dive(owner_engine, tenant, source_dive=source)
    await calibrate(owner_engine, tenant, source.id)

    assert (await _row(app_engine, tenant, dive))["calibration_source"] == "borrowed"


async def test_calibration_source_own_wins_over_a_link(owner_engine, app_engine):
    """Mirrors v1's `get_laser_extrinsics_for_dive` (and 0018): a dive with its
    own calibration uses it even when it names a source."""
    tenant = await _tenant(owner_engine)
    source = await _dive(owner_engine, tenant)
    dive = await _dive(owner_engine, tenant, source_dive=source)
    await calibrate(owner_engine, tenant, dive.id)
    await calibrate(owner_engine, tenant, source.id)

    assert (await _row(app_engine, tenant, dive))["calibration_source"] == "own"


async def test_calibration_source_none_when_uncalibrated(owner_engine, app_engine):
    tenant = await _tenant(owner_engine)
    dive = await _dive(owner_engine, tenant)

    row = await _row(app_engine, tenant, dive)
    assert row["calibration_source"] == "none"
    assert row["calibrated"] is False


# --- measured (stage 14) ------------------------------------------------------------
#
# `measured` is scoped to what stage 14 can actually measure (0026's
# `measurement_work`): a top-three measurable subject on a canonical capture
# with a valid laser and a valid head/tail -- and, for a real fish, a Label
# Studio cluster. It reads true when at least one server measurement is
# current (§9.13) and no such capture is left without one.


async def _measure_dive(owner_engine, tenant, **kwargs):
    """A high-priority dive on a pinhole camera with its own plausible
    calibration: (dive, laser calibration)."""
    dive = await _camera_dive(owner_engine, tenant, **kwargs)
    return dive, await calibrate(owner_engine, tenant, dive.id)


async def _measure(owner_engine, tenant, capture_id, calibration_id, model=None):
    fish_id = await fish(owner_engine, tenant, model=model)
    return await measurement(owner_engine, tenant, capture_id, fish_id, calibration_id)


async def test_measured_counts_a_ruler_image_like_a_fish_model(
    owner_engine, app_engine
):
    """The ruler is a known-length target measured through the same path, so a
    ruler frame is measurable and holds `measured` false until measured."""
    tenant = await _tenant(owner_engine)
    dive, calibration = await _measure_dive(owner_engine, tenant)
    ruler = await measurable_capture(
        owner_engine, tenant, dive.id, "Calibration Targets, Ruler", in_cluster=False
    )
    assert (await _row(app_engine, tenant, dive))["measured"] is False

    await _measure(owner_engine, tenant, ruler, calibration, model="Ruler")
    assert (await _row(app_engine, tenant, dive))["measured"] is True


async def test_measured_counts_a_box_image_like_the_ruler(owner_engine, app_engine):
    """The box (0.15 m) is a rigid known-length target too."""
    tenant = await _tenant(owner_engine)
    dive, calibration = await _measure_dive(owner_engine, tenant)
    box = await measurable_capture(
        owner_engine, tenant, dive.id, "Calibration Targets, Box", in_cluster=False
    )
    assert (await _row(app_engine, tenant, dive))["measured"] is False

    await _measure(owner_engine, tenant, box, calibration, model="Box")
    assert (await _row(app_engine, tenant, dive))["measured"] is True


async def test_measured_ignores_the_checkerboard(owner_engine, app_engine):
    """The checkerboard has no single head/tail span, so it is not measurable.
    Asserted as True against a dive that ALSO holds a measured model frame:
    the negative form could not fail (see v1's reason)."""
    tenant = await _tenant(owner_engine)
    dive, calibration = await _measure_dive(owner_engine, tenant)
    model = await measurable_capture(
        owner_engine, tenant, dive.id, "Fish Model, Grouper", in_cluster=False
    )
    await measurable_capture(
        owner_engine,
        tenant,
        dive.id,
        "Calibration Targets, E4E Checkerboard",
        in_cluster=False,
    )
    await _measure(owner_engine, tenant, model, calibration, model="Grouper")

    assert (await _row(app_engine, tenant, dive))["measured"] is True


async def test_measured_ignores_the_slate_marker(owner_engine, app_engine):
    """The stage-9 marker is not measurable, so it can never hold a dive in
    the stage-14 cohort forever."""
    tenant = await _tenant(owner_engine)
    dive, _ = await _measure_dive(owner_engine, tenant)
    await measurable_capture(
        owner_engine, tenant, dive.id, SLATE_MARKER, in_cluster=False
    )

    row = await _row(app_engine, tenant, dive)
    assert row["measured"] is False, "vacuous: nothing measurable, nothing measured"
    assert row["measurement_pending"] is False


async def test_measured_true_for_fish_model_image_without_a_cluster(
    owner_engine, app_engine
):
    """A fish-model dive drains: no Label Studio cluster, but the model frame
    is measurable and, once measured, `measured` flips true."""
    tenant = await _tenant(owner_engine)
    dive, calibration = await _measure_dive(owner_engine, tenant)
    model = await measurable_capture(
        owner_engine, tenant, dive.id, "Fish Model, Grouper", in_cluster=False
    )
    await _measure(owner_engine, tenant, model, calibration, model="Grouper")

    assert (await _row(app_engine, tenant, dive))["measured"] is True


async def test_measured_false_for_unmeasured_fish_model_image(owner_engine, app_engine):
    tenant = await _tenant(owner_engine)
    dive, _ = await _measure_dive(owner_engine, tenant)
    await measurable_capture(
        owner_engine, tenant, dive.id, "Fish Model, Grouper", in_cluster=False
    )

    row = await _row(app_engine, tenant, dive)
    assert (row["measured"], row["measurement_pending"]) == (False, True)


async def test_measured_true_when_every_measurable_image_has_a_measurement(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive, calibration = await _measure_dive(owner_engine, tenant)
    capture_id = await measurable_capture(owner_engine, tenant, dive.id)
    await _measure(owner_engine, tenant, capture_id, calibration)

    row = await _row(app_engine, tenant, dive)
    assert (row["measured"], row["measurement_pending"]) == (True, False)


async def test_measured_false_when_a_measurable_image_is_unmeasured(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    dive, calibration = await _measure_dive(owner_engine, tenant)
    measured = await measurable_capture(owner_engine, tenant, dive.id)
    await measurable_capture(owner_engine, tenant, dive.id)  # left unmeasured
    await _measure(owner_engine, tenant, measured, calibration)

    assert (await _row(app_engine, tenant, dive))["measured"] is False


async def test_measured_false_when_dive_has_no_measurements(owner_engine, app_engine):
    """Vacuous-truth guard: nothing measured -> not measured."""
    tenant = await _tenant(owner_engine)
    dive, _ = await _measure_dive(owner_engine, tenant)
    await measurable_capture(owner_engine, tenant, dive.id)

    assert (await _row(app_engine, tenant, dive))["measured"] is False


async def test_measured_ignores_unbound_clusters_with_no_measurable_image(
    owner_engine, app_engine
):
    """A Label Studio cluster with no measurable frame can never be bound to a
    fish; it must not hold the dive back (prod dive 466: 1632 such clusters
    against 24 measurable images)."""
    tenant = await _tenant(owner_engine)
    dive, calibration = await _measure_dive(owner_engine, tenant)
    capture_id = await measurable_capture(owner_engine, tenant, dive.id)
    await _measure(owner_engine, tenant, capture_id, calibration)
    await cluster(owner_engine, tenant, dive.id)

    assert (await _row(app_engine, tenant, dive))["measured"] is True


async def test_measured_false_when_the_calibration_was_replaced(
    owner_engine, app_engine
):
    """The 2026-08-11 panel-offset fix recalibrated dives that already had
    measurements; their lengths are stale. `measured` reads false, which is
    honest and what re-queues the dive."""
    tenant = await _tenant(owner_engine)
    dive, old = await _measure_dive(owner_engine, tenant)
    capture_id = await measurable_capture(owner_engine, tenant, dive.id)
    await _measure(owner_engine, tenant, capture_id, old)
    await calibrate(owner_engine, tenant, dive.id)  # the dive resolves anew

    row = await _row(app_engine, tenant, dive)
    assert (row["measured"], row["measurement_pending"]) == (False, True)


async def test_measured_false_for_measurements_predating_provenance(
    owner_engine, app_engine
):
    """A v1 measurement that names no calibration is not current (0013), so
    its frame reads unmeasured until stage 14 revisits it: the backfill."""
    tenant = await _tenant(owner_engine)
    dive, _ = await _measure_dive(owner_engine, tenant)
    capture_id = await measurable_capture(owner_engine, tenant, dive.id)
    fish_id = await fish(owner_engine, tenant)
    await measurement(owner_engine, tenant, capture_id, fish_id, None, v1_id=next(_n))

    assert (await _row(app_engine, tenant, dive))["measured"] is False


async def test_measured_true_through_a_borrowed_calibration(owner_engine, app_engine):
    """A fish-only dive is measured with its sibling's calibration, so that is
    the calibration its measurements name."""
    tenant = await _tenant(owner_engine)
    source, calibration = await _measure_dive(owner_engine, tenant)
    device_id = await _device(owner_engine, tenant)
    dive = await _dive(owner_engine, tenant, device_id=device_id, source_dive=source)
    capture_id = await measurable_capture(owner_engine, tenant, dive.id)
    await _measure(owner_engine, tenant, capture_id, calibration)

    assert (await _row(app_engine, tenant, dive))["measured"] is True


async def test_measured_ignores_non_top_three_images(owner_engine, app_engine):
    """Stage 14 only measures top-three photos, so others can't block."""
    tenant = await _tenant(owner_engine)
    dive, calibration = await _measure_dive(owner_engine, tenant)
    capture_id = await measurable_capture(owner_engine, tenant, dive.id)
    await _measure(owner_engine, tenant, capture_id, calibration)
    other = await _capture(owner_engine, tenant, dive)
    await _species(
        owner_engine,
        tenant,
        other,
        REAL_FISH,
        completed=True,
        top_three_photos_of_group=False,
    )

    assert (await _row(app_engine, tenant, dive))["measured"] is True


async def test_measured_ignores_species_rows_without_a_scientific_name(
    owner_engine, app_engine
):
    """A slate row carries no `Common (Scientific)` name and no target name,
    so stage 14 skips it; counting it pinned `measured` false forever."""
    tenant = await _tenant(owner_engine)
    dive, calibration = await _measure_dive(owner_engine, tenant)
    real = await measurable_capture(owner_engine, tenant, dive.id)
    await measurable_capture(owner_engine, tenant, dive.id, SLATE_MARKER)
    await _measure(owner_engine, tenant, real, calibration)

    assert (await _row(app_engine, tenant, dive))[
        "measured"
    ] is True, "the slate row must not hold `measured` false"


async def test_measured_ignores_duplicate_captures(owner_engine, app_engine):
    """is_canonical gating: a duplicate frame is never stage 14's work."""
    tenant = await _tenant(owner_engine)
    dive, calibration = await _measure_dive(owner_engine, tenant)
    capture_id = await measurable_capture(owner_engine, tenant, dive.id)
    await _measure(owner_engine, tenant, capture_id, calibration)
    duplicate = await _capture(owner_engine, tenant, dive, canonical=False)
    await _valid_laser(owner_engine, tenant, duplicate)
    await _head_tail(owner_engine, tenant, duplicate, completed=True, head_x=1.0,
                     head_y=2.0, tail_x=3.0, tail_y=4.0)  # fmt: skip
    await _species(owner_engine, tenant, duplicate, "Fish Model, Grouper",
                   completed=True, top_three_photos_of_group=True)  # fmt: skip

    assert (await _row(app_engine, tenant, dive))["measured"] is True


async def test_measured_false_when_only_a_duplicate_is_measured(
    owner_engine, app_engine
):
    """is_canonical gating on the "at least one" half too: a measurement of a
    duplicate frame is not the dive's stage-14 work done."""
    tenant = await _tenant(owner_engine)
    dive, calibration = await _measure_dive(owner_engine, tenant)
    duplicate = await _capture(owner_engine, tenant, dive, canonical=False)
    await _measure(owner_engine, tenant, duplicate, calibration)

    assert (await _row(app_engine, tenant, dive))["measured"] is False


async def test_measured_counts_only_the_servers_measurements(owner_engine, app_engine):
    """`measured` is stage 14's flag, and stage 14 reads only server results
    (`measurement_work`); a device's own measurement (mobile, §9.13) is not
    stage 14's work done."""
    tenant = await _tenant(owner_engine)
    dive, _ = await _measure_dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, dive)
    fish_id = await fish(owner_engine, tenant)
    await exec_(
        owner_engine,
        "INSERT INTO measurements (tenant_id, capture_id, fish_id, source, "
        "length_m) VALUES (:t, :c, :f, 'device', 0.3)",
        t=tenant,
        c=capture_id,
        f=fish_id,
    )

    assert (await _row(app_engine, tenant, dive))["measured"] is False


async def test_measured_does_not_wait_on_a_refused_frame(owner_engine, app_engine):
    """v2 (0025, §9.16): a frame whose very inputs were refused (a zero or
    non-finite length) is "tried, made no progress" -- not work -- until an
    input changes. v1 kept re-selecting such a dive forever."""
    tenant = await _tenant(owner_engine)
    dive, calibration = await _measure_dive(owner_engine, tenant)
    measured = await measurable_capture(owner_engine, tenant, dive.id)
    await _measure(owner_engine, tenant, measured, calibration)
    refused = await measurable_capture(owner_engine, tenant, dive.id)
    async with tenant_transaction(app_engine, tenant) as conn:
        work = (
            await conn.execute(
                text("SELECT * FROM measurement_work WHERE capture_id = :c"),
                {"c": refused},
            )
        ).one()
    await exec_(
        owner_engine,
        "INSERT INTO measurement_refusals (tenant_id, capture_id, "
        "laser_calibration_id, reason, species_label_id, content_of_image, "
        "laser_label_id, laser_x, laser_y, head_tail_label_id, head_x, head_y, "
        "tail_x, tail_y, algorithm, algorithm_version, core_version) VALUES "
        "(:t, :c, :cal, 'zero_length', :s, :content, :l, :lx, :ly, :h, :hx, :hy, "
        ":tx, :ty, 'a', '1', '4.1.0')",
        t=tenant,
        c=refused,
        cal=work.laser_calibration_id,
        s=work.species_label_id,
        content=work.content_of_image,
        l=work.laser_label_id,
        lx=work.laser_x,
        ly=work.laser_y,
        h=work.head_tail_label_id,
        hx=work.head_x,
        hy=work.head_y,
        tx=work.tail_x,
        ty=work.tail_y,
    )

    row = await _row(app_engine, tenant, dive)
    assert (row["measured"], row["measurement_pending"]) == (True, False)


# --- parity: each `*_pending` column is its selector's cohort ---------------------------
#
# For every stage, a corpus of dives in the states its cohort distinguishes --
# work and no work, the v2-only terms (camera, template, resolvability), a
# low-priority dive with work, and another tenant's dive with work -- and the
# set the view marks pending must be the set the selector picks, one by one.


async def _assert_parity(owner_engine, app_engine, tenant, column, select, expected):
    selected = await _selected(owner_engine, app_engine, tenant, select)
    viewed = await _pending(app_engine, tenant, column)
    assert viewed == selected
    # Never vacuous: the corpus has work, and has dives without it.
    assert viewed == {d.number for d in expected}


async def _other_tenant_with_work(owner_engine, seed):
    """Another tenant's dive with the stage's work: RLS keeps it out."""
    partner = await _tenant(owner_engine, "partner")
    await seed(partner)


async def test_laser_preprocess_pending_is_the_stage_0_1_cohort(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)

    async def unlabeled(t, **kwargs):
        dive = await _camera_dive(owner_engine, t, **kwargs)
        await _capture(owner_engine, t, dive)
        return dive

    work = await unlabeled(tenant)
    flagged = await _camera_dive(owner_engine, tenant)
    await _laser(
        owner_engine,
        tenant,
        await _capture(owner_engine, tenant, flagged),
        needs_reprocess=True,
    )
    done = await _camera_dive(owner_engine, tenant)
    await _laser(owner_engine, tenant, await _capture(owner_engine, tenant, done))
    duplicate_only = await _camera_dive(owner_engine, tenant)
    await _capture(owner_engine, tenant, duplicate_only, canonical=False)
    await unlabeled(tenant, priority="low")
    await unlabeled(tenant, model="axial_refractive")
    no_camera = await _dive(owner_engine, tenant)
    await _capture(owner_engine, tenant, no_camera)
    await _other_tenant_with_work(owner_engine, unlabeled)

    await _assert_parity(
        owner_engine,
        app_engine,
        tenant,
        "laser_preprocess_pending",
        laser_store.next_dive_for_laser_preprocessing,
        [work, flagged],
    )
    # And the v1 column is its negation wherever the cohort's extra terms hold.
    assert (await _row(app_engine, tenant, work))["laser_preprocessed"] is False
    assert (await _row(app_engine, tenant, done))["laser_preprocessed"] is True


async def test_laser_prediction_pending_is_the_prediction_cohort(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    version = laser_store.LASER_PREDICTOR_VERSION

    async def unpredicted(t, **kwargs):
        dive = await _camera_dive(owner_engine, t, **kwargs)
        await _capture(owner_engine, t, dive)
        return dive

    never = await unpredicted(tenant)
    # Being labeled, with a stale prediction: re-predicted.
    stale = await _camera_dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, stale)
    await _laser_prediction(owner_engine, tenant, capture_id, version=version - 1)
    await _laser(owner_engine, tenant, capture_id)
    # Stale but not being labeled: left alone (v1).
    idle = await _camera_dive(owner_engine, tenant)
    await _laser_prediction(
        owner_engine,
        tenant,
        await _capture(owner_engine, tenant, idle),
        version=version - 1,
    )
    current = await _camera_dive(owner_engine, tenant)
    await _laser_prediction(
        owner_engine,
        tenant,
        await _capture(owner_engine, tenant, current),
        version=version,
    )
    finished = await _camera_dive(owner_engine, tenant)
    await _valid_laser(
        owner_engine, tenant, await _capture(owner_engine, tenant, finished)
    )
    await unpredicted(tenant, priority="low")
    await unpredicted(tenant, model="axial_refractive")
    await _other_tenant_with_work(owner_engine, unpredicted)

    await _assert_parity(
        owner_engine,
        app_engine,
        tenant,
        "laser_prediction_pending",
        laser_store.next_dive_for_laser_prediction,
        [never, stale],
    )


async def test_clustering_pending_is_the_stage_1_cohort(owner_engine, app_engine):
    tenant = await _tenant(owner_engine)

    async def unclustered(t, **kwargs):
        dive = await _dive(owner_engine, t, **kwargs)
        await _valid_laser(owner_engine, t, await _capture(owner_engine, t, dive))
        return dive

    work = await unclustered(tenant)
    done = await unclustered(tenant)
    await cluster(owner_engine, tenant, done.id, formed_by="prediction")
    unlabeled = await _dive(owner_engine, tenant)
    await _laser(owner_engine, tenant, await _capture(owner_engine, tenant, unlabeled))
    duplicate = await _dive(owner_engine, tenant)
    await _valid_laser(
        owner_engine,
        tenant,
        await _capture(owner_engine, tenant, duplicate, canonical=False),
    )
    await unclustered(tenant, priority="low")
    await _other_tenant_with_work(owner_engine, unclustered)

    await _assert_parity(
        owner_engine,
        app_engine,
        tenant,
        "clustering_pending",
        clustering_store.next_dive_for_clustering,
        [work],
    )
    assert (await _row(app_engine, tenant, done))["has_prediction_clusters"] is True


async def test_species_preprocess_pending_is_the_stage_2_cohort(
    owner_engine, app_engine
):
    """v1's `test_view_and_selector_agree_on_species_predicate`, over every
    branch of v2's cohort."""
    tenant = await _tenant(owner_engine)

    async def unlabeled(t, **kwargs):
        dive = await _camera_dive(owner_engine, t, **kwargs)
        await _stage_2_frame(owner_engine, t, dive, species=False)
        return dive

    work = await unlabeled(tenant)
    flagged = await _camera_dive(owner_engine, tenant)
    capture_id = await _stage_2_frame(owner_engine, tenant, flagged, species=False)
    await _species(owner_engine, tenant, capture_id, needs_reprocess=True)
    done = await _camera_dive(owner_engine, tenant)
    await _stage_2_frame(owner_engine, tenant, done)
    ineligible = await _camera_dive(owner_engine, tenant)
    await _stage_2_frame(owner_engine, tenant, ineligible, valid=False, species=False)
    unclustered = await _camera_dive(owner_engine, tenant)
    await _stage_2_frame(
        owner_engine, tenant, unclustered, clustered=False, species=False
    )
    await unlabeled(tenant, priority="low")
    await unlabeled(tenant, model="axial_refractive")
    await _other_tenant_with_work(owner_engine, unlabeled)

    await _assert_parity(
        owner_engine,
        app_engine,
        tenant,
        "species_preprocess_pending",
        species_store.next_dive_for_species_preprocessing,
        [work, flagged],
    )
    assert (await _row(app_engine, tenant, done))["dive_images_preprocessed"] is True
    assert (await _row(app_engine, tenant, work))["dive_images_preprocessed"] is False
    assert (await _row(app_engine, tenant, ineligible))[
        "dive_images_preprocessed"
    ] is False


async def test_headtail_preprocess_pending_is_the_stage_5_1_cohort(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)

    async def untasked(t, **kwargs):
        dive = await _camera_dive(owner_engine, t, **kwargs)
        await _valid_laser(owner_engine, t, await _capture(owner_engine, t, dive))
        return dive

    work = await untasked(tenant)
    flagged = await _camera_dive(owner_engine, tenant)
    capture_id = await _capture(owner_engine, tenant, flagged)
    await _head_tail(owner_engine, tenant, capture_id, needs_reprocess=True)
    done = await untasked(tenant)
    async with owner_engine.connect() as conn:
        done_capture = (
            await conn.execute(
                text("SELECT id FROM captures WHERE dive_id = :d"), {"d": done.id}
            )
        ).scalar_one()
    await _head_tail(owner_engine, tenant, done_capture)
    await untasked(tenant, priority="low")
    await untasked(tenant, model="axial_refractive")
    await _other_tenant_with_work(owner_engine, untasked)

    await _assert_parity(
        owner_engine,
        app_engine,
        tenant,
        "headtail_preprocess_pending",
        headtail_store.next_dive_for_headtail_preprocessing,
        [work, flagged],
    )
    assert (await _row(app_engine, tenant, done))["headtail_preprocessed"] is True


async def test_headtail_prediction_pending_is_the_prediction_cohort(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)
    version = headtail_contract.HEADTAIL_PREDICTOR_VERSION

    async def unpredicted(t, **kwargs):
        dive = await _camera_dive(owner_engine, t, **kwargs)
        capture_id = await _capture(owner_engine, t, dive)
        await _valid_laser(owner_engine, t, capture_id)
        return dive, capture_id

    never, _ = await unpredicted(tenant)
    stale, capture_id = await unpredicted(tenant)
    await _head_tail_prediction(owner_engine, tenant, capture_id, version=version - 1)
    current, capture_id = await unpredicted(tenant)
    await _head_tail_prediction(owner_engine, tenant, capture_id, version=version)
    labeled, capture_id = await unpredicted(tenant)
    await _head_tail(owner_engine, tenant, capture_id, completed=True)
    await unpredicted(tenant, priority="low")
    await unpredicted(tenant, model="axial_refractive")
    await _other_tenant_with_work(owner_engine, unpredicted)

    async def select(conn, tenant_id):
        return await headtail_store.next_dive_for_headtail_prediction(
            conn, tenant_id, predictor_version=version
        )

    await _assert_parity(
        owner_engine,
        app_engine,
        tenant,
        "headtail_prediction_pending",
        select,
        [never, stale],
    )


async def test_slate_preprocess_pending_is_the_stage_9_cohort(owner_engine, app_engine):
    tenant = await _tenant(owner_engine)

    async def marked(t, **template):
        dive = await _slate_dive(owner_engine, t, **template)
        await _species(
            owner_engine, t, await _capture(owner_engine, t, dive), SLATE_MARKER
        )
        return dive

    work = await marked(tenant)
    flagged = await _slate_dive(owner_engine, tenant)
    await _slate_label(
        owner_engine,
        tenant,
        await _capture(owner_engine, tenant, flagged),
        needs_reprocess=True,
    )
    done = await marked(tenant)
    async with owner_engine.connect() as conn:
        done_capture = (
            await conn.execute(
                text("SELECT id FROM captures WHERE dive_id = :d"), {"d": done.id}
            )
        ).scalar_one()
    await _slate_label(owner_engine, tenant, done_capture)
    await marked(tenant, dpi=None)
    await marked(tenant, source_path=None)
    no_template = await _camera_dive(owner_engine, tenant)
    await _species(
        owner_engine,
        tenant,
        await _capture(owner_engine, tenant, no_template),
        SLATE_MARKER,
    )
    low = await marked(tenant)
    await exec_(
        owner_engine, "UPDATE dives SET priority = 'low' WHERE id = :d", d=low.id
    )
    await _other_tenant_with_work(owner_engine, marked)

    await _assert_parity(
        owner_engine,
        app_engine,
        tenant,
        "slate_preprocess_pending",
        slate_store.next_dive_for_slate_preprocessing,
        [work, flagged],
    )
    assert (await _row(app_engine, tenant, done))["slate_preprocessed"] is True


async def test_laser_calibration_pending_is_the_stage_13_cohort(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)

    async def observed(t, *, frames=2, **template):
        dive = await _slate_dive(owner_engine, t, **template)
        for _ in range(frames):
            await _slate_observation(owner_engine, t, dive)
        return dive

    work = await observed(tenant)
    calibrated = await observed(tenant)
    await calibrate(owner_engine, tenant, calibrated.id)
    refused = await observed(tenant)
    await exec_(
        owner_engine,
        "INSERT INTO laser_calibrations (tenant_id, dive_id, producer, outcome, "
        "refusal_reason, slate_template_id, inputs_as_of) SELECT :t, :d, 'slate', "
        "'refused', 'too few', slate_template_id, now() FROM dives WHERE id = :d",
        t=tenant,
        d=refused.id,
    )
    await observed(tenant, frames=1)
    await observed(tenant, dpi=None)
    low = await observed(tenant)
    await exec_(
        owner_engine, "UPDATE dives SET priority = 'low' WHERE id = :d", d=low.id
    )
    await _other_tenant_with_work(owner_engine, observed)

    await _assert_parity(
        owner_engine,
        app_engine,
        tenant,
        "laser_calibration_pending",
        laser_calibration_store.next_dive_for_laser_calibration,
        [work],
    )


async def test_checkerboard_calibration_pending_is_the_board_cohort(
    owner_engine, app_engine
):
    tenant = await _tenant(owner_engine)

    async def board(t, *, dots=2, **kwargs):
        target = await _calibration_target(owner_engine)
        dive = await _camera_dive(
            owner_engine, t, calibration_target_id=target, **kwargs
        )
        for _ in range(dots):
            await _laser(owner_engine, t, await _capture(owner_engine, t, dive))
        return dive

    work = await board(tenant)
    calibrated = await board(tenant)
    await calibrate(owner_engine, tenant, calibrated.id)
    await board(tenant, dots=1)
    await board(tenant, priority="low")
    no_camera = await _dive(
        owner_engine,
        tenant,
        calibration_target_id=await _calibration_target(owner_engine),
    )
    for _ in range(2):
        await _laser(
            owner_engine, tenant, await _capture(owner_engine, tenant, no_camera)
        )
    await _other_tenant_with_work(owner_engine, board)

    await _assert_parity(
        owner_engine,
        app_engine,
        tenant,
        "checkerboard_calibration_pending",
        laser_calibration_store.next_dive_for_checkerboard_calibration,
        [work],
    )


async def test_laser_depth_pending_is_the_depth_cohort(owner_engine, app_engine):
    tenant = await _tenant(owner_engine)

    async def undepthed(t, **kwargs):
        dive, calibration = await _measure_dive(owner_engine, t, **kwargs)
        capture_id = await _capture(owner_engine, t, dive)
        return (
            dive,
            calibration,
            capture_id,
            await _valid_laser(owner_engine, t, capture_id),
        )

    work, *_ = await undepthed(tenant)
    done, calibration, capture_id, label = await undepthed(tenant)
    await depth(owner_engine, tenant, capture_id, label, calibration)
    uncalibrated = await _camera_dive(owner_engine, tenant)
    await _valid_laser(
        owner_engine, tenant, await _capture(owner_engine, tenant, uncalibrated)
    )
    await undepthed(tenant, priority="low")
    await _other_tenant_with_work(owner_engine, undepthed)

    await _assert_parity(
        owner_engine,
        app_engine,
        tenant,
        "laser_depth_pending",
        laser_depth_store.next_dive_for_laser_depth,
        [work],
    )


async def test_measurement_pending_is_the_stage_14_cohort(owner_engine, app_engine):
    tenant = await _tenant(owner_engine)

    async def unmeasured(t, **kwargs):
        dive, calibration = await _measure_dive(owner_engine, t, **kwargs)
        return dive, calibration, await measurable_capture(owner_engine, t, dive.id)

    work, *_ = await unmeasured(tenant)
    done, calibration, capture_id = await unmeasured(tenant)
    await _measure(owner_engine, tenant, capture_id, calibration)
    uncalibrated = await _camera_dive(owner_engine, tenant)
    await measurable_capture(owner_engine, tenant, uncalibrated.id)
    await unmeasured(tenant, priority="low")
    await _other_tenant_with_work(owner_engine, unmeasured)

    await _assert_parity(
        owner_engine,
        app_engine,
        tenant,
        "measurement_pending",
        measurement_store.next_dive_for_measurement,
        [work],
    )
    assert (await _row(app_engine, tenant, done))["measured"] is True
    assert (await _row(app_engine, tenant, work))["measured"] is False
