"""The database side of the head/tail stages, tenant-scoped, on real Postgres.

Ported from fishsense-lite@77e8f8e5 (names, fixtures' shapes and reasons are
v1's):

* services/fishsense-api/tests/test_select_next_dive_endpoints.py (the stage
  5.1 half), test_cohort_needs_reprocess_all_kinds.py,
  test_canonical_only_pipeline_work.py -- the preprocess cohort;
* services/fishsense-api-workflow-worker/tests/
  test_resolve_headtail_preprocess_inputs_activity.py,
  test_resolvers_honour_needs_reprocess.py and test_reprocess_flag_drains.py
  (the head/tail cases) -- its resolver;
* services/fishsense-api/tests/test_needs_reprocess_scoping.py and
  test_needs_reprocess_clear_scope.py (the head/tail kind) -- the flags;
* services/fishsense-api/tests/test_headtail_prediction_cohort.py and
  services/fishsense-api-workflow-worker/tests/
  test_resolve_headtail_predict_inputs.py -- the predict cohort and resolver;
* services/fishsense-api/tests/test_headtail_prediction_endpoint.py -- the
  prediction write;
* services/fishsense-api/tests/test_headtail_population_cohort.py -- the
  populate cohort.

v2 changes, each pinned here:

* per tenant, ordered by `created_at` (v1: `id`) so the orchestrator can take
  the oldest candidate across the tenants it serves; the predict cohort still
  puts never-predicted dives first;
* **predictions are appended, never updated** (migration 0011): what v1
  upserted on the image is an INSERT, and "the prediction" is the current one
  (`current_head_tail_predictions`);
* intrinsics are the dive's device's current camera calibration (v1: the
  dive's camera's intrinsics), and a non-pinhole calibration is refused rather
  than rectified with pinhole maths (PLAN.md §8); **the stage-5.1 and predict
  cohorts leave out a dive the resolver refuses**, which v1 selected (and
  stalled on) every hour;
* **the processor's output is checked** (PLAN.md §9.11): a prediction for a
  capture outside the dive, or naming another capture's laser label, is refused
  and nothing is written;
* v1's legacy NULL-`superseded` rows cannot exist (migration 0010's NOT NULL),
  so v1's NULL-row cases have no v2 counterpart;
* **populate never erases a labeler's work**: re-recording a task on an
  existing (capture, project) row sets only the task and revives it, where v1
  wrote the whole row back blank.
"""

import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.headtail_store import (
    ForeignCapture,
    ForeignLaserLabel,
    HeadTailPredictionRow,
    HeadtailCatalog,
    InvalidPredictions,
    UnsupportedCameraModel,
    clear_headtail_needs_reprocess,
    dives_needing_headtail_population,
    headtail_populate_state,
    headtail_predict_captures,
    headtail_preprocess_inputs,
    next_dive_for_headtail_prediction,
    next_dive_for_headtail_preprocessing,
    persist_headtail_predictions,
    record_head_tail_task,
    set_headtail_needs_reprocess,
    supersede_head_tail_labels,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)
V = 2  # HEADTAIL_PREDICTOR_VERSION, which the orchestrator passes in
FALLBACK = -1
ORCHESTRATOR = "service:fishsense-orchestrator"
_LASER_PROJECTS = itertools.count(1000)
K = [[3000.0, 0.0, 2048.0], [0.0, 3000.0, 1536.0], [0.0, 0.0, 1.0]]
D = [-0.05, 0.01, 0.0, 0.0, 0.0]


# -- seeding (as the owner, like an admin or migrate-v1 would) --------------------


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


async def _device(owner_engine, tenant, *, calibrations=((K, D, "pinhole"),)):
    device = await _exec(
        owner_engine,
        "INSERT INTO devices (tenant_id, kind, serial) VALUES (:t, 'lite', :s) "
        "RETURNING id",
        t=tenant,
        s=uuid.uuid4().hex,
    )
    for matrix, distortion, model in calibrations:
        await _exec(
            owner_engine,
            "INSERT INTO camera_calibrations (tenant_id, device_id, camera_model, "
            "port_model, camera_matrix, distortion_coefficients) VALUES (:t, :d, :m, "
            ":port, CAST(:k AS jsonb), CAST(:dist AS jsonb))",
            t=tenant,
            d=device,
            m=model,
            port=None if model == "pinhole" else "flat",
            k=json.dumps(matrix),
            dist=json.dumps(distortion),
        )
    return device


#: `_dive`'s default: a device with a current pinhole calibration, the only
#: dive stage 5.1 can render (and so the only one either cohort selects).
_PINHOLE_DEVICE = object()


async def _dive(
    owner_engine, tenant, *, priority="high", at=T0, device=_PINHOLE_DEVICE
):
    if device is _PINHOLE_DEVICE:
        device = await _device(owner_engine, tenant)
    return await _exec(
        owner_engine,
        "INSERT INTO dives (tenant_id, source_path, name, dived_at, priority, "
        "device_id, created_at) VALUES (:t, :p, 'd', :at, :prio, :dev, :at) "
        "RETURNING id",
        t=tenant,
        p=f"/d/{uuid.uuid4()}",
        at=at,
        prio=priority,
        dev=device,
    )


async def _capture(
    owner_engine, tenant, dive, *, checksum=None, canonical=True, v1_id=None, at=T0
):
    return await _exec(
        owner_engine,
        "INSERT INTO captures (tenant_id, dive_id, source_path, captured_at, "
        "checksum, is_canonical, v1_id) VALUES (:t, :d, :p, :at, :c, :canon, :v1) "
        "RETURNING id",
        t=tenant,
        d=dive,
        p=f"/c/{uuid.uuid4()}.ORF",
        at=at,
        c=checksum or uuid.uuid4().hex,
        canon=canonical,
        v1=v1_id,
    )


async def _checksum(owner_engine, capture) -> str:
    return await _exec(
        owner_engine, "SELECT checksum FROM captures WHERE id = :c", c=capture
    )


async def _laser(
    owner_engine, tenant, capture, *, completed=True, superseded=False, x=100.0,
    y=200.0,
):  # fmt: skip
    # One laser label per (capture, project): a second dot is another project's.
    return await _exec(
        owner_engine,
        "INSERT INTO laser_labels (tenant_id, capture_id, source, ls_project_id, "
        "completed, superseded, x, y) VALUES (:t, :c, 'human', :p, :done, :gone, "
        ":x, :y) RETURNING id",
        t=tenant,
        p=next(_LASER_PROJECTS),
        c=capture,
        done=completed,
        gone=superseded,
        x=x,
        y=y,
    )


async def _headtail(
    owner_engine, tenant, capture, *, project=71, task=None, completed=True,
    superseded=False, needs_reprocess=False, head=None,
):  # fmt: skip
    head_x, head_y = head or (None, None)
    return await _exec(
        owner_engine,
        "INSERT INTO head_tail_labels (tenant_id, capture_id, source, ls_project_id, "
        "ls_task_id, completed, superseded, needs_reprocess, head_x, head_y) "
        "VALUES (:t, :c, 'human', :p, :k, :done, :gone, :flag, :hx, :hy) "
        "RETURNING id",
        t=tenant,
        c=capture,
        p=project,
        k=task,
        done=completed,
        gone=superseded,
        flag=needs_reprocess,
        hx=head_x,
        hy=head_y,
    )


async def _prediction(
    owner_engine, tenant, capture, *, version=V, laser=None, status="predicted",
    v1_id=None,
):  # fmt: skip
    points = 1.0 if status == "predicted" else None
    return await _exec(
        owner_engine,
        "INSERT INTO head_tail_predictions (tenant_id, capture_id, predictor_version, "
        "laser_label_id, status, head_x, head_y, tail_x, tail_y, v1_id) VALUES "
        "(:t, :c, :v, :l, :s, :p, :p, :p, :p, :v1) RETURNING id",
        t=tenant,
        c=capture,
        v=version,
        l=laser,
        s=status,
        p=points,
        v1=v1_id,
    )


async def _label(owner_engine, label_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT * FROM head_tail_labels WHERE id = :i"), {"i": label_id}
            )
        ).one()


async def _in(app_engine, tenant, fn, *args, **kwargs):
    async with tenant_transaction(app_engine, tenant) as conn:
        return await fn(conn, tenant, *args, **kwargs)


async def _next_preprocess(app_engine, tenant):
    candidate = await _in(app_engine, tenant, next_dive_for_headtail_preprocessing)
    return None if candidate is None else candidate.dive_id


async def _next_predict(app_engine, tenant):
    candidate = await _in(
        app_engine, tenant, next_dive_for_headtail_prediction, predictor_version=V
    )
    return None if candidate is None else candidate.dive_id


async def _population(app_engine, tenant):
    return [
        c.dive_id
        for c in await _in(app_engine, tenant, dives_needing_headtail_population)
    ]


# -- stage 5.1: the preprocess cohort (v1's select-next endpoint tests) -------------


async def test_headtail_preprocessing_requires_valid_laser_without_any_headtail(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    # dive 1: valid laser + headtail row exists -> excluded.
    # dive 2: valid laser, no headtail row -> picked.
    # dive 3: laser row is null x/y (no-laser sentinel) -> excluded.
    d1, d2, d3 = [
        await _dive(owner_engine, lab, at=T0 + timedelta(minutes=i)) for i in range(3)
    ]
    c1, c2, c3 = [await _capture(owner_engine, lab, d) for d in (d1, d2, d3)]
    await _laser(owner_engine, lab, c1)
    await _laser(owner_engine, lab, c2)
    await _laser(owner_engine, lab, c3, x=None, y=None)
    await _headtail(owner_engine, lab, c1, completed=True)

    assert await _next_preprocess(app_engine, lab) == d2


async def test_headtail_preprocessing_excludes_dive_with_only_incomplete_headtail(
    owner_engine, app_engine
):
    """Once populate seeds an incomplete row (with a real project) for every
    laser-cascaded image, the dive drops out of the cohort."""
    lab = await _tenant(owner_engine)
    capture = await _capture(owner_engine, lab, await _dive(owner_engine, lab))
    await _laser(owner_engine, lab, capture)
    await _headtail(owner_engine, lab, capture, completed=False)

    assert await _next_preprocess(app_engine, lab) is None


async def test_headtail_preprocessing_excludes_dive_when_sentinel_coexists_with_real_label(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    capture = await _capture(owner_engine, lab, await _dive(owner_engine, lab))
    await _laser(owner_engine, lab, capture)
    await _headtail(owner_engine, lab, capture, project=None, completed=False)
    await _headtail(owner_engine, lab, capture, project=71, completed=False)

    assert await _next_preprocess(app_engine, lab) is None


async def test_headtail_preprocessing_ignores_null_project_sentinels(
    owner_engine, app_engine
):
    """NULL-project rows are sentinels: they must NOT drop a dive from the
    head/tail cohort."""
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)
    capture = await _capture(owner_engine, lab, dive)
    await _laser(owner_engine, lab, capture)
    await _headtail(owner_engine, lab, capture, project=None, completed=False)

    assert await _next_preprocess(app_engine, lab) == dive


async def test_a_superseded_headtail_row_does_not_count_as_done(
    owner_engine, app_engine
):
    """Dead letters don't count as done (v1's `superseded == False`)."""
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)
    capture = await _capture(owner_engine, lab, dive)
    await _laser(owner_engine, lab, capture)
    await _headtail(owner_engine, lab, capture, superseded=True)

    assert await _next_preprocess(app_engine, lab) == dive


@pytest.mark.parametrize(
    "laser",
    [{"completed": False}, {"superseded": True}, {"x": None}, {"y": None}],
    ids=["incomplete", "superseded", "no-x", "no-y"],
)
async def test_headtail_preprocessing_excludes_incomplete_or_superseded_or_null_xy_lasers(
    owner_engine, app_engine, laser
):
    lab = await _tenant(owner_engine)
    capture = await _capture(owner_engine, lab, await _dive(owner_engine, lab))
    await _laser(owner_engine, lab, capture, **laser)

    assert await _next_preprocess(app_engine, lab) is None


async def test_headtail_preprocessing_returns_none_when_no_laser_labels(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    await _capture(owner_engine, lab, await _dive(owner_engine, lab))

    assert await _next_preprocess(app_engine, lab) is None


async def test_headtail_preprocessing_only_high_priority(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    capture = await _capture(
        owner_engine, lab, await _dive(owner_engine, lab, priority="low")
    )
    await _laser(owner_engine, lab, capture)

    assert await _next_preprocess(app_engine, lab) is None


async def test_a_valid_laser_on_a_non_canonical_copy_does_not_select(
    owner_engine, app_engine
):
    """Only the canonical copy is ever preprocessed; a duplicate dive must look
    the way a dive with no images looks (test_canonical_only_pipeline_work)."""
    lab = await _tenant(owner_engine)
    capture = await _capture(
        owner_engine, lab, await _dive(owner_engine, lab), canonical=False
    )
    await _laser(owner_engine, lab, capture)

    assert await _next_preprocess(app_engine, lab) is None


async def test_the_oldest_candidate_comes_first(owner_engine, app_engine):
    """v1: lowest dive id first. v2: oldest `created_at` first, per tenant."""
    lab = await _tenant(owner_engine)
    newer = await _dive(owner_engine, lab, at=T0 + timedelta(hours=1))
    older = await _dive(owner_engine, lab, at=T0)
    for dive in (newer, older):
        await _laser(owner_engine, lab, await _capture(owner_engine, lab, dive))

    candidate = await _in(app_engine, lab, next_dive_for_headtail_preprocessing)

    assert (candidate.dive_id, candidate.created_at) == (older, T0)


async def test_the_cohort_is_per_tenant(owner_engine, app_engine):
    lab, reef = await _tenant(owner_engine, "lab"), await _tenant(owner_engine, "reef")
    await _laser(
        owner_engine,
        reef,
        await _capture(owner_engine, reef, await _dive(owner_engine, reef)),
    )

    assert await _next_preprocess(app_engine, lab) is None


async def _unrenderable_dive(owner_engine, tenant, setup, *, at=T0):
    """A dive stage 5.1's resolver refuses: no device, a device with no
    calibration, or one whose current calibration is not a pinhole."""
    if setup == "no-device":
        return await _dive(owner_engine, tenant, at=at, device=None)
    calibrations = () if setup == "no-calibration" else ((K, D, "axial_refractive"),)
    device = await _device(owner_engine, tenant, calibrations=calibrations)
    return await _dive(owner_engine, tenant, at=at, device=device)


_UNRENDERABLE = ["no-device", "no-calibration", "axial"]


@pytest.mark.parametrize("setup", _UNRENDERABLE)
async def test_a_dive_stage_5_1_cannot_render_is_not_selected_nor_blocks_a_younger_one(
    owner_engine, app_engine, setup
):
    """v2: the cohort carries the resolver's refusals. v1 selected a dive
    whose resolver then raised, every hour, and -- oldest first -- nothing
    behind it ever ran; across tenants it blocks every tenant."""
    lab = await _tenant(owner_engine)
    stuck = await _unrenderable_dive(owner_engine, lab, setup)
    stuck_capture = await _capture(owner_engine, lab, stuck)
    await _laser(owner_engine, lab, stuck_capture)
    await _headtail(owner_engine, lab, stuck_capture, needs_reprocess=True)
    younger = await _dive(owner_engine, lab, at=T0 + timedelta(hours=1))
    await _laser(owner_engine, lab, await _capture(owner_engine, lab, younger))

    assert await _next_preprocess(app_engine, lab) == younger


async def test_only_the_current_calibration_decides_renderability(
    owner_engine, app_engine
):
    """A pinhole calibration superseded by an axial one is axial now: the
    resolver reads the current one, and so must the cohort."""
    lab = await _tenant(owner_engine)
    device = await _device(
        owner_engine, lab, calibrations=((K, D, "pinhole"), (K, D, "axial_refractive"))
    )
    dive = await _dive(owner_engine, lab, device=device)
    await _laser(owner_engine, lab, await _capture(owner_engine, lab, dive))

    assert await _next_preprocess(app_engine, lab) is None


# -- the flag branch (v1's test_cohort_needs_reprocess_all_kinds) ------------------


async def _labelled_dive(owner_engine, tenant, *, flagged, canonical=True):
    dive = await _dive(owner_engine, tenant)
    capture = await _capture(owner_engine, tenant, dive, canonical=canonical)
    await _laser(owner_engine, tenant, capture)
    await _headtail(owner_engine, tenant, capture, needs_reprocess=flagged)
    return dive, capture


async def test_fully_labelled_dive_is_not_selected_without_a_flag(
    owner_engine, app_engine
):
    """The control. If this ever fails the test below proves nothing."""
    lab = await _tenant(owner_engine)
    await _labelled_dive(owner_engine, lab, flagged=False)

    assert await _next_preprocess(app_engine, lab) is None


async def test_flagged_dive_is_selected(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, _ = await _labelled_dive(owner_engine, lab, flagged=True)

    assert await _next_preprocess(app_engine, lab) == dive


async def test_flag_on_a_non_canonical_image_does_not_select(owner_engine, app_engine):
    """A flag on a duplicate would select a dive the resolver finds no work
    for, and it would re-stage its raw bytes from the NAS every hour forever."""
    lab = await _tenant(owner_engine)
    await _labelled_dive(owner_engine, lab, flagged=True, canonical=False)

    assert await _next_preprocess(app_engine, lab) is None


async def test_flag_on_a_superseded_row_does_not_select(owner_engine, app_engine):
    """The resolver never sees a superseded row, so neither may the cohort."""
    lab = await _tenant(owner_engine)
    dive, capture = await _labelled_dive(owner_engine, lab, flagged=False)
    await _headtail(
        owner_engine, lab, capture, project=2, completed=False, superseded=True,
        needs_reprocess=True,
    )  # fmt: skip

    assert await _next_preprocess(app_engine, lab) is None


# -- the preprocess resolver (v1's resolver tests) ---------------------------------


async def _inputs(app_engine, tenant, dive):
    return await _in(app_engine, tenant, headtail_preprocess_inputs, dive)


async def _resolver_dive(owner_engine, tenant, **device):
    device_id = await _device(owner_engine, tenant, **device)
    return await _dive(owner_engine, tenant, device=device_id)


async def test_returns_only_valid_laser_without_any_real_headtail(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _resolver_dive(owner_engine, lab)
    c1, c2, c3, c4 = [
        await _capture(owner_engine, lab, dive, checksum=ch * 32)
        for ch in ("a", "b", "c", "d")
    ]
    await _laser(owner_engine, lab, c1)  # valid + completed headtail -> dropped
    await _laser(owner_engine, lab, c2, completed=False)  # incomplete laser
    await _laser(owner_engine, lab, c3)  # valid + no headtail -> kept
    await _laser(owner_engine, lab, c4)  # valid + incomplete real headtail
    await _headtail(owner_engine, lab, c1, completed=True)
    await _headtail(owner_engine, lab, c4, completed=False)

    inputs = await _inputs(app_engine, lab, dive)

    assert [c.checksum for c in inputs.captures] == ["c" * 32]
    assert inputs.captures[0].capture_id == c3
    assert inputs.camera_matrix == K
    assert inputs.distortion_coefficients == D


async def test_image_with_only_null_project_sentinel_treated_as_unlabeled(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _resolver_dive(owner_engine, lab)
    c1 = await _capture(owner_engine, lab, dive, checksum="a" * 32)
    c2 = await _capture(owner_engine, lab, dive, checksum="b" * 32)
    for capture in (c1, c2):
        await _laser(owner_engine, lab, capture)
    await _headtail(owner_engine, lab, c1, completed=False, project=None)
    await _headtail(owner_engine, lab, c2, completed=False, project=71)

    inputs = await _inputs(app_engine, lab, dive)

    assert [c.checksum for c in inputs.captures] == ["a" * 32]


async def test_drops_lasers_that_are_incomplete_superseded_or_null_xy(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _resolver_dive(owner_engine, lab)
    for laser in (
        {"completed": False}, {"superseded": True}, {"x": None}, {"y": None},
    ):  # fmt: skip
        await _laser(
            owner_engine, lab, await _capture(owner_engine, lab, dive), **laser
        )

    assert (await _inputs(app_engine, lab, dive)).captures == []


async def test_empty_when_no_laser_labels(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _resolver_dive(owner_engine, lab)
    await _capture(owner_engine, lab, dive)

    assert (await _inputs(app_engine, lab, dive)).captures == []


async def test_canonical_frames_only(owner_engine, app_engine):
    """Mirrors the cohort: the dispatched work must match what it promised."""
    lab = await _tenant(owner_engine)
    dive = await _resolver_dive(owner_engine, lab)
    await _laser(
        owner_engine, lab, await _capture(owner_engine, lab, dive, canonical=False)
    )

    assert (await _inputs(app_engine, lab, dive)).captures == []


async def test_two_valid_lasers_on_one_frame_render_it_once(owner_engine, app_engine):
    """v1 listed a frame once per valid laser (461 prod images carry two), so
    its JPEG was rendered twice by concurrent activities. Once is enough."""
    lab = await _tenant(owner_engine)
    dive = await _resolver_dive(owner_engine, lab)
    capture = await _capture(owner_engine, lab, dive)
    await _laser(owner_engine, lab, capture)
    await _laser(owner_engine, lab, capture, x=101.0)

    assert [c.capture_id for c in (await _inputs(app_engine, lab, dive)).captures] == [
        capture
    ]


async def test_a_capture_says_whether_it_came_from_v1(owner_engine, app_engine):
    """The orchestrator writes a migrated frame's JPEG over v1's, where its
    Label Studio tasks already point."""
    lab = await _tenant(owner_engine)
    dive = await _resolver_dive(owner_engine, lab)
    migrated = await _capture(owner_engine, lab, dive, v1_id=77)
    await _laser(owner_engine, lab, migrated)

    (capture,) = (await _inputs(app_engine, lab, dive)).captures
    assert (capture.capture_id, capture.from_v1) == (migrated, True)


class TestFlaggedImages:
    """v1's test_resolvers_honour_needs_reprocess.py / test_reprocess_flag_drains.py."""

    async def test_flagged_image_is_returned_though_it_is_already_labelled(
        self, owner_engine, app_engine
    ):
        lab = await _tenant(owner_engine)
        dive = await _resolver_dive(owner_engine, lab)
        capture = await _capture(owner_engine, lab, dive)
        await _laser(owner_engine, lab, capture)
        await _headtail(owner_engine, lab, capture, needs_reprocess=True)

        captures = (await _inputs(app_engine, lab, dive)).captures
        assert [c.capture_id for c in captures] == [capture]

    async def test_unflagged_labelled_image_is_still_excluded(
        self, owner_engine, app_engine
    ):
        lab = await _tenant(owner_engine)
        dive = await _resolver_dive(owner_engine, lab)
        capture = await _capture(owner_engine, lab, dive)
        await _laser(owner_engine, lab, capture)
        await _headtail(owner_engine, lab, capture, needs_reprocess=False)

        assert (await _inputs(app_engine, lab, dive)).captures == []

    async def test_flagged_image_resolves_though_its_laser_was_superseded(
        self, owner_engine, app_engine
    ):
        """The cohort's flag branch has no laser gate, so the resolver's must
        not either -- or a laser superseded after flagging wedges the dive."""
        lab = await _tenant(owner_engine)
        dive = await _resolver_dive(owner_engine, lab)
        capture = await _capture(owner_engine, lab, dive)
        await _laser(owner_engine, lab, capture, superseded=True)
        await _headtail(owner_engine, lab, capture, needs_reprocess=True)

        captures = (await _inputs(app_engine, lab, dive)).captures
        assert [c.capture_id for c in captures] == [capture]

    async def test_eligible_frames_come_before_flagged_ones(
        self, owner_engine, app_engine
    ):
        """v1's order: the laser-cascaded frames, then the flagged ones."""
        lab = await _tenant(owner_engine)
        dive = await _resolver_dive(owner_engine, lab)
        flagged = await _capture(owner_engine, lab, dive)
        await _headtail(owner_engine, lab, flagged, needs_reprocess=True)
        fresh = await _capture(owner_engine, lab, dive)
        await _laser(owner_engine, lab, fresh)

        captures = (await _inputs(app_engine, lab, dive)).captures
        assert [c.capture_id for c in captures] == [fresh, flagged]

    async def test_a_frame_both_eligible_and_flagged_is_rendered_once(
        self, owner_engine, app_engine
    ):
        """A flagged sentinel (no project) on a laser-valid frame is reached
        by both branches; v1 de-duplicated them (`seen`)."""
        lab = await _tenant(owner_engine)
        dive = await _resolver_dive(owner_engine, lab)
        capture = await _capture(owner_engine, lab, dive)
        await _laser(owner_engine, lab, capture)
        await _headtail(
            owner_engine, lab, capture, project=None, completed=False,
            needs_reprocess=True,
        )  # fmt: skip

        captures = (await _inputs(app_engine, lab, dive)).captures
        assert [c.capture_id for c in captures] == [capture]


async def test_eligible_frames_come_in_laser_label_order(owner_engine, app_engine):
    """v1 walked the dive's laser labels, so frames came in label order."""
    lab = await _tenant(owner_engine)
    dive = await _resolver_dive(owner_engine, lab)
    first_captured = await _capture(owner_engine, lab, dive)
    second_captured = await _capture(owner_engine, lab, dive)
    await _laser(owner_engine, lab, second_captured)
    await _laser(owner_engine, lab, first_captured)

    captures = (await _inputs(app_engine, lab, dive)).captures
    assert [c.capture_id for c in captures] == [second_captured, first_captured]


@pytest.mark.parametrize(
    ("setup", "match"),
    [("no-dive", "not found"), ("no-device", "no device"),
     ("no-calibration", "no camera calibration")],
)  # fmt: skip
async def test_raises_when_dive_or_device_or_calibration_missing(
    owner_engine, app_engine, setup, match
):
    lab = await _tenant(owner_engine)
    if setup == "no-dive":
        dive = uuid.uuid4()
    elif setup == "no-device":
        dive = await _dive(owner_engine, lab, device=None)
    else:
        dive = await _resolver_dive(owner_engine, lab, calibrations=())

    with pytest.raises(ValueError, match=match):
        await _inputs(app_engine, lab, dive)


async def test_the_current_calibration_is_the_latest(owner_engine, app_engine):
    """Calibrations are append-only; a correction is a new row."""
    newer = [[1.0, 0.0, 1.0], [0.0, 1.0, 1.0], [0.0, 0.0, 1.0]]
    lab = await _tenant(owner_engine)
    dive = await _resolver_dive(
        owner_engine, lab, calibrations=((K, D, "pinhole"), (newer, D, "pinhole"))
    )

    assert (await _inputs(app_engine, lab, dive)).camera_matrix == newer


async def test_a_one_row_distortion_vector_is_flattened(owner_engine, app_engine):
    """`cv2.calibrateCamera` returns (1, 5); the contract takes a flat list."""
    lab = await _tenant(owner_engine)
    dive = await _resolver_dive(owner_engine, lab, calibrations=((K, [D], "pinhole"),))

    assert (await _inputs(app_engine, lab, dive)).distortion_coefficients == D


async def test_a_non_pinhole_calibration_is_refused(owner_engine, app_engine):
    """v2: stage 5.1 rectifies with pinhole maths. An axial (flat-port)
    camera must not be silently undistorted as if it were one (PLAN.md §8)."""
    lab = await _tenant(owner_engine)
    dive = await _resolver_dive(
        owner_engine, lab, calibrations=((K, D, "axial_refractive"),)
    )

    with pytest.raises(UnsupportedCameraModel, match="axial_refractive"):
        await _inputs(app_engine, lab, dive)


# -- the flags (v1's _set_needs_reprocess tests, head/tail kind) --------------------


async def _flags(owner_engine) -> dict[uuid.UUID, bool]:
    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT id, needs_reprocess FROM head_tail_labels")
        )
        return {r.id: r.needs_reprocess for r in rows}


async def _scoping_seed(owner_engine, tenant):
    dive = await _dive(owner_engine, tenant)
    rows = {}
    for key, completed, canonical in (
        ("open", False, True),  # open, canonical      -> flagged
        ("done", True, True),  # completed, canonical -> NOT flagged by default
        ("dup", False, False),  # open, non-canonical  -> never flagged
    ):
        capture = await _capture(owner_engine, tenant, dive, canonical=canonical)
        rows[key] = await _headtail(owner_engine, tenant, capture, completed=completed)
    return dive, rows


class TestScoping:
    async def test_default_flags_only_incomplete_canonical_labels(
        self, owner_engine, app_engine
    ):
        lab = await _tenant(owner_engine)
        dive, rows = await _scoping_seed(owner_engine, lab)

        assert await _in(app_engine, lab, set_headtail_needs_reprocess, dive) == 1
        flags = await _flags(owner_engine)
        assert flags[rows["open"]] is True
        assert flags[rows["done"]] is False, "already answered -- nothing to redraw"
        assert flags[rows["dup"]] is False, "non-canonical is never preprocessed"

    async def test_only_incomplete_false_flags_completed_too(
        self, owner_engine, app_engine
    ):
        lab = await _tenant(owner_engine)
        dive, rows = await _scoping_seed(owner_engine, lab)

        n = await _in(
            app_engine, lab, set_headtail_needs_reprocess, dive, only_incomplete=False
        )

        assert n == 2
        flags = await _flags(owner_engine)
        assert (flags[rows["open"]], flags[rows["done"]], flags[rows["dup"]]) == (
            True,
            True,
            False,
        )

    async def test_clearing_lowers_every_canonical_flag_regardless_of_completion(
        self, owner_engine, app_engine
    ):
        lab = await _tenant(owner_engine)
        dive, rows = await _scoping_seed(owner_engine, lab)
        await _in(
            app_engine, lab, set_headtail_needs_reprocess, dive, only_incomplete=False
        )

        assert await _in(app_engine, lab, clear_headtail_needs_reprocess, dive) == 2
        assert not any((await _flags(owner_engine)).values())

    async def test_is_idempotent_in_both_directions(self, owner_engine, app_engine):
        lab = await _tenant(owner_engine)
        dive, _ = await _scoping_seed(owner_engine, lab)

        assert await _in(app_engine, lab, set_headtail_needs_reprocess, dive) == 1
        assert await _in(app_engine, lab, set_headtail_needs_reprocess, dive) == 1
        assert await _in(app_engine, lab, clear_headtail_needs_reprocess, dive) == 2
        assert await _in(app_engine, lab, clear_headtail_needs_reprocess, dive) == 2

    async def test_dive_with_no_labels_returns_zero(self, owner_engine, app_engine):
        """The parent clears unconditionally; an error would fail the workflow."""
        lab = await _tenant(owner_engine)

        assert (
            await _in(app_engine, lab, clear_headtail_needs_reprocess, uuid.uuid4())
            == 0
        )

    async def test_superseded_incomplete_row_is_not_flagged(
        self, owner_engine, app_engine
    ):
        """A flag on a superseded row is visible to the cohort and invisible
        to the resolver -- the mismatch that re-stages a dive every hour."""
        lab = await _tenant(owner_engine)
        dive = await _dive(owner_engine, lab)
        row = await _headtail(
            owner_engine, lab, await _capture(owner_engine, lab, dive),
            completed=False, superseded=True,
        )  # fmt: skip

        assert await _in(app_engine, lab, set_headtail_needs_reprocess, dive) == 0
        assert (await _flags(owner_engine))[row] is False

    async def test_clearing_reaches_a_row_superseded_after_it_was_flagged(
        self, owner_engine, app_engine
    ):
        lab = await _tenant(owner_engine)
        dive = await _dive(owner_engine, lab)
        row = await _headtail(
            owner_engine, lab, await _capture(owner_engine, lab, dive),
            superseded=True, needs_reprocess=True,
        )  # fmt: skip

        assert await _in(app_engine, lab, clear_headtail_needs_reprocess, dive) == 1
        assert (await _flags(owner_engine))[row] is False


class TestClearScope:
    async def _two_flagged(self, owner_engine, tenant):
        dive = await _dive(owner_engine, tenant)
        rows = {}
        for key in ("redrawn", "raised_mid_run"):
            capture = await _capture(owner_engine, tenant, dive, checksum=None)
            rows[key] = (
                await _checksum(owner_engine, capture),
                await _headtail(
                    owner_engine, tenant, capture, completed=False,
                    needs_reprocess=True,
                ),
            )  # fmt: skip
        return dive, rows

    async def test_scoped_clear_leaves_a_flag_raised_during_the_run(
        self, owner_engine, app_engine
    ):
        lab = await _tenant(owner_engine)
        dive, rows = await self._two_flagged(owner_engine, lab)
        redrawn, _ = rows["redrawn"]

        n = await _in(app_engine, lab, clear_headtail_needs_reprocess, dive, [redrawn])

        assert n == 1
        flags = await _flags(owner_engine)
        assert flags[rows["raised_mid_run"][1]] is True
        assert flags[rows["redrawn"][1]] is False

    async def test_unscoped_clear_still_lowers_everything(
        self, owner_engine, app_engine
    ):
        lab = await _tenant(owner_engine)
        dive, _ = await self._two_flagged(owner_engine, lab)

        assert await _in(app_engine, lab, clear_headtail_needs_reprocess, dive) == 2

    async def test_an_empty_scope_is_not_read_as_no_scope(
        self, owner_engine, app_engine
    ):
        """`[]` means "this run redrew nothing", which must clear nothing."""
        lab = await _tenant(owner_engine)
        dive, _ = await self._two_flagged(owner_engine, lab)

        assert await _in(app_engine, lab, clear_headtail_needs_reprocess, dive, []) == 0
        assert all((await _flags(owner_engine)).values())


# -- the predict cohort (v1's test_headtail_prediction_cohort.py) --------------------


async def _predict_seed(owner_engine, tenant, *, at=T0, **laser):
    dive = await _dive(owner_engine, tenant, at=at)
    capture = await _capture(owner_engine, tenant, dive)
    laser_id = await _laser(owner_engine, tenant, capture, **laser)
    return dive, capture, laser_id


async def test_selects_a_dive_with_a_valid_laser_and_no_prediction(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive, _, _ = await _predict_seed(owner_engine, lab)
    assert await _next_predict(app_engine, lab) == dive


async def test_ignores_a_dive_with_no_valid_laser(owner_engine, app_engine):
    """The whole stage is gated on the dot: no laser, nothing to crop around."""
    lab = await _tenant(owner_engine)
    await _predict_seed(owner_engine, lab, completed=False)
    assert await _next_predict(app_engine, lab) is None


async def test_ignores_a_superseded_laser(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    await _predict_seed(owner_engine, lab, superseded=True)
    assert await _next_predict(app_engine, lab) is None


async def test_drops_out_once_predicted(owner_engine, app_engine):
    """The cohort must go false, or the dive is re-selected every hour."""
    lab = await _tenant(owner_engine)
    _, capture, laser = await _predict_seed(owner_engine, lab)
    await _prediction(owner_engine, lab, capture, laser=laser)
    assert await _next_predict(app_engine, lab) is None


async def test_never_predicts_over_a_completed_human_label(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    _, capture, _ = await _predict_seed(owner_engine, lab)
    await _headtail(owner_engine, lab, capture, completed=True)
    assert await _next_predict(app_engine, lab) is None


async def test_an_incomplete_populate_seeded_row_is_not_a_label(
    owner_engine, app_engine
):
    """Populate seeds rows that carry a project; keyed on project id, the
    cohort would starve the detector on exactly the dives it should assist."""
    lab = await _tenant(owner_engine)
    dive, capture, _ = await _predict_seed(owner_engine, lab)
    await _headtail(owner_engine, lab, capture, completed=False)
    assert await _next_predict(app_engine, lab) == dive


async def test_a_superseded_human_label_re_enters_the_cohort(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, capture, _ = await _predict_seed(owner_engine, lab)
    await _headtail(owner_engine, lab, capture, superseded=True)
    assert await _next_predict(app_engine, lab) == dive


@pytest.mark.parametrize("version", [0, None], ids=["stale", "null"])
async def test_stale_or_null_predictor_version_re_enters_the_cohort(
    owner_engine, app_engine, version
):
    """Mismatch, not absence. `IS DISTINCT FROM`: a NULL (migrated) row is
    stale, and `!=` would answer NULL and select nothing."""
    lab = await _tenant(owner_engine)
    dive, capture, _ = await _predict_seed(owner_engine, lab)
    await _prediction(owner_engine, lab, capture, version=version, v1_id=1)
    assert await _next_predict(app_engine, lab) == dive


@pytest.mark.parametrize(
    "status", ["predicted", "no_detections", "laser_off_all_fish", "headtail_failed"]
)
async def test_prediction_from_a_superseded_laser_is_stale(
    owner_engine, app_engine, status
):
    """The dot that chose the fish was later dead-lettered, so the mask may be
    of the wrong thing entirely. v2: an abstention names its dot too (the
    processor's), so a corrected dot re-opens a "no fish" as well."""
    lab = await _tenant(owner_engine)
    dive, capture, _ = await _predict_seed(owner_engine, lab)
    dead = await _laser(owner_engine, lab, capture, superseded=True)
    await _prediction(owner_engine, lab, capture, laser=dead, status=status)
    assert await _next_predict(app_engine, lab) == dive


async def test_only_the_current_prediction_counts(owner_engine, app_engine):
    """v2: predictions are appended; the latest is the one that is judged. A
    stale row followed by a current one is current."""
    lab = await _tenant(owner_engine)
    _, capture, laser = await _predict_seed(owner_engine, lab)
    await _prediction(owner_engine, lab, capture, version=FALLBACK, laser=laser)
    await _prediction(owner_engine, lab, capture, version=V, laser=laser)
    assert await _next_predict(app_engine, lab) is None


async def test_predict_only_high_priority(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab, priority="low")
    await _laser(owner_engine, lab, await _capture(owner_engine, lab, dive))
    assert await _next_predict(app_engine, lab) is None


async def test_predict_non_canonical_images_are_ignored(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)
    await _laser(
        owner_engine, lab, await _capture(owner_engine, lab, dive, canonical=False)
    )
    assert await _next_predict(app_engine, lab) is None


async def test_returns_the_oldest_dive_first(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    await _predict_seed(owner_engine, lab, at=T0 + timedelta(hours=1))
    older, _, _ = await _predict_seed(owner_engine, lab, at=T0)
    assert await _next_predict(app_engine, lab) == older


async def test_first_prediction_is_preferred_over_an_upgrade(owner_engine, app_engine):
    """A dive needing a *first* prediction outranks one needing only an
    upgrade, even when it is newer: a fallback-only dive is permanently stale
    while no GPU exists, and would otherwise starve every dive behind it."""
    lab = await _tenant(owner_engine)
    _, capture, _ = await _predict_seed(owner_engine, lab, at=T0)
    await _prediction(owner_engine, lab, capture, version=FALLBACK)
    fresh, _, _ = await _predict_seed(owner_engine, lab, at=T0 + timedelta(hours=1))

    candidate = await _in(
        app_engine, lab, next_dive_for_headtail_prediction, predictor_version=V
    )

    assert (candidate.dive_id, candidate.never_predicted) == (fresh, True)


@pytest.mark.parametrize("setup", _UNRENDERABLE)
async def test_predict_skips_a_dive_stage_5_1_cannot_render(
    owner_engine, app_engine, setup
):
    """Predict reads stage 5.1's JPEG; a dive 5.1 refuses never gets one, so
    its resolver would defer every image every hour -- and, never predicted,
    it would outrank every dive behind it forever."""
    lab = await _tenant(owner_engine)
    stuck = await _unrenderable_dive(owner_engine, lab, setup)
    await _laser(owner_engine, lab, await _capture(owner_engine, lab, stuck))
    younger, _, _ = await _predict_seed(owner_engine, lab, at=T0 + timedelta(hours=1))

    assert await _next_predict(app_engine, lab) == younger


async def test_an_upgrade_only_dive_is_still_selected_when_nothing_else_needs_one(
    owner_engine, app_engine
):
    """Preference, not exclusion: the upgrade queue must still drain."""
    lab = await _tenant(owner_engine)
    dive, capture, _ = await _predict_seed(owner_engine, lab)
    await _prediction(owner_engine, lab, capture, version=FALLBACK)

    candidate = await _in(
        app_engine, lab, next_dive_for_headtail_prediction, predictor_version=V
    )

    assert (candidate.dive_id, candidate.never_predicted) == (dive, False)


# -- the predict resolver (v1's test_resolve_headtail_predict_inputs.py) -------------


async def _needing(app_engine, tenant, dive):
    return await _in(
        app_engine, tenant, headtail_predict_captures, dive, predictor_version=V
    )


async def test_selects_an_unpredicted_image_with_a_valid_laser(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive, capture, laser = await _predict_seed(owner_engine, lab)

    (picked,) = await _needing(app_engine, lab, dive)

    assert picked.capture_id == capture
    assert [(d.laser_label_id, d.x, d.y) for d in picked.dots] == [
        (laser, 100.0, 200.0)
    ]
    assert (picked.has_existing_prediction, picked.existing_laser_superseded) == (
        False,
        False,
    )


@pytest.mark.parametrize(
    "laser",
    [{"x": None}, {"completed": False}, {"superseded": True}],
    ids=["no-coordinates", "incomplete", "superseded"],
)
async def test_skips_an_image_with_no_usable_laser(owner_engine, app_engine, laser):
    """The dot is the crop centre; without one there is nothing to predict on."""
    lab = await _tenant(owner_engine)
    dive, _, _ = await _predict_seed(owner_engine, lab, **laser)
    assert await _needing(app_engine, lab, dive) == []


async def test_skips_an_image_a_human_already_labelled(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, capture, _ = await _predict_seed(owner_engine, lab)
    await _headtail(owner_engine, lab, capture, completed=True)
    assert await _needing(app_engine, lab, dive) == []


async def test_an_incomplete_headtail_row_is_not_a_label(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, capture, _ = await _predict_seed(owner_engine, lab)
    await _headtail(owner_engine, lab, capture, completed=False)
    assert [p.capture_id for p in await _needing(app_engine, lab, dive)] == [capture]


async def test_skips_an_image_with_a_current_prediction(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, capture, laser = await _predict_seed(owner_engine, lab)
    await _prediction(owner_engine, lab, capture, laser=laser)
    assert await _needing(app_engine, lab, dive) == []


@pytest.mark.parametrize("version", [0, None, FALLBACK])
async def test_reselects_a_stale_or_null_version(owner_engine, app_engine, version):
    lab = await _tenant(owner_engine)
    dive, capture, laser = await _predict_seed(owner_engine, lab)
    await _prediction(owner_engine, lab, capture, version=version, laser=laser, v1_id=9)

    (picked,) = await _needing(app_engine, lab, dive)

    assert picked.capture_id == capture
    assert (picked.has_existing_prediction, picked.existing_laser_superseded) == (
        True,
        False,
    )


async def test_reselects_when_the_predictions_laser_was_superseded(
    owner_engine, app_engine
):
    """A prediction naming a label no longer in the live set was made from a
    dot since dead-lettered; the surviving dot is the one to use."""
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)
    capture = await _capture(owner_engine, lab, dive)
    dead = await _laser(owner_engine, lab, capture, superseded=True)
    live = await _laser(owner_engine, lab, capture)
    await _prediction(owner_engine, lab, capture, laser=dead)

    (picked,) = await _needing(app_engine, lab, dive)

    assert [d.laser_label_id for d in picked.dots] == [live], "the surviving dot"
    assert (picked.has_existing_prediction, picked.existing_laser_superseded) == (
        True,
        True,
    )


async def test_skips_non_canonical_images(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)
    await _laser(
        owner_engine, lab, await _capture(owner_engine, lab, dive, canonical=False)
    )
    assert await _needing(app_engine, lab, dive) == []


async def test_carries_every_valid_dot_in_order(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)
    capture = await _capture(owner_engine, lab, dive)
    first = await _laser(owner_engine, lab, capture, x=10.0, y=20.0)
    second = await _laser(owner_engine, lab, capture, x=11.0, y=21.0)

    (picked,) = await _needing(app_engine, lab, dive)

    assert [(d.x, d.y) for d in picked.dots] == [(10.0, 20.0), (11.0, 21.0)]
    assert [d.laser_label_id for d in picked.dots] == [first, second]


# -- persisting predictions (v1's prediction endpoint, now append-only) -------------


def _row(capture, **overrides) -> HeadTailPredictionRow:
    values = {
        "capture_id": capture,
        "status": "predicted",
        "head_x": 1.0, "head_y": 2.0, "tail_x": 3.0, "tail_y": 4.0,
        "width": 4014, "height": 3016, "mask_area_px": 900,
        "silhouette_ratio": 0.25, "crop_x": 1100, "crop_y": 825,
        "laser_label_id": None, "predictor_version": V,
        "checkpoint": "sam3/3.1/sam3.1_multiplex.pt", "core_version": "4.1.0",
    }  # fmt: skip
    values.update(overrides)
    return HeadTailPredictionRow(**values)


async def _current(owner_engine, capture):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT * FROM current_head_tail_predictions "
                    "WHERE capture_id = :c"
                ),
                {"c": capture},
            )
        ).one()


async def test_persist_appends_and_the_latest_is_current(owner_engine, app_engine):
    """v1 upserted on the image; v2 appends, so a re-run is history, and
    `current_head_tail_predictions` is what every reader sees."""
    lab = await _tenant(owner_engine)
    dive, capture, laser = await _predict_seed(owner_engine, lab)

    for x in (1.0, 5.0):
        written = await _in(
            app_engine, lab, persist_headtail_predictions, dive,
            [_row(capture, head_x=x, laser_label_id=laser)],
        )  # fmt: skip
        assert written == 1

    current = await _current(owner_engine, capture)
    assert (current.head_x, current.laser_label_id, current.width) == (5.0, laser, 4014)
    assert current.checkpoint == "sam3/3.1/sam3.1_multiplex.pt"
    async with owner_engine.connect() as conn:
        count = await conn.execute(
            text("SELECT count(*) FROM head_tail_predictions WHERE capture_id = :c"),
            {"c": capture},
        )
        assert count.scalar_one() == 2


@pytest.mark.parametrize(
    "status",
    ["no_detections", "laser_off_all_fish", "headtail_failed", "decode_failed"],
)
async def test_an_abstention_is_recorded_with_its_reason(
    owner_engine, app_engine, status
):
    """An abstention is a row, not a missing row: the cohort selects on
    absence, so an unrecorded one would be re-predicted every hour."""
    lab = await _tenant(owner_engine)
    dive, capture, _ = await _predict_seed(owner_engine, lab)

    await _in(
        app_engine, lab, persist_headtail_predictions, dive,
        [_row(capture, status=status, head_x=None, head_y=None, tail_x=None,
              tail_y=None)],
    )  # fmt: skip

    assert (await _current(owner_engine, capture)).status == status
    assert await _next_predict(app_engine, lab) is None


async def test_a_prediction_for_a_capture_outside_the_dive_is_refused(
    owner_engine, app_engine
):
    """v2 (PLAN.md §9.11): the processor's output is checked, all or nothing."""
    lab = await _tenant(owner_engine)
    dive, capture, _ = await _predict_seed(owner_engine, lab)
    _, elsewhere, _ = await _predict_seed(owner_engine, lab)

    with pytest.raises(ForeignCapture):
        await _in(
            app_engine, lab, persist_headtail_predictions, dive,
            [_row(capture), _row(elsewhere)],
        )  # fmt: skip

    async with owner_engine.connect() as conn:
        count = await conn.execute(text("SELECT count(*) FROM head_tail_predictions"))
        assert count.scalar_one() == 0


async def test_a_prediction_naming_another_captures_laser_is_refused(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)
    capture = await _capture(owner_engine, lab, dive)
    other = await _capture(owner_engine, lab, dive)
    others_laser = await _laser(owner_engine, lab, other)

    with pytest.raises(ForeignLaserLabel):
        await _in(
            app_engine, lab, persist_headtail_predictions, dive,
            [_row(capture, laser_label_id=others_laser)],
        )  # fmt: skip
    assert issubclass(ForeignLaserLabel, InvalidPredictions)


# -- the populate cohort (v1's test_headtail_population_cohort.py) -----------------


async def test_population_selects_a_predicted_unlabelled_image(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive, capture, _ = await _predict_seed(owner_engine, lab)
    await _prediction(owner_engine, lab, capture)
    assert await _population(app_engine, lab) == [dive]


async def test_unpredicted_images_are_not_populated(owner_engine, app_engine):
    """The gate. Without it, populate would seed a row and remove the image
    from the predict cohort permanently."""
    lab = await _tenant(owner_engine)
    await _predict_seed(owner_engine, lab)
    assert await _population(app_engine, lab) == []


async def test_an_abstention_still_opens_the_gate(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    dive, capture, _ = await _predict_seed(owner_engine, lab)
    await _prediction(owner_engine, lab, capture, status="no_detections")
    assert await _population(app_engine, lab) == [dive]


async def test_drops_out_once_labelling_is_complete(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    _, capture, _ = await _predict_seed(owner_engine, lab)
    await _prediction(owner_engine, lab, capture)
    await _headtail(owner_engine, lab, capture, completed=True)
    assert await _population(app_engine, lab) == []


async def test_stays_in_the_cohort_while_labelling_is_incomplete(
    owner_engine, app_engine
):
    """ "No completed label", not "no row" -- so the idempotent populate
    self-heals hourly until labelers finish."""
    lab = await _tenant(owner_engine)
    dive, capture, _ = await _predict_seed(owner_engine, lab)
    await _prediction(owner_engine, lab, capture)
    await _headtail(owner_engine, lab, capture, completed=False)
    assert await _population(app_engine, lab) == [dive]


async def test_population_requires_a_valid_laser(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    _, capture, _ = await _predict_seed(owner_engine, lab, superseded=True)
    await _prediction(owner_engine, lab, capture)
    assert await _population(app_engine, lab) == []


async def test_population_only_high_priority_and_canonical(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    low = await _dive(owner_engine, lab, priority="low")
    c1 = await _capture(owner_engine, lab, low)
    high = await _dive(owner_engine, lab)
    c2 = await _capture(owner_engine, lab, high, canonical=False)
    for capture in (c1, c2):
        await _laser(owner_engine, lab, capture)
        await _prediction(owner_engine, lab, capture)
    assert await _population(app_engine, lab) == []


async def test_returns_every_match_oldest_first(owner_engine, app_engine):
    lab = await _tenant(owner_engine)
    newer, c2, _ = await _predict_seed(owner_engine, lab, at=T0 + timedelta(hours=1))
    older, c1, _ = await _predict_seed(owner_engine, lab, at=T0)
    for capture in (c1, c2):
        await _prediction(owner_engine, lab, capture)
    assert await _population(app_engine, lab) == [older, newer]


# -- what populate and the backfill read and write -----------------------------------


async def test_populate_state_reads_candidates_predictions_and_live_labels(
    owner_engine, app_engine
):
    """v1's populate read `get_laser_labels`, `get_headtail_labels` and
    `get_headtail_predictions` for the dive; one snapshot here."""
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)
    valid = await _capture(owner_engine, lab, dive, v1_id=12)
    await _laser(owner_engine, lab, valid)
    await _laser(owner_engine, lab, valid)  # a second dot: still one candidate
    done = await _capture(owner_engine, lab, dive)
    await _laser(owner_engine, lab, done)
    await _headtail(owner_engine, lab, done, completed=True, task=5)
    no_laser = await _capture(owner_engine, lab, dive)
    stale = await _headtail(owner_engine, lab, no_laser, completed=False, task=6)
    await _headtail(owner_engine, lab, no_laser, project=2, superseded=True, task=7)
    await _prediction(owner_engine, lab, valid, version=FALLBACK)
    await _prediction(owner_engine, lab, valid, version=V)

    state = await _in(app_engine, lab, headtail_populate_state, dive)

    assert state.dive_number > 0
    assert [(c.capture_id, c.number, c.from_v1) for c in state.candidates] == [
        (valid, 12, True)
    ]
    assert [(p.capture_id, p.predictor_version) for p in state.predictions] == [
        (valid, V)
    ], "the current prediction only"
    assert {(label.capture_id, label.ls_task_id) for label in state.labels} == {
        (done, 5),
        (no_laser, 6),
    }, "live (not superseded) rows only"
    assert stale in {label.id for label in state.labels}


async def test_record_head_tail_task_seeds_a_pending_human_row(
    owner_engine, app_engine
):
    """v2 decision: a populate-seeded row's source is `human` -- the row a
    labeler will fill (docs/port-plan.md)."""
    lab = await _tenant(owner_engine)
    capture = await _capture(owner_engine, lab, await _dive(owner_engine, lab))

    await _in(
        app_engine,
        lab,
        record_head_tail_task,
        capture,
        ls_project_id=71,
        ls_task_id=900,
    )

    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT * FROM head_tail_labels WHERE capture_id = :c"),
                {"c": capture},
            )
        ).one()
    assert (row.ls_project_id, row.ls_task_id, row.completed, row.superseded) == (
        71,
        900,
        False,
        False,
    )
    assert row.source == "human"


async def test_recording_again_revives_the_row_and_never_erases_a_labelers_work(
    owner_engine, app_engine
):
    """v2 change. v1's populate PUT the whole row back blank on a re-record
    (completed False, points None), so a task a labeler had already answered
    -- whose cursor the sync had already passed -- lost its answer. Here the
    re-record revives the row and points it at the task; the labeler's columns
    are the sync's alone."""
    lab = await _tenant(owner_engine)
    capture = await _capture(owner_engine, lab, await _dive(owner_engine, lab))
    row = await _headtail(
        owner_engine, lab, capture, project=71, task=900, completed=True,
        superseded=True, head=(10.0, 20.0),
    )  # fmt: skip

    await _in(
        app_engine,
        lab,
        record_head_tail_task,
        capture,
        ls_project_id=71,
        ls_task_id=901,
    )

    label = await _label(owner_engine, row)
    assert (label.ls_task_id, label.superseded) == (901, False)
    assert (label.completed, label.head_x, label.head_y) == (True, 10.0, 20.0)


async def test_supersede_retires_only_the_named_live_incomplete_rows_of_the_dive(
    owner_engine, app_engine
):
    lab = await _tenant(owner_engine)
    dive = await _dive(owner_engine, lab)
    stale = await _headtail(
        owner_engine, lab, await _capture(owner_engine, lab, dive), completed=False
    )
    done = await _headtail(
        owner_engine, lab, await _capture(owner_engine, lab, dive), completed=True
    )
    elsewhere = await _headtail(
        owner_engine,
        lab,
        await _capture(owner_engine, lab, await _dive(owner_engine, lab)),
        completed=False,
    )

    n = await _in(
        app_engine, lab, supersede_head_tail_labels, dive, [stale, done, elsewhere]
    )

    assert n == 1
    assert (await _label(owner_engine, stale)).superseded is True
    assert (await _label(owner_engine, done)).superseded is False
    assert (await _label(owner_engine, elsewhere)).superseded is False


# -- the catalog: the orchestrator's service principal --------------------------------


async def test_the_catalog_acts_only_in_tenants_it_is_a_member_of(
    owner_engine, app_engine, seed_memberships
):
    tenants = await seed_memberships({ORCHESTRATOR: {"lab": "service"}})
    lab = tenants["lab"]
    reef = await _tenant(owner_engine, "reef")
    for tenant in (lab, reef):
        await _laser(
            owner_engine,
            tenant,
            await _capture(owner_engine, tenant, await _dive(owner_engine, tenant)),
        )
    catalog = HeadtailCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.member_tenants() == [lab]
    assert await catalog.next_dive_for_headtail_preprocessing(lab) is not None
    with pytest.raises(PermissionError):
        await catalog.next_dive_for_headtail_preprocessing(reef)
