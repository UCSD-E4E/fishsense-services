"""The slate detector's database side, on real Postgres.

New in v2 (v1's slate predictor estimated pose and was retired 2026-08-03).
The model is 2026-10-03_slate_detector@95a77d95's presence classifier; see
`fishsense_services_contracts.slate_presence`. Pinned here:

* **predictions are appended, never updated** (migration slate_01, like every
  prediction table), and `current_slate_presence` is the latest per capture;
* **the cohort**: a dive of **any** priority (this is for dives nobody
  labelled; the other cohorts are high-only, deliberately) whose device has a
  current pinhole calibration (the frame is rectified) and which has a
  canonical capture with no current prediction at the current model version.
  An abstention counts as a prediction; another version is stale. Oldest
  first;
* the resolver mirrors the selector, capture by capture, with the intrinsics;
* the processor's output is checked (PLAN.md §9.11): a prediction for a
  capture outside the dive is refused, and nothing is written;
* **what the automatic chain reads**: `slate_frames` (the dive's current slate
  frames, with their probability) and `slate_presence` (every current
  prediction);
* RLS scopes every row to its tenant, and the app role may only read and
  append.
"""

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from _slate_calibration_seed import (
    DISTORTION,
    K,
    T0,
    capture,
    device_with_camera,
    dive,
    later,
    tenant,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.slate_presence_store import (
    SLATE_PRESENCE_THRESHOLD,
    ForeignCapture,
    InvalidSlatePresence,
    SlateDetectionUnavailable,
    SlatePresenceCatalog,
    SlatePresenceRow,
    next_dive_for_slate_detection,
    persist_slate_presence,
    slate_detection_inputs,
    slate_frames,
    slate_presence,
)

V = 1  # SLATE_DETECTOR_VERSION, which the orchestrator passes in
SHA = "b8d377ba22d155e7056a5e9ae747fdd0970c7c73dee981bbee17d95c8156cf78"
ORCHESTRATOR = "service:fishsense-orchestrator"


async def _one(owner_engine, sql, **params):
    async with owner_engine.begin() as conn:
        result = await conn.execute(text(sql), params)
        return result.scalar_one() if result.returns_rows else None


async def _dive(owner_engine, lab, *, camera=True, **kwargs):
    device = (await device_with_camera(owner_engine, lab))[0] if camera else None
    return await dive(owner_engine, lab, device=device, **kwargs)


async def _predicted(owner_engine, lab, capture_id, *, probability=0.9, version=V,
                     status="predicted"):  # fmt: skip
    return await _one(
        owner_engine,
        "INSERT INTO slate_presence_predictions (tenant_id, capture_id, status, "
        "probability, model_version, weights_sha256) VALUES (:t, :c, :s, :p, :v, "
        ":sha) RETURNING id",
        t=lab,
        c=capture_id,
        s=status,
        p=probability if status == "predicted" else None,
        v=version,
        sha=SHA,
    )


async def _in(app_engine, lab, fn, *args, **kwargs):
    async with tenant_transaction(app_engine, lab) as conn:
        return await fn(conn, lab, *args, **kwargs)


async def _next(app_engine, lab, version=V):
    candidate = await _in(
        app_engine, lab, next_dive_for_slate_detection, model_version=version
    )
    return None if candidate is None else candidate.dive_id


def _row(capture_id, probability=0.97, **overrides):
    values = {
        "capture_id": capture_id,
        "status": "predicted",
        "probability": probability,
        "model_version": V,
        "weights_sha256": SHA,
    }
    return SlatePresenceRow(**{**values, **overrides})


# -- the schema ---------------------------------------------------------------------


async def test_a_prediction_has_its_probability_exactly_when_predicted(owner_engine):
    lab = await tenant(owner_engine)
    frame = await capture(owner_engine, lab, await _dive(owner_engine, lab))

    for status, probability in (("predicted", None), ("decode_failed", 0.5)):
        with pytest.raises(IntegrityError, match="check"):
            await _one(
                owner_engine,
                "INSERT INTO slate_presence_predictions (tenant_id, capture_id, "
                "status, probability, model_version, weights_sha256) VALUES "
                "(:t, :c, :s, :p, 1, :sha)",
                t=lab, c=frame, s=status, p=probability, sha=SHA,
            )  # fmt: skip


@pytest.mark.parametrize(
    ("column", "value"),
    [("probability", 1.5), ("status", "skipped"), ("weights_sha256", "abc")],
)
async def test_the_rows_are_checked(owner_engine, column, value):
    lab = await tenant(owner_engine)
    frame = await capture(owner_engine, lab, await _dive(owner_engine, lab))
    values = {"status": "predicted", "probability": 0.5, "weights_sha256": SHA}
    values[column] = value

    with pytest.raises(IntegrityError, match="check"):
        await _one(
            owner_engine,
            "INSERT INTO slate_presence_predictions (tenant_id, capture_id, status, "
            "probability, model_version, weights_sha256) VALUES (:t, :c, :status, "
            ":probability, 1, :weights_sha256)",
            t=lab,
            c=frame,
            **values,
        )


async def test_a_prediction_is_its_tenants_capture(owner_engine):
    lab = await tenant(owner_engine)
    reef = await tenant(owner_engine, "reef")
    frame = await capture(owner_engine, reef, await _dive(owner_engine, reef))

    with pytest.raises(IntegrityError, match="foreign key"):
        await _predicted(owner_engine, lab, frame)


# -- the cohort ---------------------------------------------------------------------


async def test_selects_a_dive_with_an_unpredicted_canonical_capture(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    await capture(owner_engine, lab, only)

    assert await _next(app_engine, lab) == only


@pytest.mark.parametrize("priority", ["high", "low"])
async def test_any_priority(owner_engine, app_engine, priority):
    """Unlike every other cohort: this is for dives nobody labelled."""
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab, priority=priority)
    await capture(owner_engine, lab, only)

    assert await _next(app_engine, lab) == only


async def test_drops_out_once_every_canonical_capture_is_predicted(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    await _predicted(owner_engine, lab, await capture(owner_engine, lab, only))
    await capture(owner_engine, lab, only, canonical=False)

    assert await _next(app_engine, lab) is None


async def test_an_abstention_is_a_prediction_too(owner_engine, app_engine):
    """Or a raw that never decodes re-selects its dive every hour."""
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await _predicted(owner_engine, lab, frame, status="decode_failed")

    assert await _next(app_engine, lab) is None


async def test_another_version_is_stale(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await _predicted(owner_engine, lab, frame, version=V)

    assert await _next(app_engine, lab, version=V + 1) == only


async def test_only_the_current_prediction_counts(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await _predicted(owner_engine, lab, frame, version=V)
    await _predicted(owner_engine, lab, frame, version=V - 1)

    assert await _next(app_engine, lab) == only


async def test_a_dive_that_cannot_be_rectified_is_not_offered(owner_engine, app_engine):
    """No device, or a device whose calibration is not a pinhole: the
    resolver would refuse it, and the oldest refused dive would be selected
    every hour ahead of every other (`camera_sql`)."""
    lab = await tenant(owner_engine)
    no_camera = await _dive(owner_engine, lab, camera=False, created_at=T0)
    await capture(owner_engine, lab, no_camera)
    device = await _one(
        owner_engine,
        "INSERT INTO devices (tenant_id, kind, serial) VALUES (:t, 'lite', :s) "
        "RETURNING id",
        t=lab,
        s=f"TG6-{uuid.uuid4()}",
    )
    await _one(
        owner_engine,
        "INSERT INTO camera_calibrations (tenant_id, device_id, camera_model, "
        "port_model, camera_matrix, distortion_coefficients) VALUES (:t, :d, "
        "'axial_refractive', 'flat', :k, '[0, 0, 0, 0, 0]')",
        t=lab,
        d=device,
        k=json.dumps(K),
    )
    axial = await dive(owner_engine, lab, device=device, created_at=later(1))
    await capture(owner_engine, lab, axial)

    assert await _next(app_engine, lab) is None


async def test_the_oldest_dive_first(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    newer = await _dive(owner_engine, lab, created_at=later(2))
    older = await _dive(owner_engine, lab, created_at=later(1))
    for each in (newer, older):
        await capture(owner_engine, lab, each)

    assert await _next(app_engine, lab) == older


async def test_another_tenants_dive_is_never_offered(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    reef = await tenant(owner_engine, "reef")
    await capture(owner_engine, reef, await _dive(owner_engine, reef))

    assert await _next(app_engine, lab) is None


# -- the resolver -------------------------------------------------------------------


async def test_resolves_the_unpredicted_canonical_captures_and_the_camera(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    first = await capture(owner_engine, lab, only, checksum="a" * 32)
    done = await capture(owner_engine, lab, only)
    stale = await capture(owner_engine, lab, only, checksum="c" * 32)
    await capture(owner_engine, lab, only, canonical=False)
    await _predicted(owner_engine, lab, done)
    await _predicted(owner_engine, lab, stale, version=V - 1)

    inputs = await _in(app_engine, lab, slate_detection_inputs, only, model_version=V)

    assert inputs.dive_id == only
    assert (inputs.camera_matrix, inputs.distortion_coefficients) == (K, DISTORTION)
    assert [(c.capture_id, c.checksum) for c in inputs.captures] == [
        (first, "a" * 32),
        (stale, "c" * 32),
    ]


async def test_a_dive_with_no_pinhole_camera_is_refused(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab, camera=False)
    await capture(owner_engine, lab, only)

    with pytest.raises(SlateDetectionUnavailable, match="camera"):
        await _in(app_engine, lab, slate_detection_inputs, only, model_version=V)


# -- persisting ----------------------------------------------------------------------


async def test_persist_appends_and_the_latest_is_current(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)

    assert await _in(app_engine, lab, persist_slate_presence, only, [_row(frame, 0.2)])
    await _in(app_engine, lab, persist_slate_presence, only, [_row(frame, 0.8)])

    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT probability, model_version, weights_sha256 "
                    "FROM slate_presence_predictions ORDER BY seq"
                )
            )
        ).all()
        current = await conn.execute(
            text("SELECT probability FROM current_slate_presence")
        )
        assert current.scalars().all() == [0.8]
    assert [tuple(r) for r in rows] == [(0.2, V, SHA), (0.8, V, SHA)]


async def test_an_abstention_is_recorded_without_a_probability(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)

    await _in(
        app_engine, lab, persist_slate_presence, only,
        [_row(frame, None, status="decode_failed")],
    )  # fmt: skip

    (current,) = await _in(app_engine, lab, slate_presence, only)
    assert (current.status, current.probability) == ("decode_failed", None)


async def test_a_prediction_for_a_capture_outside_the_dive_is_refused(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    mine = await capture(owner_engine, lab, only)
    elsewhere = await capture(owner_engine, lab, await _dive(owner_engine, lab))

    with pytest.raises(ForeignCapture):
        await _in(
            app_engine, lab, persist_slate_presence, only,
            [_row(mine), _row(elsewhere)],
        )  # fmt: skip
    assert issubclass(ForeignCapture, InvalidSlatePresence)

    async with owner_engine.connect() as conn:
        written = await conn.execute(
            text("SELECT count(*) FROM slate_presence_predictions")
        )
        assert written.scalar_one() == 0


async def test_nothing_to_persist_writes_nothing(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)

    assert await _in(app_engine, lab, persist_slate_presence, only, []) == 0


# -- what the automatic chain reads --------------------------------------------------


async def test_slate_frames_are_the_current_predictions_at_the_threshold(
    owner_engine, app_engine
):
    """In capture order, with their probability. The latest prediction is the
    one read, whatever its version: until a re-prediction lands it is the best
    there is."""
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    at_threshold = await capture(owner_engine, lab, only)
    below = await capture(owner_engine, lab, only)
    now_slate = await capture(owner_engine, lab, only)
    abstained = await capture(owner_engine, lab, only)
    await capture(owner_engine, lab, only)  # never predicted
    await _predicted(
        owner_engine, lab, at_threshold, probability=SLATE_PRESENCE_THRESHOLD
    )
    await _predicted(owner_engine, lab, below, probability=0.49)
    await _predicted(owner_engine, lab, now_slate, probability=0.1)
    await _predicted(owner_engine, lab, now_slate, probability=0.95, version=V + 1)
    await _predicted(owner_engine, lab, abstained, status="decode_failed")
    elsewhere = await capture(owner_engine, lab, await _dive(owner_engine, lab))
    await _predicted(owner_engine, lab, elsewhere)

    frames = await _in(app_engine, lab, slate_frames, only)

    assert frames == [(at_threshold, 0.5), (now_slate, 0.95)]


async def test_slate_frames_take_a_stricter_threshold(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    sure = await capture(owner_engine, lab, only)
    maybe = await capture(owner_engine, lab, only)
    await _predicted(owner_engine, lab, sure, probability=0.99)
    await _predicted(owner_engine, lab, maybe, probability=0.6)

    assert await _in(app_engine, lab, slate_frames, only, threshold=0.9) == [
        (sure, 0.99)
    ]


async def test_slate_presence_is_every_current_prediction(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await _predicted(owner_engine, lab, frame, probability=0.3, version=V - 1)
    latest = await _predicted(owner_engine, lab, frame, probability=0.02)

    (current,) = await _in(app_engine, lab, slate_presence, only)

    assert (current.id, current.capture_id, current.status) == (
        latest,
        frame,
        "predicted",
    )
    assert (current.probability, current.model_version, current.weights_sha256) == (
        0.02,
        V,
        SHA,
    )


# -- tenancy and append-only ----------------------------------------------------------


async def test_a_tenant_sees_only_its_own_predictions(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    reef = await tenant(owner_engine, "reef")
    frame = await capture(owner_engine, reef, await _dive(owner_engine, reef))
    await _predicted(owner_engine, reef, frame)

    async with tenant_transaction(app_engine, lab) as conn:
        for relation in ("slate_presence_predictions", "current_slate_presence"):
            seen = await conn.execute(text(f"SELECT count(*) FROM {relation}"))
            assert seen.scalar_one() == 0, relation


@pytest.mark.parametrize(
    "statement",
    ["UPDATE slate_presence_predictions SET probability = 0",
     "DELETE FROM slate_presence_predictions"],
)  # fmt: skip
async def test_the_app_role_may_only_append(owner_engine, app_engine, statement):
    lab = await tenant(owner_engine)
    frame = await capture(owner_engine, lab, await _dive(owner_engine, lab))
    await _predicted(owner_engine, lab, frame)

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_transaction(app_engine, lab) as conn:
            await conn.execute(text(statement))


async def test_the_catalog_acts_only_in_tenants_it_is_a_member_of(
    owner_engine, app_engine, seed_memberships
):
    tenants = await seed_memberships({ORCHESTRATOR: {"lab": "service"}})
    lab = tenants["lab"]
    reef = await tenant(owner_engine, "reef")
    for each in (lab, reef):
        await capture(owner_engine, each, await _dive(owner_engine, each))
    catalog = SlatePresenceCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.member_tenants() == [lab]
    candidate = await catalog.next_dive_for_slate_detection(lab, model_version=V)
    assert candidate is not None
    inputs = await catalog.slate_detection_inputs(
        lab, candidate.dive_id, model_version=V
    )
    (only,) = inputs.captures
    assert (
        await catalog.persist_slate_presence(
            lab, candidate.dive_id, [_row(only.capture_id)]
        )
        == 1
    )
    assert await catalog.slate_frames(lab, candidate.dive_id) == [
        (only.capture_id, 0.97)
    ]
    assert len(await catalog.slate_presence(lab, candidate.dive_id)) == 1
    with pytest.raises(PermissionError):
        await catalog.next_dive_for_slate_detection(reef, model_version=V)
