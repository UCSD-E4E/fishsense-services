"""The slate detector's database side, on real Postgres.

New in v2 (v1's slate predictor estimated pose and was retired 2026-08-03).
The model is 2026-10-03_slate_detector@95a77d95's presence classifier; see
`fishsense_services_contracts.slate_presence`. Pinned here:

* **predictions are appended, never updated** (migration slate_01, like every
  prediction table), and `current_slate_presence` is the latest per capture;
* **the cohort**: a dive of **any** priority, labelled or not (every canonical
  frame is scored, for publication; the other cohorts are high-only,
  deliberately) whose device has a
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
from datetime import timedelta

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
    slate_label,
    species,
    tenant,
)
from _slate_calibration_seed import slate_presence as slate_presence_row
from research_seed import rows
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
                     status="predicted", **columns):  # fmt: skip
    return await slate_presence_row(
        owner_engine,
        lab,
        capture_id,
        probability=probability if status == "predicted" else None,
        model_version=version,
        **columns,
    )


async def _in(app_engine, lab, fn, *args, **kwargs):
    async with tenant_transaction(app_engine, lab) as conn:
        return await fn(conn, lab, *args, **kwargs)


async def _next(app_engine, lab, version=V):
    candidate = await _in(
        app_engine, lab, next_dive_for_slate_detection, model_version=version
    )
    return None if candidate is None else candidate.dive_id


AT = T0 + timedelta(days=34)
RENDER = {
    "decode_config": "production",
    "decode_params": {"stretch_mode": "off", "clahe_enabled": True},
    "rectified": True,
    "cache_long_side": 1600,
    "jpeg_quality": 95,
    "input_width": 1024,
    "input_height": 768,
    "tta": "hflip",
}


def _row(capture_id, probability=0.97, **overrides):
    values = {
        "capture_id": capture_id,
        "status": "predicted",
        "probability": probability,
        "model_name": "slate-detector",
        "model_version": V,
        "weights_sha256": SHA,
        "core_version": "4.1.0",
        "processor_version": "0.1.2",
        "render": RENDER,
        "predicted_at": AT,
    }
    return SlatePresenceRow(**{**values, **overrides})


# -- the schema ---------------------------------------------------------------------


async def test_a_prediction_has_its_probability_exactly_when_predicted(owner_engine):
    lab = await tenant(owner_engine)
    frame = await capture(owner_engine, lab, await _dive(owner_engine, lab))

    for status, probability in (("predicted", None), ("decode_failed", 0.5)):
        with pytest.raises(IntegrityError, match="check"):
            await slate_presence_row(
                owner_engine, lab, frame, status=status, probability=probability
            )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("probability", 1.5),
        ("status", "skipped"),
        ("weights_sha256", "abc"),
        ("input_width", 0),
    ],
)
async def test_the_rows_are_checked(owner_engine, column, value):
    lab = await tenant(owner_engine)
    frame = await capture(owner_engine, lab, await _dive(owner_engine, lab))

    with pytest.raises(IntegrityError, match="check"):
        await slate_presence_row(owner_engine, lab, frame, **{column: value})


@pytest.mark.parametrize(
    "column",
    ["model_name", "decode_config", "rectified", "input_width", "input_height",
     "render", "predicted_at"],
)  # fmt: skip
async def test_a_prediction_must_say_how_it_was_made(owner_engine, column):
    """Publication-grade: no row without its model, its render and its time."""
    lab = await tenant(owner_engine)
    frame = await capture(owner_engine, lab, await _dive(owner_engine, lab))

    with pytest.raises(IntegrityError, match="null"):
        await slate_presence_row(owner_engine, lab, frame, **{column: None})


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
    """Unlike every other cohort: every canonical frame is scored."""
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab, priority=priority)
    await capture(owner_engine, lab, only)

    assert await _next(app_engine, lab) == only


async def test_labelled_frames_are_scored_too(owner_engine, app_engine):
    """The owner's decision: every canonical frame, so the model can be
    evaluated against the answers people already gave."""
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await species(owner_engine, lab, frame)
    await slate_label(owner_engine, lab, frame, completed=True)

    assert await _next(app_engine, lab) == only
    inputs = await _in(app_engine, lab, slate_detection_inputs, only, model_version=V)
    assert [c.capture_id for c in inputs.captures] == [frame]


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


async def test_persist_records_how_each_prediction_was_made(owner_engine, app_engine):
    """Publication-grade: the model, weights, fishsense-core and processor
    versions, the render (its headline settings as columns, all of it as
    JSON), the probability and when it ran."""
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)

    await _in(app_engine, lab, persist_slate_presence, only, [_row(frame, 0.73)])

    (row,) = await rows(
        owner_engine,
        "SELECT probability, model_name, model_version, weights_sha256, "
        "core_version, processor_version, decode_config, rectified, input_width, "
        "input_height, render, predicted_at FROM slate_presence_predictions",
    )
    assert row == {
        "probability": 0.73,
        "model_name": "slate-detector",
        "model_version": V,
        "weights_sha256": SHA,
        "core_version": "4.1.0",
        "processor_version": "0.1.2",
        "decode_config": "production",
        "rectified": True,
        "input_width": 1024,
        "input_height": 768,
        "render": RENDER,
        "predicted_at": AT,
    }


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
