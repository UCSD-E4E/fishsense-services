"""The database side of the web portal, tenant-scoped, on real Postgres.

Ported from fishsense-lite@77e8f8e5:

* services/fishsense-api/tests/test_label_studio_project_ids_gated.py and
  test_label_studio_project_ids_superseded.py -- the "live project" query the
  landing page and triage both use (`label_controller.py`
  `get_*_label_studio_project_ids`, `_gate_scan`);
* services/fishsense-api/tests/test_calibration_source_endpoints.py, the
  set / clear half (`dive_controller.py` `set_dive_calibration_source`,
  `clear_dive_calibration_source`). Its resolution half (own calibration
  first, then the link) is `effective_laser_calibrations` (0018), which the
  calibration slice owns.

Names, fixtures' shapes and reasons are v1's. v2 changes, each pinned here:

* one query for the four kinds (v1 had four endpoints), per tenant;
* **the gate reads the *current* prediction.** v2's predictions are
  append-only, so v1's "a re-prediction clears the verdict" is a newer
  prediction row without one;
* `gated` is refused for a kind with no gate, instead of being unrepresentable;
* dives are named by `number` (v1's id for a migrated dive), never a uuid;
  the calibration link is refused across tenants (it can't be seen).
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.portal_store import (
    DiveNotFound,
    GateNotApplicable,
    SelfLink,
    clear_calibration_source,
    label_studio_project_ids,
    list_dives,
    set_calibration_source,
)

T0 = datetime(2025, 1, 1, tzinfo=UTC)


async def _one(conn, sql: str, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


@pytest.fixture
async def lab(owner_engine) -> uuid.UUID:
    async with owner_engine.begin() as conn:
        return await _one(
            conn, "INSERT INTO tenants (slug, name) VALUES ('lab', 'lab') RETURNING id"
        )


@pytest.fixture
def seed(owner_engine, lab):
    """Seed as the owner (as migrate-v1 would), in the lab tenant."""

    class Seed:
        async def dive(
            self, number, *, name=None, tenant=None, slate=None, source=None
        ):
            async with owner_engine.begin() as conn:
                return await _one(
                    conn,
                    "INSERT INTO dives (tenant_id, v1_id, name, source_path, dived_at, "
                    "slate_template_id, calibration_source_dive_id) "
                    "VALUES (:t, :n, :name, :p, :at, :slate, :source) RETURNING id",
                    t=tenant or lab, n=number, name=name, p=f"/dives/{number}",
                    at=T0, slate=slate, source=source,
                )  # fmt: skip

        async def image(self, image_id, dive=None, tenant=None):
            """v1's `_image`: a capture, with its v1 id."""
            async with owner_engine.begin() as conn:
                dive = dive or await _one(
                    conn,
                    "INSERT INTO dives (tenant_id, source_path, dived_at) "
                    "VALUES (:t, :p, :at) RETURNING id",
                    t=tenant or lab, p=f"/dives/for-{image_id}-{uuid.uuid4()}", at=T0,
                )  # fmt: skip
                return await _one(
                    conn,
                    "INSERT INTO captures (tenant_id, v1_id, dive_id, source_path, "
                    "captured_at, checksum) VALUES (:t, :id, :d, :p, :at, :c) "
                    "RETURNING id",
                    t=tenant or lab, id=image_id, d=dive, p=f"/img-{image_id}",
                    at=T0, c=f"{image_id:032d}",
                )  # fmt: skip

        async def label(self, table, capture, *, project_id, completed=True,
                        superseded=False, tenant=None):  # fmt: skip
            async with owner_engine.begin() as conn:
                await conn.execute(
                    text(
                        f"INSERT INTO {table} (tenant_id, capture_id, source, "
                        "ls_project_id, completed, superseded) "
                        "VALUES (:t, :c, 'human', :p, :done, :gone)"
                    ),
                    {"t": tenant or lab, "c": capture, "p": project_id,
                     "done": completed, "gone": superseded},
                )  # fmt: skip

        async def prediction(self, capture, *, gate_verdict, tenant=None):
            async with owner_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO laser_predictions (tenant_id, capture_id, "
                        "confidence, predictor_version, gate_verdict) "
                        "VALUES (:t, :c, 0.9, 2, :v)"
                    ),
                    {"t": tenant or lab, "c": capture, "v": gate_verdict},
                )

        async def slate_template(self, number):
            async with owner_engine.begin() as conn:
                return await _one(
                    conn,
                    "INSERT INTO slate_templates (name, reference_points, v1_id) "
                    "VALUES (:n, '[]', :id) RETURNING id",
                    n=f"portal test slate {uuid.uuid4()}",
                    id=number,
                )

        async def calibration_source(self, dive_id):
            async with owner_engine.connect() as conn:
                return await _one(
                    conn,
                    "SELECT s.number FROM dives d "
                    "LEFT JOIN dives s ON s.id = d.calibration_source_dive_id "
                    "WHERE d.id = :d",
                    d=dive_id,
                )

    return Seed()


async def _ids(app_engine, tenant, kind="laser", **flags) -> list[int]:
    async with tenant_transaction(app_engine, tenant) as conn:
        return await label_studio_project_ids(conn, tenant, kind, **flags)


# -- the auto-accept gate filter (test_label_studio_project_ids_gated.py) --------

# Project 10: both frames judged by the gate -> the gate is done here.
# Project 20: one frame judged, one still pending -> the gate is mid-sweep.
# Project 30: frames exist but the gate has never run.
FULLY_GATED, PARTIALLY_GATED, UNGATED = 10, 20, 30


async def _seed_three_projects(seed):
    images = [await seed.image(i) for i in range(1, 6)]
    for image, project in zip(
        images, [FULLY_GATED, FULLY_GATED, PARTIALLY_GATED, PARTIALLY_GATED, UNGATED]
    ):
        await seed.label("laser_labels", image, project_id=project)
    for image, verdict in zip(
        images, ["auto_accepted", "off_line", "auto_accepted", None, None]
    ):
        await seed.prediction(image, gate_verdict=verdict)


async def test_omitted_gated_is_unchanged(seed, app_engine, lab):
    """Default behaviour must not move -- every project still surfaces."""
    await _seed_three_projects(seed)
    assert await _ids(app_engine, lab) == [FULLY_GATED, PARTIALLY_GATED, UNGATED]


async def test_gated_true_requires_the_gate_to_be_finished(seed, app_engine, lab):
    """A half-swept project is excluded -- its pending frames are the
    machine's work, not a labeler's."""
    await _seed_three_projects(seed)
    assert await _ids(app_engine, lab, gated=True) == [FULLY_GATED]


async def test_gated_true_excludes_a_project_the_gate_never_touched(
    seed, app_engine, lab
):
    """Vacuous truth reads as False, per the `dive_pipeline_status`
    convention: no judged prediction means the gate has not been here."""
    await _seed_three_projects(seed)
    assert UNGATED not in await _ids(app_engine, lab, gated=True)


async def test_gated_false_is_the_exact_complement(seed, app_engine, lab):
    """`gated=false` must partition the same base set, so the two answers
    reassemble into the unfiltered one. Anything else makes the flag a
    third, silently different query."""
    await _seed_three_projects(seed)
    unfiltered = await _ids(app_engine, lab)
    gated = await _ids(app_engine, lab, gated=True)
    ungated = await _ids(app_engine, lab, gated=False)
    assert sorted(gated + ungated) == unfiltered
    assert set(gated).isdisjoint(ungated)


async def test_a_project_with_no_predictions_at_all_is_not_gated(seed, app_engine, lab):
    """No prediction row means the detector has not run, so there is
    nothing for the gate to have judged."""
    await seed.label("laser_labels", await seed.image(1), project_id=FULLY_GATED)
    assert await _ids(app_engine, lab, gated=True) == []
    assert await _ids(app_engine, lab, gated=False) == [FULLY_GATED]


async def test_superseded_labels_do_not_hold_a_project_ungated(seed, app_engine, lab):
    """A dead-lettered label is not live labeling work, so its image's
    pending prediction must not keep an otherwise-finished project off the
    landing page."""
    one, two = await seed.image(1), await seed.image(2)
    await seed.label("laser_labels", one, project_id=FULLY_GATED)
    await seed.label("laser_labels", two, project_id=FULLY_GATED, superseded=True)
    await seed.prediction(one, gate_verdict="auto_accepted")
    await seed.prediction(two, gate_verdict=None)
    assert await _ids(app_engine, lab, gated=True) == [FULLY_GATED]


async def test_gated_composes_with_incomplete(seed, app_engine, lab):
    """Both filters apply together -- the landing page sends `incomplete`
    and `gated` in the same request."""
    one, two = await seed.image(1), await seed.image(2)
    # Gate finished, but every label is already done: no work left.
    await seed.label("laser_labels", one, project_id=FULLY_GATED, completed=True)
    # Gate finished and work remains -> this is the one to show.
    await seed.label("laser_labels", two, project_id=PARTIALLY_GATED, completed=False)
    await seed.prediction(one, gate_verdict="auto_accepted")
    await seed.prediction(two, gate_verdict="audit_sample")
    assert await _ids(app_engine, lab, gated=True, incomplete=True) == [PARTIALLY_GATED]


async def test_a_re_prediction_clears_the_verdict(seed, app_engine, lab):
    """v2 change. v1's persist upserted the prediction without the gate
    fields, so re-predicting cleared the verdict and the dive dropped off the
    landing page until the gate came back. Predictions are append-only in v2:
    the newer row, with no verdict, is the one the gate reads."""
    image = await seed.image(1)
    await seed.label("laser_labels", image, project_id=FULLY_GATED)
    await seed.prediction(image, gate_verdict="auto_accepted")
    assert await _ids(app_engine, lab, gated=True) == [FULLY_GATED]

    await seed.prediction(image, gate_verdict=None)

    assert await _ids(app_engine, lab, gated=True) == []
    assert await _ids(app_engine, lab, gated=False) == [FULLY_GATED]


async def test_a_later_verdict_judges_the_frame(seed, app_engine, lab):
    """The other direction: a verdict appended after an unjudged prediction
    is the current answer, so the project becomes gated."""
    image = await seed.image(1)
    await seed.label("laser_labels", image, project_id=FULLY_GATED)
    await seed.prediction(image, gate_verdict=None)
    await seed.prediction(image, gate_verdict="off_line")
    assert await _ids(app_engine, lab, gated=True) == [FULLY_GATED]


@pytest.mark.parametrize("kind", ["head_tail", "species", "slate"])
async def test_gated_is_refused_for_a_kind_with_no_gate(app_engine, lab, kind):
    """v2 change. v1 gave those endpoints no `gated` flag at all, "rather than
    accepting one that could never be satisfied" and silently blanking a
    section. One query serves every kind here, so the flag is refused."""
    with pytest.raises(GateNotApplicable):
        await _ids(app_engine, lab, kind, gated=True)


# -- superseded rows (test_label_studio_project_ids_superseded.py) ---------------


async def test_laser_superseded_only_project_excluded(seed, app_engine, lab):
    one, two = await seed.image(11), await seed.image(12)
    # project 42: one live label -> should surface
    await seed.label("laser_labels", one, project_id=42, completed=False)
    # project 99: only a superseded label -> should NOT surface
    await seed.label("laser_labels", two, project_id=99, completed=False,
                     superseded=True)  # fmt: skip

    assert await _ids(app_engine, lab) == [42]
    assert await _ids(app_engine, lab, incomplete=True) == [42]


async def test_laser_live_projects_still_returned(seed, app_engine, lab):
    one, two = await seed.image(11), await seed.image(12)
    await seed.label("laser_labels", one, project_id=42, completed=True)
    await seed.label("laser_labels", two, project_id=43, completed=False)

    assert await _ids(app_engine, lab) == [42, 43]
    # incomplete filter unchanged: only the project with an incomplete row
    assert await _ids(app_engine, lab, incomplete=True) == [43]


async def test_laser_project_with_mixed_rows_survives_if_any_live(
    seed, app_engine, lab
):
    """A project keeps surfacing as long as it has at least one
    non-superseded label, even if others are superseded."""
    one, two = await seed.image(11), await seed.image(12)
    await seed.label("laser_labels", one, project_id=42, superseded=True)
    await seed.label("laser_labels", two, project_id=42)

    assert await _ids(app_engine, lab) == [42]


@pytest.mark.parametrize(
    "kind, table",
    [
        ("head_tail", "head_tail_labels"),
        ("species", "species_labels"),
        ("slate", "slate_labels"),
    ],
)
async def test_every_kind_excludes_superseded_only_projects(
    seed, app_engine, lab, kind, table
):
    """v1 pinned this for headtail "for parity"; species and dive-slate had
    the same filter. One query now serves all four, so all four are pinned."""
    one, two = await seed.image(11), await seed.image(12)
    await seed.label(table, one, project_id=71, completed=False)
    await seed.label(table, two, project_id=88, completed=False, superseded=True)

    assert await _ids(app_engine, lab, kind) == [71]
    assert await _ids(app_engine, lab, kind, incomplete=True) == [71]


async def test_each_kind_reads_only_its_own_labels(seed, app_engine, lab):
    image = await seed.image(1)
    await seed.label("laser_labels", image, project_id=1)
    await seed.label("head_tail_labels", image, project_id=2)
    await seed.label("species_labels", image, project_id=3)
    await seed.label("slate_labels", image, project_id=4)

    assert [
        await _ids(app_engine, lab, kind)
        for kind in ("laser", "head_tail", "species", "slate")
    ] == [[1], [2], [3], [4]]


async def test_a_sentinel_is_no_project(seed, app_engine, lab, owner_engine):
    """A sentinel (no Label Studio project) is never a card."""
    image = await seed.image(1)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO laser_labels (tenant_id, capture_id, source, completed) "
                "VALUES (:t, :c, 'import', false)"
            ),
            {"t": lab, "c": image},
        )
    assert await _ids(app_engine, lab) == []


async def test_another_tenants_projects_are_not_listed(
    seed, app_engine, lab, owner_engine
):
    async with owner_engine.begin() as conn:
        partner = await _one(
            conn,
            "INSERT INTO tenants (slug, name) VALUES ('partner', 'p') RETURNING id",
        )
    await seed.label("laser_labels", await seed.image(1), project_id=42)
    await seed.label(
        "laser_labels",
        await seed.image(2, tenant=partner),
        project_id=77,
        tenant=partner,
    )

    assert await _ids(app_engine, lab) == [42]
    assert await _ids(app_engine, partner) == [77]


# -- dives, by number --------------------------------------------------------------


async def test_dives_are_listed_by_number_with_what_the_portal_shows(
    seed, app_engine, lab
):
    slate = await seed.slate_template(9005)
    source = await seed.dive(1, name="slate dive", slate=slate)
    await seed.dive(2, name="fish dive", source=source)

    async with tenant_transaction(app_engine, lab) as conn:
        dives = await list_dives(conn, lab)

    assert [
        (d.number, d.name, d.slate_template_number, d.calibration_source_number)
        for d in dives
    ] == [(1, "slate dive", 9005, None), (2, "fish dive", None, 1)]
    assert dives[0].dived_at == T0
    assert dives[0].priority == "low"


async def test_another_tenants_dives_are_not_listed(
    seed, app_engine, lab, owner_engine
):
    async with owner_engine.begin() as conn:
        partner = await _one(
            conn,
            "INSERT INTO tenants (slug, name) VALUES ('partner', 'p') RETURNING id",
        )
    await seed.dive(1)
    await seed.dive(2, tenant=partner)

    async with tenant_transaction(app_engine, lab) as conn:
        assert [d.number for d in await list_dives(conn, lab)] == [1]


# -- the calibration-source link (test_calibration_source_endpoints.py) -----------


async def _set(app_engine, tenant, number, source):
    async with tenant_transaction(app_engine, tenant) as conn:
        return await set_calibration_source(conn, tenant, number, source)


async def _clear(app_engine, tenant, number):
    async with tenant_transaction(app_engine, tenant) as conn:
        await clear_calibration_source(conn, tenant, number)


async def test_set_calibration_source_links_the_dives(seed, app_engine, lab):
    await seed.dive(1)
    two = await seed.dive(2)

    returned = await _set(app_engine, lab, 2, 1)

    assert returned.number == 2
    assert returned.calibration_source_number == 1
    assert await seed.calibration_source(two) == 1


async def test_set_calibration_source_rejects_self_link(seed, app_engine, lab):
    await seed.dive(1)

    with pytest.raises(SelfLink):
        await _set(app_engine, lab, 1, 1)


async def test_set_calibration_source_404_when_source_missing(seed, app_engine, lab):
    await seed.dive(1)

    with pytest.raises(DiveNotFound) as error:
        await _set(app_engine, lab, 1, 999)
    assert error.value.which == "calibration source dive"


async def test_set_calibration_source_404_when_dive_missing(seed, app_engine, lab):
    await seed.dive(1)

    with pytest.raises(DiveNotFound) as error:
        await _set(app_engine, lab, 999, 1)
    assert error.value.which == "dive"


async def test_a_dive_cannot_borrow_another_tenants_calibration(
    seed, app_engine, lab, owner_engine
):
    """v2 change (PLAN.md §9.17): borrowing stays within a tenant. Another
    tenant's dive is not merely refused, it is not there to be found."""
    async with owner_engine.begin() as conn:
        partner = await _one(
            conn,
            "INSERT INTO tenants (slug, name) VALUES ('partner', 'p') RETURNING id",
        )
    mine = await seed.dive(1)
    await seed.dive(2, tenant=partner)

    with pytest.raises(DiveNotFound):
        await _set(app_engine, lab, 1, 2)
    assert await seed.calibration_source(mine) is None


async def test_relinking_replaces_the_source(seed, app_engine, lab):
    await seed.dive(1)
    await seed.dive(3)
    two = await seed.dive(2)

    await _set(app_engine, lab, 2, 1)
    await _set(app_engine, lab, 2, 3)

    assert await seed.calibration_source(two) == 3


async def test_clear_calibration_source_unlinks(seed, app_engine, lab):
    source = await seed.dive(1)
    two = await seed.dive(2, source=source)

    await _clear(app_engine, lab, 2)

    assert await seed.calibration_source(two) is None


async def test_clear_calibration_source_is_idempotent(seed, app_engine, lab):
    one = await seed.dive(1)

    await _clear(app_engine, lab, 1)  # already null

    assert await seed.calibration_source(one) is None


async def test_clear_calibration_source_404_when_dive_missing(app_engine, lab):
    with pytest.raises(DiveNotFound):
        await _clear(app_engine, lab, 999)


async def test_the_store_scopes_to_the_tenant_itself_not_only_through_rls(
    seed, lab, owner_engine
):
    """The app layer is the first line of defence and RLS the backstop
    (PLAN.md §4.1). Read as the owner, which bypasses RLS, the queries still
    answer for the tenant they were given."""
    async with owner_engine.begin() as conn:
        partner = await _one(
            conn,
            "INSERT INTO tenants (slug, name) VALUES ('partner', 'p') RETURNING id",
        )
    source = await seed.dive(1)
    await seed.dive(2, source=source)
    partner_source = await seed.dive(3, tenant=partner)
    await seed.dive(4, tenant=partner, source=partner_source)
    await seed.label("laser_labels", await seed.image(1, source), project_id=42)
    await seed.label(
        "laser_labels", await seed.image(2, partner_source, tenant=partner), project_id=77,
        tenant=partner,
    )  # fmt: skip

    async with owner_engine.connect() as conn:
        assert await label_studio_project_ids(conn, lab, "laser") == [42]
        dives = await list_dives(conn, lab)
    assert [(d.number, d.calibration_source_number) for d in dives] == [
        (1, None),
        (2, 1),
    ]
