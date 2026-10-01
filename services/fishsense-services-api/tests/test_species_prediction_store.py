"""The BioCLIP species pre-annotation stage's database side, on real Postgres.

New in v2 (no v1 counterpart: v1 has no species model). Built as the head/tail
predict stage's store is (tests/test_headtail_store.py), and pinned here:

* the head/tail prediction records its kept mask's box (`mask_bbox`,
  migration 0033), additively: NULL where no mask was kept, and on every row
  written before it;
* **species predictions are appended, never updated** (0034, like every
  prediction table), and `current_species_predictions` is the latest per
  capture;
* **the cohort**: a canonical capture whose current head/tail prediction has
  a mask box and no current species prediction at the current version, cropped
  from that head/tail prediction. A fallback row (another version) or one
  cropped from a superseded head/tail prediction is stale; never-predicted
  dives go first;
* the processor's output is checked (PLAN.md §9.11): a prediction for a
  capture outside the dive, or cropped from another capture's head/tail
  prediction, is refused, and nothing is written;
* **pre-annotation only**: persisting predictions never writes or completes a
  species label;
* RLS scopes every row to its tenant, and the app role may only read and
  append.
"""

import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.headtail_store import (
    HeadTailPredictionRow,
    persist_headtail_predictions,
)
from fishsense_services_api.species_prediction_store import (
    ForeignCapture,
    ForeignHeadtailPrediction,
    InvalidSpeciesPredictions,
    SpeciesPredictionCatalog,
    SpeciesPredictionRow,
    next_dive_for_species_prediction,
    persist_species_predictions,
    species_predict_captures,
    species_prediction_state,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)
V = 1  # SPECIES_PREDICTOR_VERSION, which the orchestrator passes in
FALLBACK = -1
BOX = [100, 200, 300, 260]
HOGFISH = "Fish, Hogfish (Lachnolaimus maximus)"
ORCHESTRATOR = "service:fishsense-orchestrator"
_TASKS = itertools.count(50_000)


async def _exec(owner_engine, sql, **params):
    async with owner_engine.begin() as conn:
        result = await conn.execute(text(sql), params)
        return result.scalar_one() if result.returns_rows else None


async def _tenant(owner_engine, slug="lab") -> uuid.UUID:
    return await _exec(
        owner_engine,
        "INSERT INTO tenants (slug, name) VALUES (:s, :s) RETURNING id",
        s=slug,
    )


async def _dive(owner_engine, tenant, *, priority="high", at=T0):
    return await _exec(
        owner_engine,
        "INSERT INTO dives (tenant_id, source_path, name, dived_at, priority, "
        "created_at) VALUES (:t, :p, 'd', :at, :prio, :at) RETURNING id",
        t=tenant,
        p=f"/d/{uuid.uuid4()}",
        at=at,
        prio=priority,
    )


async def _capture(owner_engine, tenant, dive, *, canonical=True):
    return await _exec(
        owner_engine,
        "INSERT INTO captures (tenant_id, dive_id, source_path, captured_at, "
        "checksum, is_canonical) VALUES (:t, :d, :p, :at, :c, :canon) RETURNING id",
        t=tenant,
        d=dive,
        p=f"/c/{uuid.uuid4()}.ORF",
        at=T0,
        c=uuid.uuid4().hex,
        canon=canonical,
    )


async def _headtail(owner_engine, tenant, capture, *, box=BOX, status="predicted"):
    points = 1.0 if status == "predicted" else None
    return await _exec(
        owner_engine,
        "INSERT INTO head_tail_predictions (tenant_id, capture_id, predictor_version, "
        "status, head_x, head_y, tail_x, tail_y, mask_bbox) VALUES "
        "(:t, :c, 2, :s, :p, :p, :p, :p, :box) RETURNING id",
        t=tenant,
        c=capture,
        s=status,
        p=points,
        box=box,
    )


async def _species(owner_engine, tenant, capture, headtail, *, version=V,
                   status="predicted"):  # fmt: skip
    scored = status == "predicted"
    return await _exec(
        owner_engine,
        "INSERT INTO species_predictions (tenant_id, capture_id, "
        "headtail_prediction_id, status, predictor_version, model_id, "
        "predicted_choice, top1_probability, margin, top5) VALUES (:t, :c, :h, :s, "
        ":v, 'bioclip/2.5-vith14@x', :choice, :p, :m, CAST(:top5 AS jsonb)) "
        "RETURNING id",
        t=tenant,
        c=capture,
        h=headtail,
        s=status,
        v=version,
        choice=HOGFISH if scored else None,
        p=0.9 if scored else None,
        m=0.8 if scored else None,
        top5=json.dumps([{"choice": HOGFISH, "probability": 0.9}] if scored else []),
    )


async def _seed(owner_engine, tenant, *, at=T0, **dive):
    """A dive with one canonical capture whose head/tail mask has a box."""
    dive_id = await _dive(owner_engine, tenant, at=at, **dive)
    capture = await _capture(owner_engine, tenant, dive_id)
    headtail = await _headtail(owner_engine, tenant, capture)
    return dive_id, capture, headtail


async def _in(app_engine, tenant, fn, *args, **kwargs):
    async with tenant_transaction(app_engine, tenant) as conn:
        return await fn(conn, tenant, *args, **kwargs)


async def _next(app_engine, tenant):
    candidate = await _in(
        app_engine, tenant, next_dive_for_species_prediction, predictor_version=V
    )
    return None if candidate is None else candidate.dive_id


# -- the head/tail mask box (0033) ----------------------------------------------------


async def test_a_head_tail_prediction_persists_its_mask_box(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)
    capture = await _capture(owner_engine, lab, dive)
    row = HeadTailPredictionRow(
        capture_id=capture, status="predicted", head_x=1.0, head_y=2.0, tail_x=3.0,
        tail_y=4.0, predictor_version=2, mask_bbox=BOX,
    )  # fmt: skip

    await _in(app_engine, lab, persist_headtail_predictions, dive, [row])

    async with owner_engine.connect() as conn:
        current = (
            await conn.execute(
                text(
                    "SELECT mask_bbox FROM head_tail_predictions WHERE capture_id = :c"
                ),
                {"c": capture},
            )
        ).scalar_one()
    assert current == BOX


async def test_the_box_is_optional(owner_engine, app_engine):
    """Additive: an abstention has none, and neither does a row written before
    the column (or by a processor older than contract 5)."""
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)
    capture = await _capture(owner_engine, lab, dive)

    await _in(
        app_engine, lab, persist_headtail_predictions, dive,
        [HeadTailPredictionRow(capture_id=capture, status="no_detections",
                               predictor_version=2)],
    )  # fmt: skip

    async with owner_engine.connect() as conn:
        box = await conn.execute(
            text("SELECT mask_bbox FROM head_tail_predictions WHERE capture_id = :c"),
            {"c": capture},
        )
        assert box.scalar_one() is None


async def test_a_box_is_four_pixels_or_none(owner_engine):
    lab = await _tenant(owner_engine)
    capture = await _capture(owner_engine, lab, await _dive(owner_engine, lab))
    with pytest.raises(IntegrityError):
        await _headtail(owner_engine, lab, capture, box=[1, 2, 3])


# -- the cohort -----------------------------------------------------------------------


async def test_selects_a_boxed_fish_with_no_species_prediction(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive, _, _ = await _seed(owner_engine, lab)

    assert await _next(app_engine, lab) == dive


async def test_a_head_tail_abstention_has_no_fish_to_classify(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    capture = await _capture(owner_engine, lab, await _dive(owner_engine, lab))
    await _headtail(owner_engine, lab, capture, box=None, status="no_detections")

    assert await _next(app_engine, lab) is None


async def test_only_the_current_head_tail_prediction_counts(owner_engine, app_engine):
    """A later head/tail prediction without a mask (a corrected dot that now
    misses the fish) means there is no fish to classify, whatever an older
    row said."""
    lab = await _tenant(owner_engine)
    _, capture, _ = await _seed(owner_engine, lab)
    await _headtail(owner_engine, lab, capture, box=None, status="laser_off_all_fish")

    assert await _next(app_engine, lab) is None


async def test_drops_out_once_predicted_at_the_current_version(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    _, capture, headtail = await _seed(owner_engine, lab)
    await _species(owner_engine, lab, capture, headtail)

    assert await _next(app_engine, lab) is None


async def test_an_abstention_is_a_prediction_too(owner_engine, app_engine):
    """The cohort selects on a row's absence: an unrecorded decode failure
    would be re-predicted every hour."""
    lab = await _tenant(owner_engine)
    _, capture, headtail = await _seed(owner_engine, lab)
    await _species(owner_engine, lab, capture, headtail, status="decode_failed")

    assert await _next(app_engine, lab) is None


@pytest.mark.parametrize("version", [FALLBACK, V + 1])
async def test_another_version_is_stale(owner_engine, app_engine, version):
    lab = await _tenant(owner_engine)
    dive, capture, headtail = await _seed(owner_engine, lab)
    await _species(owner_engine, lab, capture, headtail, version=version)

    assert await _next(app_engine, lab) == dive


async def test_a_prediction_cropped_from_an_older_head_tail_row_is_stale(
    owner_engine, app_engine
):
    """A new head/tail prediction (a new SAM version, a corrected dot) may
    have kept another fish: the species row cropped from the old one no
    longer describes it."""
    lab = await _tenant(owner_engine)
    dive, capture, old = await _seed(owner_engine, lab)
    await _species(owner_engine, lab, capture, old)
    await _headtail(owner_engine, lab, capture, box=[10, 10, 90, 50])

    assert await _next(app_engine, lab) == dive


async def test_only_the_current_species_prediction_counts(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, capture, headtail = await _seed(owner_engine, lab)
    await _species(owner_engine, lab, capture, headtail)
    await _species(owner_engine, lab, capture, headtail, version=FALLBACK)

    assert await _next(app_engine, lab) == dive


async def test_only_high_priority_canonical_captures(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    await _seed(owner_engine, lab, priority="low")
    dive = await _dive(owner_engine, lab)
    duplicate = await _capture(owner_engine, lab, dive, canonical=False)
    await _headtail(owner_engine, lab, duplicate)

    assert await _next(app_engine, lab) is None


async def test_never_predicted_first_then_the_oldest(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    upgrade, capture, headtail = await _seed(owner_engine, lab, at=T0)
    await _species(owner_engine, lab, capture, headtail, version=FALLBACK)
    newer, _, _ = await _seed(owner_engine, lab, at=T0 + timedelta(hours=2))
    older, _, _ = await _seed(owner_engine, lab, at=T0 + timedelta(hours=1))

    candidate = await _in(
        app_engine, lab, next_dive_for_species_prediction, predictor_version=V
    )
    assert (candidate.dive_id, candidate.never_predicted) == (older, True)
    assert newer != older and upgrade != older


async def test_an_upgrade_only_dive_is_selected_when_nothing_else_needs_one(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive, capture, headtail = await _seed(owner_engine, lab)
    await _species(owner_engine, lab, capture, headtail, version=FALLBACK)

    candidate = await _in(
        app_engine, lab, next_dive_for_species_prediction, predictor_version=V
    )
    assert (candidate.dive_id, candidate.never_predicted) == (dive, False)


# -- the resolver mirrors the cohort ---------------------------------------------------


async def test_resolves_each_fish_with_its_box_and_crop_source(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive, fresh, fresh_ht = await _seed(owner_engine, lab)
    stale = await _capture(owner_engine, lab, dive)
    stale_ht = await _headtail(owner_engine, lab, stale, box=[1, 2, 30, 40])
    await _species(owner_engine, lab, stale, stale_ht, version=FALLBACK)
    done = await _capture(owner_engine, lab, dive)
    await _species(owner_engine, lab, done, await _headtail(owner_engine, lab, done))
    no_fish = await _capture(owner_engine, lab, dive)
    await _headtail(owner_engine, lab, no_fish, box=None, status="no_detections")

    captures = await _in(
        app_engine, lab, species_predict_captures, dive, predictor_version=V
    )

    assert [(c.capture_id, c.headtail_prediction_id, c.mask_bbox,
             c.has_existing_prediction) for c in captures] == [
        (fresh, fresh_ht, BOX, False),
        (stale, stale_ht, [1, 2, 30, 40], True),
    ]  # fmt: skip
    assert all(len(c.checksum) == 32 and c.from_v1 is False for c in captures)


# -- persisting ------------------------------------------------------------------------


def _row(capture, headtail, **overrides) -> SpeciesPredictionRow:
    values = {
        "capture_id": capture,
        "headtail_prediction_id": headtail,
        "status": "predicted",
        "predicted_choice": HOGFISH,
        "top1_probability": 0.91,
        "margin": 0.85,
        "top5": [{"choice": HOGFISH, "probability": 0.91}],
        "predictor_version": V,
        "model_id": "bioclip/2.5-vith14@0123456789ab",
    }
    values.update(overrides)
    return SpeciesPredictionRow(**values)


async def _current(owner_engine, capture):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT * FROM current_species_predictions WHERE capture_id = :c"),
                {"c": capture},
            )
        ).one()


async def test_persist_appends_and_the_latest_is_current(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, capture, headtail = await _seed(owner_engine, lab)

    for p in (0.5, 0.9):
        assert await _in(
            app_engine, lab, persist_species_predictions, dive,
            [_row(capture, headtail, top1_probability=p)],
        ) == 1  # fmt: skip

    current = await _current(owner_engine, capture)
    assert (current.top1_probability, current.predicted_choice) == (0.9, HOGFISH)
    assert current.headtail_prediction_id == headtail
    assert current.top5 == [{"choice": HOGFISH, "probability": 0.91}]
    assert current.model_id == "bioclip/2.5-vith14@0123456789ab"
    async with owner_engine.connect() as conn:
        count = await conn.execute(
            text("SELECT count(*) FROM species_predictions WHERE capture_id = :c"),
            {"c": capture},
        )
        assert count.scalar_one() == 2
    assert await _next(app_engine, lab) is None


async def test_an_abstention_is_recorded_without_scores(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, capture, headtail = await _seed(owner_engine, lab)

    await _in(
        app_engine, lab, persist_species_predictions, dive,
        [_row(capture, headtail, status="decode_failed", predicted_choice=None,
              top1_probability=None, margin=None, top5=[])],
    )  # fmt: skip

    assert (await _current(owner_engine, capture)).status == "decode_failed"


async def test_a_prediction_without_its_scores_is_refused(owner_engine):
    lab = await _tenant(owner_engine)
    _, capture, headtail = await _seed(owner_engine, lab)
    with pytest.raises(IntegrityError):
        await _exec(
            owner_engine,
            "INSERT INTO species_predictions (tenant_id, capture_id, "
            "headtail_prediction_id, status, predictor_version, model_id) "
            "VALUES (:t, :c, :h, 'predicted', 1, 'm')",
            t=lab,
            c=capture,
            h=headtail,
        )


async def test_a_prediction_for_a_capture_outside_the_dive_is_refused(
    owner_engine, app_engine
):
    """PLAN.md §9.11: the processor's output is checked, all or nothing."""
    lab = await _tenant(owner_engine)
    dive, capture, headtail = await _seed(owner_engine, lab)
    _, elsewhere, elsewhere_ht = await _seed(owner_engine, lab)

    with pytest.raises(ForeignCapture):
        await _in(
            app_engine, lab, persist_species_predictions, dive,
            [_row(capture, headtail), _row(elsewhere, elsewhere_ht)],
        )  # fmt: skip

    async with owner_engine.connect() as conn:
        count = await conn.execute(text("SELECT count(*) FROM species_predictions"))
        assert count.scalar_one() == 0


async def test_a_prediction_cropped_from_another_captures_mask_is_refused(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive, capture, _ = await _seed(owner_engine, lab)
    other = await _capture(owner_engine, lab, dive)
    others_headtail = await _headtail(owner_engine, lab, other)

    with pytest.raises(ForeignHeadtailPrediction):
        await _in(
            app_engine, lab, persist_species_predictions, dive,
            [_row(capture, others_headtail)],
        )  # fmt: skip
    assert issubclass(ForeignHeadtailPrediction, InvalidSpeciesPredictions)


async def test_persisting_never_writes_a_species_label(owner_engine, app_engine):
    """Pre-annotation only: a human confirms every label, and
    `pre_annotation` stays reserved and unwritten."""
    lab = await _tenant(owner_engine)
    dive, capture, headtail = await _seed(owner_engine, lab)

    await _in(
        app_engine, lab, persist_species_predictions, dive, [_row(capture, headtail)]
    )

    async with owner_engine.connect() as conn:
        labels = await conn.execute(
            text("SELECT count(*) FROM species_labels WHERE tenant_id = :t"),
            {"t": lab},
        )
        assert labels.scalar_one() == 0


# -- tenancy and append-only -------------------------------------------------------------


async def test_a_tenant_sees_only_its_own_predictions(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    reef = await _tenant(owner_engine, "reef")
    _, capture, headtail = await _seed(owner_engine, reef)
    await _species(owner_engine, reef, capture, headtail)

    async with tenant_transaction(app_engine, lab) as conn:
        for relation in ("species_predictions", "current_species_predictions"):
            seen = await conn.execute(text(f"SELECT count(*) FROM {relation}"))
            assert seen.scalar_one() == 0, relation


@pytest.mark.parametrize(
    "statement",
    ["UPDATE species_predictions SET margin = 0",
     "DELETE FROM species_predictions"],
)  # fmt: skip
async def test_the_app_role_may_only_append(owner_engine, app_engine, statement):
    lab = await _tenant(owner_engine)
    _, capture, headtail = await _seed(owner_engine, lab)
    await _species(owner_engine, lab, capture, headtail)

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_transaction(app_engine, lab) as conn:
            await conn.execute(text(statement))


# -- what populate and the backfill read --------------------------------------------------


async def _species_label(owner_engine, tenant, capture, *, project=None, task=None,
                         completed=False, superseded=False):  # fmt: skip
    return await _exec(
        owner_engine,
        "INSERT INTO species_labels (tenant_id, capture_id, source, ls_project_id, "
        "ls_task_id, completed, superseded) VALUES (:t, :c, 'human', :p, :k, :done, "
        ":gone) RETURNING id",
        t=tenant,
        c=capture,
        p=project,
        k=task,
        done=completed,
        gone=superseded,
    )


async def test_the_state_is_current_predictions_and_live_tasks(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive, capture, headtail = await _seed(owner_engine, lab)
    await _species(owner_engine, lab, capture, headtail, version=FALLBACK)
    latest = await _species(owner_engine, lab, capture, headtail)
    task = next(_TASKS)
    await _species_label(owner_engine, lab, capture, project=7, task=task)
    await _species_label(owner_engine, lab, capture, project=8, task=next(_TASKS),
                         superseded=True)  # fmt: skip
    _, elsewhere, elsewhere_ht = await _seed(owner_engine, lab)
    await _species(owner_engine, lab, elsewhere, elsewhere_ht)

    state = await _in(app_engine, lab, species_prediction_state, dive)

    number = await _exec(owner_engine, "SELECT number FROM dives WHERE id = :d", d=dive)
    assert state.dive_number == number
    (prediction,) = state.predictions
    assert (prediction.id, prediction.capture_id, prediction.predictor_version) == (
        latest,
        capture,
        V,
    )
    assert (prediction.predicted_choice, prediction.top1_probability) == (HOGFISH, 0.9)
    assert [(t.capture_id, t.ls_project_id, t.ls_task_id, t.completed)
            for t in state.tasks] == [(capture, 7, task, False)]  # fmt: skip


async def test_the_catalog_acts_only_in_tenants_it_is_a_member_of(
    owner_engine, app_engine, seed_memberships
):
    tenants = await seed_memberships({ORCHESTRATOR: {"lab": "service"}})
    lab = tenants["lab"]
    reef = await _tenant(owner_engine, "reef")
    for tenant in (lab, reef):
        await _seed(owner_engine, tenant)
    catalog = SpeciesPredictionCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.member_tenants() == [lab]
    assert await catalog.next_dive_for_species_prediction(lab, predictor_version=V)
    with pytest.raises(PermissionError):
        await catalog.next_dive_for_species_prediction(reef, predictor_version=V)
