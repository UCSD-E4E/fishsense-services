"""The database side of the laser slice, tenant-scoped, on real Postgres.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api/tests/:
test_select_next_dive_endpoints.py (the laser_preprocessing_*,
laser_prediction_*, needing_laser_population_* and auto_accept_* tests),
test_laser_prediction_reprediction_cohort.py, test_laser_cohort_needs_reprocess.py,
test_laser_needs_reprocess_endpoints.py (+ the clear-scope tests),
test_laser_prediction_endpoint.py, test_dives_with_complete_laser_labeling_endpoint.py,
test_laser_labels_include_superseded.py, test_laser_superseded_reason.py,
test_label_studio_project_ids_gated.py / _superseded.py and
test_dive_laser_line_endpoint.py. Names, fixtures' shapes and reasons are v1's.

v2 changes, each pinned here:

* every cohort is per tenant, and orders by (created_at, number) -- `number` is
  v1's dive id for a migrated dive, whose rows share one `created_at`, so v1's
  lowest-id order survives the migration (the clustering store's uuid
  tiebreak would not);
* **the gate's verdict is appended, never written over the prediction**
  (laser_predictions is append-only): only a verdict that changed is appended,
  a new prediction reads as unjudged, and a migrated row's own verdict counts
  until the gate re-judges it;
* **the dive line is appended only when the fit changed** (dive_laser_lines is
  append-only; v1 rewrote it on every hourly run);
* the validator's write supersedes only still-live rows and records why; a
  revival (remediation) refuses if the dive changed since it was planned;
* **populate never re-opens a label.** v1's PUT re-wrote an existing
  (image, project) row to a fresh placeholder, which could un-supersede a
  validator-superseded label; v2 records a task's row only where there is none;
* the processor's output is checked: a prediction, verdict or supersede that
  names a row outside the dive is refused and nothing is written.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.laser_store import (
    ForeignRows,
    GateVerdict,
    LaserCatalog,
    LineFit,
    NewLaserPrediction,
    PopulatedLabel,
    PopulationChanged,
    apply_laser_validation,
    clear_laser_reprocess_flags,
    dive_camera,
    dives_needing_laser_population,
    dives_with_complete_laser_labeling,
    laser_gate_inputs,
    laser_label_population,
    laser_label_studio_project_ids,
    laser_populate_items,
    laser_predict_captures,
    laser_preprocess_captures,
    laser_task_targets,
    mark_laser_labels_auto_accepted,
    next_dive_for_laser_auto_accept,
    next_dive_for_laser_prediction,
    next_dive_for_laser_preprocessing,
    persist_laser_predictions,
    raise_laser_reprocess_flags,
    record_laser_gate_verdicts,
    record_populated_laser_labels,
    revive_laser_labels,
)

T0 = datetime(2025, 3, 6, 17, 0, 15, tzinfo=UTC)
ORCHESTRATOR = "service:fishsense-orchestrator"
K = [[3000.0, 0.0, 2000.0], [0.0, 3000.0, 1500.0], [0.0, 0.0, 1.0]]
D = [-0.05, 0.01, 0.0, 0.0, 0.0]


# -- seeding (as the owner, like an admin or migrate-v1) -------------------------


class Seed:
    """A tenant's rows, written as the schema owner."""

    def __init__(self, owner_engine, tenant):
        self.owner = owner_engine
        self.tenant = tenant
        self._path = 0

    async def _one(self, sql, **params):
        async with self.owner.begin() as conn:
            result = await conn.execute(text(sql), params)
            return result.scalar() if result.returns_rows else None

    async def dive(self, *, priority="high", number=None, created_at=None, device=None):
        self._path += 1
        return await self._one(
            "INSERT INTO dives (tenant_id, source_path, dived_at, priority, number, "
            "device_id, created_at) VALUES (:t, :p, :at, :pr, :n, :dev, "
            "coalesce(:c, now())) RETURNING id",
            t=self.tenant, p=f"/{uuid.uuid4()}", at=T0, pr=priority, n=number,
            dev=device, c=created_at,
        )  # fmt: skip

    async def capture(self, dive, *, canonical=True, checksum=None, number=None,
                      v1_id=None):  # fmt: skip
        return await self._one(
            "INSERT INTO captures (tenant_id, dive_id, source_path, captured_at, "
            "checksum, is_canonical, number, v1_id) VALUES (:t, :d, :p, :at, :c, "
            ":canon, :n, :v1) RETURNING id",
            t=self.tenant, d=dive, p=f"/{uuid.uuid4()}.ORF", at=T0,
            c=checksum or uuid.uuid4().hex, canon=canonical, n=number, v1=v1_id,
        )  # fmt: skip

    async def label(self, capture, *, project=43, task=None, completed=False,
                    superseded=False, x=None, y=None, needs_reprocess=False,
                    number=None, source="human"):  # fmt: skip
        return await self._one(
            "INSERT INTO laser_labels (tenant_id, capture_id, source, ls_project_id, "
            "ls_task_id, completed, superseded, x, y, needs_reprocess, number) "
            "VALUES (:t, :c, :s, :p, :task, :done, :gone, :x, :y, :flag, :n) "
            "RETURNING id",
            t=self.tenant, c=capture, s=source, p=project, task=task,
            done=completed, gone=superseded, x=x, y=y, flag=needs_reprocess,
            n=number,
        )  # fmt: skip

    async def prediction(self, capture, *, x=10.0, y=20.0, version=2, color=None,
                         gate_verdict=None, auto_accept=False, v1_id=None,
                         width=4000, height=3000):  # fmt: skip
        return await self._one(
            "INSERT INTO laser_predictions (tenant_id, capture_id, x, y, "
            "predictor_version, color, gate_verdict, auto_accept, v1_id, width, "
            "height) VALUES (:t, :c, :x, :y, :v, :col, :g, :a, :v1, :w, :h) "
            "RETURNING id",
            t=self.tenant, c=capture, x=x, y=y, v=version, col=color,
            g=gate_verdict, a=auto_accept, v1=v1_id, w=width, h=height,
        )  # fmt: skip

    async def verdict(self, prediction, verdict, *, auto_accept=False):
        await self._one(
            "INSERT INTO laser_prediction_verdicts (tenant_id, prediction_id, "
            "auto_accept, gate_verdict) VALUES (:t, :p, :a, :g)",
            t=self.tenant, p=prediction, a=auto_accept, g=verdict,
        )  # fmt: skip

    async def slate_label(self, capture, *, completed=True, superseded=False):
        await self._one(
            "INSERT INTO slate_labels (tenant_id, capture_id, source, ls_project_id, "
            "completed, superseded) VALUES (:t, :c, 'human', 77, :done, :gone)",
            t=self.tenant, c=capture, done=completed, gone=superseded,
        )  # fmt: skip

    async def camera(self):
        device = await self._one(
            "INSERT INTO devices (tenant_id, kind, serial) VALUES (:t, 'lite', :s) "
            "RETURNING id",
            t=self.tenant, s=uuid.uuid4().hex,
        )  # fmt: skip
        await self._one(
            "INSERT INTO camera_calibrations (tenant_id, device_id, camera_matrix, "
            "distortion_coefficients) VALUES (:t, :d, CAST(:k AS jsonb), "
            "CAST(:dist AS jsonb))",
            t=self.tenant, d=device, k=json.dumps(K), dist=json.dumps(D),
        )  # fmt: skip
        return device

    async def rows(self, sql, **params):
        async with self.owner.connect() as conn:
            return (await conn.execute(text(sql), params)).mappings().all()


@pytest.fixture
async def lab(owner_engine) -> Seed:
    async with owner_engine.begin() as conn:
        tenant = (
            await conn.execute(
                text("INSERT INTO tenants (slug, name) VALUES ('lab', 'lab') "
                     "RETURNING id")
            )
        ).scalar_one()  # fmt: skip
    return Seed(owner_engine, tenant)


async def _as_tenant(app_engine, tenant, fn, *args, **kwargs):
    async with tenant_transaction(app_engine, tenant) as conn:
        return await fn(conn, tenant, *args, **kwargs)


async def _next(app_engine, lab, selector):
    candidate = await _as_tenant(app_engine, lab.tenant, selector)
    return None if candidate is None else candidate.dive_id


# -- stage 0.1: the laser-preprocessing cohort ---------------------------------


async def test_laser_preprocessing_picks_oldest_high_priority_with_unlabeled_images(
    lab, app_engine
):
    d1 = await lab.dive(number=1_000_005)
    d2 = await lab.dive(number=1_000_006)
    await lab.capture(d1)
    await lab.capture(d2)

    assert await _next(app_engine, lab, next_dive_for_laser_preprocessing) == d1


async def test_migrated_dives_keep_v1s_lowest_id_order(lab, app_engine):
    """v2: migrate-v1 inserts every dive in one transaction, so they share a
    `created_at`; the tiebreak is `number` (v1's id), never the uuid."""
    same = T0 - timedelta(days=30)
    later = await lab.dive(number=1_000_900, created_at=same)
    earlier = await lab.dive(number=1_000_012, created_at=same)
    for dive in (later, earlier):
        await lab.capture(dive)

    assert await _next(app_engine, lab, next_dive_for_laser_preprocessing) == earlier


async def test_laser_preprocessing_skips_dives_with_every_image_labeled(
    lab, app_engine
):
    labeled = await lab.dive()
    await lab.label(await lab.capture(labeled), completed=True, x=1.0, y=2.0)
    unlabeled = await lab.dive()
    await lab.capture(unlabeled)

    assert await _next(app_engine, lab, next_dive_for_laser_preprocessing) == unlabeled


async def test_laser_preprocessing_treats_incomplete_label_as_labeled(lab, app_engine):
    """Populate seeds incomplete rows; the JPEG is written by then."""
    dive = await lab.dive()
    await lab.label(await lab.capture(dive), completed=False)

    assert await _next(app_engine, lab, next_dive_for_laser_preprocessing) is None


async def test_laser_preprocessing_ignores_null_project_sentinels(lab, app_engine):
    """A sentinel (no project) is not a real label: the image still needs its JPEG."""
    dive = await lab.dive()
    await lab.label(await lab.capture(dive), project=None)

    assert await _next(app_engine, lab, next_dive_for_laser_preprocessing) == dive


async def test_laser_preprocessing_excludes_dive_when_sentinel_coexists_with_real_label(
    lab, app_engine
):
    dive = await lab.dive()
    capture = await lab.capture(dive)
    await lab.label(capture, project=None)
    await lab.label(capture, project=43)

    assert await _next(app_engine, lab, next_dive_for_laser_preprocessing) is None


async def test_laser_preprocessing_returns_none_with_no_high_priority(lab, app_engine):
    await lab.capture(await lab.dive(priority="low"))

    assert await _next(app_engine, lab, next_dive_for_laser_preprocessing) is None


async def test_laser_preprocessing_excludes_dive_with_no_images(lab, app_engine):
    await lab.dive()

    assert await _next(app_engine, lab, next_dive_for_laser_preprocessing) is None


async def test_laser_preprocessing_ignores_non_canonical_copies(lab, app_engine):
    dive = await lab.dive()
    await lab.capture(dive, canonical=False)

    assert await _next(app_engine, lab, next_dive_for_laser_preprocessing) is None


async def test_flagged_dive_is_selected_though_fully_labelled(lab, app_engine):
    dive = await lab.dive()
    await lab.label(await lab.capture(dive), needs_reprocess=True)

    assert await _next(app_engine, lab, next_dive_for_laser_preprocessing) == dive


async def test_a_flag_on_a_superseded_label_does_not_select(lab, app_engine):
    """The resolver hides superseded rows; selecting on one would wedge."""
    dive = await lab.dive()
    await lab.label(await lab.capture(dive), needs_reprocess=True, superseded=True)

    assert await _next(app_engine, lab, next_dive_for_laser_preprocessing) is None


async def test_flag_on_a_non_canonical_image_does_not_select(lab, app_engine):
    dive = await lab.dive()
    canonical = await lab.capture(dive)
    await lab.label(canonical)
    await lab.label(await lab.capture(dive, canonical=False), needs_reprocess=True)

    assert await _next(app_engine, lab, next_dive_for_laser_preprocessing) is None


async def test_clearing_the_flag_drains_the_dive(lab, app_engine):
    dive = await lab.dive()
    await lab.label(await lab.capture(dive), needs_reprocess=True)

    await _as_tenant(app_engine, lab.tenant, clear_laser_reprocess_flags, dive, None)

    assert await _next(app_engine, lab, next_dive_for_laser_preprocessing) is None


async def test_the_resolver_mirrors_the_selector(lab, app_engine):
    """Canonical images with no real label, or a live flagged one -- exactly
    the cohort's predicate, or a dive would be re-selected with no work."""
    dive = await lab.dive()
    unlabeled = await lab.capture(dive, checksum="a" * 32)
    labeled = await lab.capture(dive, checksum="b" * 32)
    flagged = await lab.capture(dive, checksum="c" * 32)
    sentinel_only = await lab.capture(dive, checksum="d" * 32)
    await lab.capture(dive, canonical=False, checksum="e" * 32)
    await lab.label(labeled)
    await lab.label(flagged, needs_reprocess=True)
    await lab.label(sentinel_only, project=None)

    captures = await _as_tenant(app_engine, lab.tenant, laser_preprocess_captures, dive)

    assert {c.capture_id for c in captures} == {unlabeled, flagged, sentinel_only}


async def test_a_capture_says_whether_it_came_from_v1(lab, app_engine):
    dive = await lab.dive()
    await lab.capture(dive, v1_id=4_004_242)

    (capture,) = await _as_tenant(
        app_engine, lab.tenant, laser_preprocess_captures, dive
    )

    assert capture.from_v1 and capture.number == 4_004_242


async def test_the_dive_camera_is_its_devices_current_calibration(lab, app_engine):
    device = await lab.camera()
    dive = await lab.dive(device=device)

    camera = await _as_tenant(app_engine, lab.tenant, dive_camera, dive)

    assert camera.camera_matrix == K
    assert camera.distortion_coefficients == D
    assert (
        await _as_tenant(app_engine, lab.tenant, dive_camera, await lab.dive()) is None
    )


# -- needs_reprocess -----------------------------------------------------------


async def test_raise_flags_the_dives_canonical_live_incomplete_labels(lab, app_engine):
    dive = await lab.dive()
    open_label = await lab.label(await lab.capture(dive))
    done = await lab.label(await lab.capture(dive), completed=True, x=1.0, y=1.0)
    gone = await lab.label(await lab.capture(dive), superseded=True)
    copy = await lab.label(await lab.capture(dive, canonical=False))
    other_dive = await lab.label(await lab.capture(await lab.dive()))

    raised = await _as_tenant(app_engine, lab.tenant, raise_laser_reprocess_flags, dive)

    flagged = {
        r["id"]
        for r in await lab.rows("SELECT id FROM laser_labels WHERE needs_reprocess")
    }
    assert raised == 1 and flagged == {open_label}
    assert not flagged & {done, gone, copy, other_dive}


async def test_raise_can_include_completed_labels(lab, app_engine):
    dive = await lab.dive()
    await lab.label(await lab.capture(dive), completed=True, x=1.0, y=1.0)

    assert (
        await _as_tenant(
            app_engine, lab.tenant, raise_laser_reprocess_flags, dive,
            only_incomplete=False,
        )
        == 1
    )  # fmt: skip


async def test_clear_is_scoped_to_the_captures_redrawn(lab, app_engine):
    """A flag raised while the child ran must survive (v1's clear scope; v1
    named the frames by checksum, one canonical capture per tenant)."""
    dive = await lab.dive()
    first = await lab.capture(dive)
    redrawn = await lab.label(first, needs_reprocess=True)
    raised_since = await lab.label(await lab.capture(dive), needs_reprocess=True)

    cleared = await _as_tenant(
        app_engine, lab.tenant, clear_laser_reprocess_flags, dive, [first]
    )

    still = {r["id"] for r in await lab.rows(
        "SELECT id FROM laser_labels WHERE needs_reprocess")}  # fmt: skip
    assert cleared == 1 and still == {raised_since} and redrawn not in still


async def test_an_empty_scope_clears_nothing_and_none_clears_the_dive(lab, app_engine):
    dive = await lab.dive()
    await lab.label(await lab.capture(dive), needs_reprocess=True)
    await lab.label(await lab.capture(dive), needs_reprocess=True, superseded=True,
                    completed=True)  # fmt: skip

    assert await _as_tenant(app_engine, lab.tenant, clear_laser_reprocess_flags,
                            dive, []) == 0  # fmt: skip
    # Clearing ignores superseded and completed: a flag nothing lowers wedges.
    assert await _as_tenant(app_engine, lab.tenant, clear_laser_reprocess_flags,
                            dive, None) == 2  # fmt: skip


# -- the laser-prediction cohort -------------------------------------------------


async def test_laser_prediction_selects_dive_with_unpredicted_unlabeled_image(
    lab, app_engine
):
    dive = await lab.dive()
    await lab.capture(dive)

    assert await _next(app_engine, lab, next_dive_for_laser_prediction) == dive


async def test_laser_prediction_none_when_all_predicted_or_labeled(lab, app_engine):
    dive = await lab.dive()
    await lab.prediction(await lab.capture(dive))
    await lab.label(await lab.capture(dive), completed=True, x=1.0, y=1.0)

    assert await _next(app_engine, lab, next_dive_for_laser_prediction) is None


async def test_laser_prediction_seeded_placeholder_does_not_exclude(lab, app_engine):
    """Dive 84 / project 274728: populate's placeholder carries a project."""
    dive = await lab.dive()
    await lab.label(await lab.capture(dive), completed=False)

    assert await _next(app_engine, lab, next_dive_for_laser_prediction) == dive


async def test_laser_prediction_superseded_completed_label_does_not_exclude(
    lab, app_engine
):
    dive = await lab.dive()
    await lab.label(await lab.capture(dive), completed=True, superseded=True)

    assert await _next(app_engine, lab, next_dive_for_laser_prediction) == dive


async def test_stale_prediction_on_an_actively_labeled_dive_is_selected(
    lab, app_engine
):
    dive = await lab.dive()
    capture = await lab.capture(dive)
    await lab.prediction(capture, version=1)
    await lab.label(capture, completed=False)

    assert await _next(app_engine, lab, next_dive_for_laser_prediction) == dive


async def test_null_version_counts_as_stale(lab, app_engine):
    dive = await lab.dive()
    capture = await lab.capture(dive)
    await lab.prediction(capture, version=None, v1_id=6_000_077)
    await lab.label(capture, completed=False)

    assert await _next(app_engine, lab, next_dive_for_laser_prediction) == dive


async def test_stale_prediction_on_a_finished_dive_is_left_alone(lab, app_engine):
    dive = await lab.dive()
    await lab.prediction(await lab.capture(dive), version=1)

    assert await _next(app_engine, lab, next_dive_for_laser_prediction) is None


async def test_a_humans_completed_label_protects_its_image(lab, app_engine):
    """The dive is still being labeled, but the only stale prediction sits on
    an image a human finished: never re-predict over finished work."""
    dive = await lab.dive()
    capture = await lab.capture(dive)
    await lab.prediction(capture, version=1)
    await lab.label(capture, completed=True, x=1.0, y=1.0, project=1)
    being_labeled = await lab.capture(dive)
    await lab.prediction(being_labeled)
    await lab.label(being_labeled, completed=False)

    assert await _next(app_engine, lab, next_dive_for_laser_prediction) is None


async def test_a_re_prediction_is_what_makes_a_prediction_current(lab, app_engine):
    """v2: predictions are appended; the latest per capture is current."""
    dive = await lab.dive()
    capture = await lab.capture(dive)
    await lab.prediction(capture, version=1)
    await lab.prediction(capture, version=2)
    await lab.label(capture, completed=False)

    assert await _next(app_engine, lab, next_dive_for_laser_prediction) is None


async def test_the_predict_resolver_mirrors_the_selector(lab, app_engine):
    dive = await lab.dive()
    fresh = await lab.capture(dive)
    stale = await lab.capture(dive)
    await lab.prediction(stale, version=1)
    current = await lab.capture(dive)
    await lab.prediction(current)
    human = await lab.capture(dive)
    await lab.label(human, completed=True, x=1.0, y=1.0)
    await lab.capture(dive, canonical=False)

    captures = await _as_tenant(app_engine, lab.tenant, laser_predict_captures, dive)

    assert {c.capture_id for c in captures} == {fresh, stale}


async def test_persist_appends_one_prediction_per_result(lab, app_engine):
    dive = await lab.dive()
    capture = await lab.capture(dive)
    old = await lab.prediction(capture, version=1)
    await lab.verdict(old, "auto_accepted", auto_accept=True)

    written = await _as_tenant(
        app_engine, lab.tenant, persist_laser_predictions, dive,
        [NewLaserPrediction(capture_id=capture, x=1.5, y=2.5, confidence=0.9,
                            width=4000, height=3000, color="green",
                            color_margin=-12.0, rejected_out_of_region=False,
                            predictor_version=2, checkpoint="run3_epoch_021.pt",
                            core_version="4.1.0")],
    )  # fmt: skip

    rows = await lab.rows(
        "SELECT * FROM current_laser_predictions_gated WHERE capture_id = :c",
        c=capture,
    )
    assert written == 1
    (current,) = rows
    assert (current["x"], current["color"], current["predictor_version"]) == (
        1.5, "green", 2)  # fmt: skip
    # A re-prediction clears the verdict: the new row has none (v1's rule).
    assert current["gate_verdict"] is None and current["auto_accept"] is False


async def test_persist_refuses_a_capture_of_another_dive(lab, app_engine):
    dive = await lab.dive()
    foreign = await lab.capture(await lab.dive())

    with pytest.raises(ForeignRows):
        await _as_tenant(
            app_engine, lab.tenant, persist_laser_predictions, dive,
            [NewLaserPrediction(capture_id=foreign, x=None, y=None, confidence=0.1,
                                predictor_version=2)],
        )  # fmt: skip
    assert not await lab.rows("SELECT id FROM laser_predictions")


# -- the auto-accept backlog cohort ---------------------------------------------


async def test_auto_accept_selects_a_dive_with_an_unjudged_prediction(lab, app_engine):
    dive = await lab.dive()
    await lab.prediction(await lab.capture(dive))

    assert await _next(app_engine, lab, next_dive_for_laser_auto_accept) == dive


async def test_auto_accept_skips_a_dive_whose_predictions_were_all_judged(
    lab, app_engine
):
    dive = await lab.dive()
    prediction = await lab.prediction(await lab.capture(dive))
    await lab.verdict(prediction, "off_line")

    assert await _next(app_engine, lab, next_dive_for_laser_auto_accept) is None


async def test_a_migrated_rows_own_verdict_counts_as_judged(lab, app_engine):
    dive = await lab.dive()
    await lab.prediction(
        await lab.capture(dive), gate_verdict="audit_sample", v1_id=6_000_009
    )

    assert await _next(app_engine, lab, next_dive_for_laser_auto_accept) is None


async def test_auto_accept_ignores_an_abstention_with_no_dot(lab, app_engine):
    """The gate writes only changed verdicts, so an abstention would never
    drain from an "unjudged" cohort."""
    dive = await lab.dive()
    await lab.prediction(await lab.capture(dive), x=None, y=None)

    assert await _next(app_engine, lab, next_dive_for_laser_auto_accept) is None


async def test_auto_accept_only_selects_high_priority_dives(lab, app_engine):
    await lab.prediction(await lab.capture(await lab.dive(priority="low")))

    assert await _next(app_engine, lab, next_dive_for_laser_auto_accept) is None


async def test_auto_accept_ignores_non_canonical_images(lab, app_engine):
    await lab.prediction(await lab.capture(await lab.dive(), canonical=False))

    assert await _next(app_engine, lab, next_dive_for_laser_auto_accept) is None


async def test_auto_accept_never_selects_an_older_or_unversioned_prediction(
    lab, app_engine
):
    dive = await lab.dive()
    await lab.prediction(await lab.capture(dive), version=1)
    await lab.prediction(await lab.capture(dive), version=None, v1_id=6_000_005)

    assert await _next(app_engine, lab, next_dive_for_laser_auto_accept) is None


async def test_auto_accept_selects_a_dive_only_partly_judged(lab, app_engine):
    dive = await lab.dive()
    judged = await lab.prediction(await lab.capture(dive))
    await lab.verdict(judged, "auto_accepted", auto_accept=True)
    await lab.prediction(await lab.capture(dive))

    assert await _next(app_engine, lab, next_dive_for_laser_auto_accept) == dive


async def test_a_re_prediction_re_arms_the_backlog(lab, app_engine):
    dive = await lab.dive()
    capture = await lab.capture(dive)
    await lab.verdict(await lab.prediction(capture), "auto_accepted", auto_accept=True)
    await lab.prediction(capture)

    assert await _next(app_engine, lab, next_dive_for_laser_auto_accept) == dive


# -- the gate's reads and writes ---------------------------------------------------


async def test_gate_inputs_are_the_dives_current_predictions(lab, app_engine):
    dive = await lab.dive(number=1_000_442)
    capture = await lab.capture(dive, number=5_132_158)
    await lab.prediction(capture, x=1.0, y=1.0)
    current = await lab.prediction(capture, x=5.0, y=6.0)

    inputs = await _as_tenant(app_engine, lab.tenant, laser_gate_inputs, dive)

    assert inputs.dive_number == 1_000_442
    ((prediction_id, capture_number, x, y, version),) = [
        (p.prediction_id, p.capture_number, p.x, p.y, p.predictor_version)
        for p in inputs.predictions
    ]
    assert (prediction_id, capture_number, x, y, version) == (
        current, 5_132_158, 5.0, 6.0, 2)  # fmt: skip


async def _gated(lab, prediction):
    (row,) = await lab.rows(
        "SELECT auto_accept, gate_verdict, line_offset_px, line_position_z "
        "FROM current_laser_predictions_gated WHERE id = :p",
        p=prediction,
    )
    return dict(row)


async def test_a_verdict_is_appended_never_written_over_the_prediction(lab, app_engine):
    dive = await lab.dive()
    prediction = await lab.prediction(await lab.capture(dive))

    written = await _as_tenant(
        app_engine, lab.tenant, record_laser_gate_verdicts, dive,
        [GateVerdict(prediction, True, "auto_accepted", 0.8, 1.2)],
    )  # fmt: skip

    assert written == 1
    assert await _gated(lab, prediction) == {
        "auto_accept": True, "gate_verdict": "auto_accepted",
        "line_offset_px": 0.8, "line_position_z": 1.2,
    }  # fmt: skip
    (raw,) = await lab.rows(
        "SELECT gate_verdict, auto_accept FROM laser_predictions WHERE id = :p",
        p=prediction,
    )
    assert raw["gate_verdict"] is None and raw["auto_accept"] is False


async def test_unchanged_verdicts_are_not_rewritten(lab, app_engine):
    """v1's `_changed`: margins compared at 1e-6, so a float round trip does
    not manufacture a write."""
    dive = await lab.dive()
    prediction = await lab.prediction(await lab.capture(dive))
    verdict = GateVerdict(prediction, True, "auto_accepted", 0.8, 1.2)
    await _as_tenant(
        app_engine, lab.tenant, record_laser_gate_verdicts, dive, [verdict]
    )

    again = await _as_tenant(
        app_engine, lab.tenant, record_laser_gate_verdicts, dive,
        [GateVerdict(prediction, True, "auto_accepted", 0.8 + 1e-9, 1.2)],
    )  # fmt: skip

    assert again == 0
    assert len(await lab.rows("SELECT id FROM laser_prediction_verdicts")) == 1


async def test_a_changed_verdict_clears_a_standing_auto_accept(lab, app_engine):
    """The safety case: a dive that loses consensus must not leave
    `auto_accept` standing on the strength of a line that no longer exists."""
    dive = await lab.dive()
    prediction = await lab.prediction(await lab.capture(dive))
    await lab.verdict(prediction, "auto_accepted", auto_accept=True)

    written = await _as_tenant(
        app_engine, lab.tenant, record_laser_gate_verdicts, dive,
        [GateVerdict(prediction, False, "dive_ineligible", None, None)],
    )  # fmt: skip

    assert written == 1
    assert (await _gated(lab, prediction))["auto_accept"] is False


async def test_a_migrated_verdict_is_what_a_new_one_is_compared_against(
    lab, app_engine
):
    dive = await lab.dive()
    prediction = await lab.prediction(
        await lab.capture(dive), gate_verdict="off_line", v1_id=6_000_031
    )

    unchanged = await _as_tenant(
        app_engine, lab.tenant, record_laser_gate_verdicts, dive,
        [GateVerdict(prediction, False, "off_line", None, None)],
    )  # fmt: skip

    assert unchanged == 0


async def test_a_verdict_for_a_prediction_outside_the_dive_is_refused(lab, app_engine):
    dive = await lab.dive()
    mine = await lab.prediction(await lab.capture(dive))
    foreign = await lab.prediction(await lab.capture(await lab.dive()))

    with pytest.raises(ForeignRows):
        await _as_tenant(
            app_engine, lab.tenant, record_laser_gate_verdicts, dive,
            [GateVerdict(mine, False, "off_line", 20.0, 0.1),
             GateVerdict(foreign, False, "off_line", 20.0, 0.1)],
        )  # fmt: skip
    assert not await lab.rows("SELECT id FROM laser_prediction_verdicts")


async def test_the_verdict_table_is_append_only_for_the_app(lab, app_engine):
    dive = await lab.dive()
    prediction = await lab.prediction(await lab.capture(dive))
    await lab.verdict(prediction, "off_line")

    async with tenant_transaction(app_engine, lab.tenant) as conn:
        denied = await conn.execute(
            text("SELECT has_table_privilege('laser_prediction_verdicts', 'UPDATE') "
                 "OR has_table_privilege('laser_prediction_verdicts', 'DELETE')")
        )  # fmt: skip
        assert denied.scalar_one() is False


# -- populate --------------------------------------------------------------------


async def test_needing_laser_population_lists_predicted_incomplete_dives(
    lab, app_engine
):
    needing = await lab.dive()
    await lab.prediction(await lab.capture(needing))
    done = await lab.dive()
    capture = await lab.capture(done)
    await lab.prediction(capture)
    await lab.label(capture, completed=True, x=1.0, y=1.0)
    await lab.capture(await lab.dive())  # not predicted

    dives = await _as_tenant(app_engine, lab.tenant, dives_needing_laser_population)

    assert [d.dive_id for d in dives] == [needing]


async def test_needing_laser_population_counts_a_superseded_completed_label(
    lab, app_engine
):
    """v1's cohort does not filter superseded here: a completed label, live or
    not, takes the image out."""
    dive = await lab.dive()
    capture = await lab.capture(dive)
    await lab.prediction(capture)
    await lab.label(capture, completed=True, superseded=True, x=1.0, y=1.0)

    assert (
        await _as_tenant(app_engine, lab.tenant, dives_needing_laser_population) == []
    )


async def test_populate_items_are_predicted_canonical_captures_without_a_live_label(
    lab, app_engine
):
    dive = await lab.dive(number=1_000_031)
    item = await lab.capture(dive, number=5_000_700)
    await lab.prediction(item, x=5.0, y=6.0, color="green")
    await lab.capture(dive)  # unpredicted: deferred, never seeded
    labeled = await lab.capture(dive)
    await lab.prediction(labeled, color="green")
    await lab.label(labeled, completed=True, x=1.0, y=1.0)
    copy = await lab.capture(dive, canonical=False)
    await lab.prediction(copy, color="red")

    population = await _as_tenant(app_engine, lab.tenant, laser_populate_items, dive)

    assert population.dive_number == 1_000_031
    ((capture_id, number, x, y),) = [
        (i.capture.capture_id, i.capture.number, i.x, i.y) for i in population.items
    ]
    assert (capture_id, number, x, y) == (item, 5_000_700, 5.0, 6.0)
    # Colour votes come from every prediction of the dive (v1's majority input).
    assert sorted(population.colors, key=str) == ["green", "green", "red"]


async def test_a_populate_item_carries_its_effective_auto_accept(lab, app_engine):
    dive = await lab.dive()
    capture = await lab.capture(dive)
    await lab.verdict(await lab.prediction(capture), "auto_accepted", auto_accept=True)

    (item,) = (
        await _as_tenant(app_engine, lab.tenant, laser_populate_items, dive)
    ).items

    assert item.auto_accept is True


async def test_populate_records_a_row_per_task_with_its_source(lab, app_engine):
    dive = await lab.dive()
    human, auto = await lab.capture(dive), await lab.capture(dive)

    written = await _as_tenant(
        app_engine, lab.tenant, record_populated_laser_labels,
        [PopulatedLabel(human, 500, 9001, "human"),
         PopulatedLabel(auto, 500, 9002, "auto_accept")],
    )  # fmt: skip

    rows = {
        r["capture_id"]: r
        for r in await lab.rows(
            "SELECT capture_id, ls_project_id, ls_task_id, source, completed "
            "FROM laser_labels"
        )
    }
    assert written == 2
    assert (rows[human]["ls_task_id"], rows[human]["source"]) == (9001, "human")
    assert rows[auto]["source"] == "auto_accept"
    assert not rows[auto]["completed"]


async def test_populate_never_re_opens_an_existing_label(lab, app_engine):
    """v2 fix. v1's PUT re-wrote an existing (image, project) row to a fresh
    placeholder -- superseded=False, x=None -- so a populate run that re-found
    a superseded label's task silently un-superseded it. The validator never
    un-supersedes, and neither does populate."""
    dive = await lab.dive()
    capture = await lab.capture(dive)
    label = await lab.label(capture, project=500, task=9001, completed=True,
                            superseded=True, x=1.0, y=2.0)  # fmt: skip

    written = await _as_tenant(
        app_engine, lab.tenant, record_populated_laser_labels,
        [PopulatedLabel(capture, 500, 9001, "human")],
    )  # fmt: skip

    (row,) = await lab.rows("SELECT * FROM laser_labels WHERE id = :l", l=label)
    assert written == 0
    assert (row["superseded"], row["completed"], row["x"]) == (True, True, 1.0)


# -- the task targets (backfill, apply auto-accept) ------------------------------


async def test_backfill_targets_are_open_tasks_with_a_placeable_prediction(
    lab, app_engine
):
    dive = await lab.dive(number=1_000_094)
    target = await lab.capture(dive)
    await lab.prediction(target, x=5.0, y=6.0)
    await lab.label(target, project=500, task=1)
    no_dot = await lab.capture(dive)
    await lab.prediction(no_dot, x=None, y=None)
    await lab.label(no_dot, project=500, task=2)
    done = await lab.capture(dive)
    await lab.prediction(done)
    await lab.label(done, project=500, task=3, completed=True, x=1.0, y=1.0)
    gone = await lab.capture(dive)
    await lab.prediction(gone)
    await lab.label(gone, project=500, task=4, superseded=True)

    targets = await _as_tenant(app_engine, lab.tenant, laser_task_targets, dive)

    assert targets.dive_number == 1_000_094
    assert [(t.ls_task_id, t.ls_project_id, t.x) for t in targets.targets] == [
        (1, 500, 5.0)
    ]


async def test_apply_targets_are_only_auto_accepted_predictions(lab, app_engine):
    dive = await lab.dive()
    cleared = await lab.capture(dive)
    await lab.verdict(await lab.prediction(cleared), "auto_accepted", auto_accept=True)
    await lab.label(cleared, project=500, task=1)
    dark_run = await lab.capture(dive)
    await lab.verdict(await lab.prediction(dark_run), "auto_accepted")
    await lab.label(dark_run, project=500, task=2)

    targets = await _as_tenant(
        app_engine, lab.tenant, laser_task_targets, dive, auto_accepted_only=True
    )

    assert [t.ls_task_id for t in targets.targets] == [1]


async def test_an_auto_accepted_task_is_marked_so(lab, app_engine):
    """The source says the gate confirmed it (port-plan: `auto_accept`)."""
    dive = await lab.dive()
    label = await lab.label(await lab.capture(dive), project=500, task=1)

    await _as_tenant(app_engine, lab.tenant, mark_laser_labels_auto_accepted, [1])

    (row,) = await lab.rows("SELECT source FROM laser_labels WHERE id = :l", l=label)
    assert row["source"] == "auto_accept"


# -- validation --------------------------------------------------------------------


async def test_dive_with_only_completed_labels_qualifies(lab, app_engine):
    dive = await lab.dive()
    await lab.label(await lab.capture(dive), completed=True, x=1.0, y=1.0)

    dives = await _as_tenant(app_engine, lab.tenant, dives_with_complete_laser_labeling)

    assert [d.dive_id for d in dives] == [dive]


async def test_dive_with_any_incomplete_label_excluded(lab, app_engine):
    dive = await lab.dive()
    await lab.label(await lab.capture(dive), completed=True, x=1.0, y=1.0)
    await lab.label(await lab.capture(dive), completed=False)

    assert (
        await _as_tenant(app_engine, lab.tenant, dives_with_complete_laser_labeling)
        == []
    )


async def test_superseded_incomplete_label_does_not_block(lab, app_engine):
    dive = await lab.dive()
    await lab.label(await lab.capture(dive), completed=True, x=1.0, y=1.0)
    await lab.label(await lab.capture(dive), completed=False, superseded=True)

    dives = await _as_tenant(app_engine, lab.tenant, dives_with_complete_laser_labeling)

    assert [d.dive_id for d in dives] == [dive]


async def test_dive_with_zero_or_only_superseded_labels_excluded(lab, app_engine):
    await lab.dive()
    only_gone = await lab.dive()
    await lab.label(await lab.capture(only_gone), completed=True, superseded=True)

    assert (
        await _as_tenant(app_engine, lab.tenant, dives_with_complete_laser_labeling)
        == []
    )


async def test_the_population_is_the_full_dive_superseded_included(lab, app_engine):
    """fishsense-lite #927: the validator judges every positive, superseded
    included; the order is imposed by the judgement, not trusted from here."""
    dive = await lab.dive()
    a = await lab.capture(dive, number=2_000_020)
    b = await lab.capture(dive, number=2_000_010)
    await lab.label(a, project=1, completed=True, x=1.0, y=1.0, number=3_000_003)
    await lab.label(a, project=2, completed=True, superseded=True, x=2.0, y=2.0,
                    number=3_000_001)  # fmt: skip
    await lab.label(b, completed=True, x=3.0, y=3.0, number=3_000_002)
    await lab.label(b, project=3, completed=False)  # no dot: not a positive
    await lab.slate_label(b)
    await lab.slate_label(a, completed=False)

    population = await _as_tenant(app_engine, lab.tenant, laser_label_population, dive)

    assert sorted((r.capture_number, r.number, r.superseded) for r in population.rows) == [
        (2_000_010, 3_000_002, False), (2_000_020, 3_000_001, True),
        (2_000_020, 3_000_003, False)]  # fmt: skip
    # Calibration frames: a completed, live slate label (what stage 13 reads).
    assert population.calibration_capture_numbers == [2_000_010]


def _line(**overrides):
    values = dict(a=0.4, b=-0.9, c=10.0, n_points=40, inlier_count=38,
                  inlier_fraction=0.95, residual_std=1.1, label_noise_mad=1.0,
                  line_confidence=120.0, noise_estimator="signed_residual_mad")  # fmt: skip
    values.update(overrides)
    return LineFit(**values)


async def test_validation_supersedes_live_rows_and_records_why(lab, app_engine):
    dive = await lab.dive()
    capture = await lab.capture(dive)
    fish = await lab.label(capture, project=1, completed=True, x=1.0, y=1.0)
    slate = await lab.label(capture, project=2, completed=True, x=1.0, y=1.0)

    written = await _as_tenant(
        app_engine, lab.tenant, apply_laser_validation, dive,
        [(fish, "validator_3sigma"), (slate, "validator_coarse_calibration")], None,
    )  # fmt: skip

    rows = {r["id"]: r for r in await lab.rows(
        "SELECT id, superseded, superseded_reason FROM laser_labels")}  # fmt: skip
    assert written.superseded == 2
    assert rows[fish]["superseded_reason"] == "validator_3sigma"
    assert rows[slate]["superseded_reason"] == "validator_coarse_calibration"
    assert all(r["superseded"] for r in rows.values())


async def test_validation_never_rewrites_an_already_superseded_row(lab, app_engine):
    """Its reason stays whoever superseded it (an operator's `manual`, say)."""
    dive = await lab.dive()
    label = await lab.label(await lab.capture(dive), completed=True, superseded=True,
                            x=1.0, y=1.0)  # fmt: skip
    async with lab.owner.begin() as conn:
        await conn.execute(text("UPDATE laser_labels SET superseded_reason = 'manual'"))

    written = await _as_tenant(
        app_engine, lab.tenant, apply_laser_validation, dive,
        [(label, "validator_3sigma")], None,
    )  # fmt: skip

    (row,) = await lab.rows("SELECT superseded_reason FROM laser_labels")
    assert written.superseded == 0 and row["superseded_reason"] == "manual"


async def test_validation_refuses_a_label_outside_the_dive(lab, app_engine):
    dive = await lab.dive()
    mine = await lab.label(await lab.capture(dive), completed=True, x=1.0, y=1.0)
    foreign = await lab.label(await lab.capture(await lab.dive()), completed=True,
                              x=1.0, y=1.0)  # fmt: skip

    with pytest.raises(ForeignRows):
        await _as_tenant(
            app_engine, lab.tenant, apply_laser_validation, dive,
            [(mine, "validator_3sigma"), (foreign, "validator_3sigma")], None,
        )  # fmt: skip
    assert not await lab.rows("SELECT id FROM laser_labels WHERE superseded")


async def test_the_line_is_appended_only_when_the_fit_changes(lab, app_engine):
    """v1 rewrote every complete dive's line hourly; appending each would add
    ~270 rows an hour for nothing. An unchanged fit appends nothing."""
    dive = await lab.dive()

    first = await _as_tenant(app_engine, lab.tenant, apply_laser_validation, dive,
                             [], _line())  # fmt: skip
    same = await _as_tenant(app_engine, lab.tenant, apply_laser_validation, dive,
                            [], _line())  # fmt: skip
    moved = await _as_tenant(app_engine, lab.tenant, apply_laser_validation, dive,
                             [], _line(c=11.0))  # fmt: skip

    lines = await lab.rows(
        "SELECT c, noise_estimator FROM dive_laser_lines ORDER BY seq"
    )
    assert (first.line_appended, same.line_appended, moved.line_appended) == (
        True, False, True)  # fmt: skip
    assert [r["c"] for r in lines] == [10.0, 11.0]
    assert {r["noise_estimator"] for r in lines} == {"signed_residual_mad"}
    (current,) = await lab.rows("SELECT c FROM current_dive_laser_lines")
    assert current["c"] == 11.0


async def test_the_current_views_show_the_columns_added_since_they_were_made(
    lab, app_engine
):
    """0009's and 0011's `SELECT *` views froze their column lists before
    0017 (`noise_estimator`) and 0019 (`number`); 0021 appends them, so a
    reader of the current line can tell which noise scale it was fitted on."""
    dive = await lab.dive()
    await lab.prediction(await lab.capture(dive))
    await _as_tenant(app_engine, lab.tenant, apply_laser_validation, dive, [], _line())

    (line,) = await lab.rows(
        "SELECT noise_estimator, number FROM current_dive_laser_lines"
    )
    (prediction,) = await lab.rows("SELECT number FROM current_laser_predictions")

    assert line["noise_estimator"] == "signed_residual_mad"
    assert line["number"] is not None and prediction["number"] is not None


async def test_a_migrated_line_with_the_old_estimator_is_replaced(lab, app_engine):
    """The same numbers under a different noise scale are a different fit."""
    dive = await lab.dive()
    async with lab.owner.begin() as conn:
        await conn.execute(
            text("INSERT INTO dive_laser_lines (tenant_id, dive_id, a, b, c, "
                 "n_points, inlier_count, inlier_fraction, residual_std, "
                 "label_noise_mad, line_confidence, noise_estimator) VALUES "
                 "(:t, :d, 0.4, -0.9, 10.0, 40, 38, 0.95, 1.1, 1.0, 120.0, "
                 "'absolute_residual_mad')"),
            {"t": lab.tenant, "d": dive},
        )  # fmt: skip

    written = await _as_tenant(app_engine, lab.tenant, apply_laser_validation, dive,
                               [], _line())  # fmt: skip

    assert written.line_appended


# -- remediation --------------------------------------------------------------------


async def test_revive_restores_exactly_the_reviewed_labels(lab, app_engine):
    dive = await lab.dive()
    capture = await lab.capture(dive)
    eroded = await lab.label(capture, project=1, completed=True, superseded=True,
                             x=1.0, y=1.0, number=3_000_041)  # fmt: skip
    other = await lab.label(capture, project=2, completed=True, superseded=True,
                            x=1.0, y=1.0, number=3_000_042)  # fmt: skip
    population = await _as_tenant(app_engine, lab.tenant, laser_label_population, dive)

    revived = await _as_tenant(app_engine, lab.tenant, revive_laser_labels, dive,
                               [3_000_041], population.fingerprint)  # fmt: skip

    rows = {r["id"]: r for r in await lab.rows(
        "SELECT id, superseded, superseded_reason FROM laser_labels")}  # fmt: skip
    assert revived == 1
    assert (rows[eroded]["superseded"], rows[eroded]["superseded_reason"]) == (
        False, "remediation")  # fmt: skip
    assert rows[other]["superseded"] is True


async def test_re_applying_writes_nothing(lab, app_engine):
    dive = await lab.dive()
    await lab.label(await lab.capture(dive), completed=True, superseded=True,
                    x=1.0, y=1.0, number=3_000_041)  # fmt: skip
    population = await _as_tenant(app_engine, lab.tenant, laser_label_population, dive)
    await _as_tenant(app_engine, lab.tenant, revive_laser_labels, dive, [3_000_041],
                     population.fingerprint)  # fmt: skip
    fresh = await _as_tenant(app_engine, lab.tenant, laser_label_population, dive)

    assert await _as_tenant(app_engine, lab.tenant, revive_laser_labels, dive, [3_000_041],
                            fresh.fingerprint) == 0  # fmt: skip


async def test_revive_refuses_if_the_dive_changed_since_it_was_planned(lab, app_engine):
    """v2: the plan is made on a snapshot read before the processor judged
    it; a label edited since (a sync, a validator run) could change the plan,
    so the write refuses and nothing is revived."""
    dive = await lab.dive()
    await lab.label(await lab.capture(dive), completed=True, superseded=True,
                    x=1.0, y=1.0, number=3_000_041)  # fmt: skip
    population = await _as_tenant(app_engine, lab.tenant, laser_label_population, dive)
    await lab.label(await lab.capture(dive), completed=True, x=5.0, y=5.0)

    with pytest.raises(PopulationChanged):
        await _as_tenant(app_engine, lab.tenant, revive_laser_labels, dive, [3_000_041],
                         population.fingerprint)  # fmt: skip
    assert len(await lab.rows("SELECT id FROM laser_labels WHERE superseded")) == 1


async def test_revive_refuses_a_label_of_another_dive(lab, app_engine):
    dive = await lab.dive()
    await lab.label(await lab.capture(await lab.dive()), completed=True,
                    superseded=True, x=1.0, y=1.0, number=3_000_099)  # fmt: skip
    population = await _as_tenant(app_engine, lab.tenant, laser_label_population, dive)

    with pytest.raises(ForeignRows):
        await _as_tenant(app_engine, lab.tenant, revive_laser_labels, dive, [3_000_099],
                         population.fingerprint)  # fmt: skip


# -- the landing page's project ids --------------------------------------------------


async def _gated_project(lab, project, verdicts):
    dive = await lab.dive()
    for verdict in verdicts:
        capture = await lab.capture(dive)
        await lab.label(capture, project=project)
        prediction = await lab.prediction(capture)
        if verdict is not None:
            await lab.verdict(prediction, verdict)


async def test_gated_true_requires_the_gate_to_be_finished(lab, app_engine):
    await _gated_project(lab, 1, ["off_line", "audit_sample"])
    await _gated_project(lab, 2, ["off_line", None])
    await _gated_project(lab, 3, [None])
    await lab.label(await lab.capture(await lab.dive()), project=4)  # no prediction

    ids = await _as_tenant(app_engine, lab.tenant, laser_label_studio_project_ids,
                           gated=True)  # fmt: skip
    rest = await _as_tenant(app_engine, lab.tenant, laser_label_studio_project_ids,
                            gated=False)  # fmt: skip
    every = await _as_tenant(app_engine, lab.tenant, laser_label_studio_project_ids)

    assert ids == [1]
    assert rest == [2, 3, 4]  # the exact complement
    assert every == [1, 2, 3, 4]


async def test_superseded_labels_do_not_hold_a_project_ungated(lab, app_engine):
    dive = await lab.dive()
    judged = await lab.capture(dive)
    await lab.label(judged, project=1)
    await lab.verdict(await lab.prediction(judged), "off_line")
    stale = await lab.capture(dive)
    await lab.label(stale, project=1, superseded=True)
    await lab.prediction(stale)

    assert await _as_tenant(app_engine, lab.tenant, laser_label_studio_project_ids,
                            gated=True) == [1]  # fmt: skip


async def test_a_superseded_only_project_is_excluded_and_incomplete_composes(
    lab, app_engine
):
    await lab.label(await lab.capture(await lab.dive()), project=1, superseded=True)
    await lab.label(await lab.capture(await lab.dive()), project=2, completed=True,
                    x=1.0, y=1.0)  # fmt: skip
    await lab.label(await lab.capture(await lab.dive()), project=3)

    assert await _as_tenant(app_engine, lab.tenant, laser_label_studio_project_ids) == [
        2,
        3,
    ]
    assert await _as_tenant(app_engine, lab.tenant, laser_label_studio_project_ids,
                            incomplete=True) == [3]  # fmt: skip


# -- the catalog, across tenants ---------------------------------------------------


async def test_the_catalog_serves_only_tenants_it_is_a_member_of(
    owner_engine, app_engine, seed_memberships
):
    tenants = await seed_memberships({ORCHESTRATOR: {"lab": "service"}})
    async with owner_engine.begin() as conn:
        other = (await conn.execute(text(
            "INSERT INTO tenants (slug, name) VALUES ('other', 'other') RETURNING id"
        ))).scalar_one()  # fmt: skip
    lab = Seed(owner_engine, tenants["lab"])
    await lab.capture(await lab.dive())
    await Seed(owner_engine, other).capture(await Seed(owner_engine, other).dive())
    catalog = LaserCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.member_tenants() == [tenants["lab"]]
    assert await catalog.next_dive_for_laser_preprocessing(tenants["lab"]) is not None
    with pytest.raises(PermissionError):
        await catalog.next_dive_for_laser_preprocessing(other)


async def test_rls_hides_another_tenants_rows(owner_engine, app_engine, lab):
    async with owner_engine.begin() as conn:
        other = (await conn.execute(text(
            "INSERT INTO tenants (slug, name) VALUES ('other', 'other') RETURNING id"
        ))).scalar_one()  # fmt: skip
    await Seed(owner_engine, other).capture(await Seed(owner_engine, other).dive())

    assert await _next(app_engine, lab, next_dive_for_laser_preprocessing) is None
