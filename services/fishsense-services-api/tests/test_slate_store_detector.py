"""Stage 9 and the dive-slate project, fed by the slate detector too.

New in v2. Until now a frame reached a dive's slate project only when a
labeller answered its species task `Slate, Laser on slate`
(tests/test_slate_store.py, v1's path, unchanged). The slate detector
(2026-10-03_slate_detector@95a77d95; `slate_presence_store`) finds slate
frames in dives nobody labelled, and **a dive with no slate labels** has its
detector frames (current P(slate) >= 0.5) drawn and queued as well, so slate
calibration needs no frame-hunting. Pinned here:

* "no slate labels" means no person's slate work in the dive: no live species
  slate marker on a canonical frame, and no live slate label in a real project
  that the detector did not queue. So it stays true while the detector's own
  rows are labelled, and goes false for good once a person marks a slate
  (the marker path then owns the dive);
* a detector frame follows a marked frame's rules from there (canonical, no
  live slate label in a real project for stage 9, none completed for
  populate), and stage 9's other terms hold: high priority, a template, a
  dive it can resolve;
* **provenance**: a populate candidate the detector queued carries the
  prediction that queued it, and recording its task writes it on the slate
  label row (`slate_presence_prediction_id`); a marked frame carries none;
* the latest prediction counts, whatever its version, and an abstention is
  not a slate.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from _slate_calibration_seed import (
    SLATE_MARKER,
    capture,
    device_with_camera,
    dive,
    later,
    slate_label,
    slate_presence,
    slate_template,
    species,
    tenant,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.slate_store import (
    next_dive_for_slate_preprocessing,
    record_slate_label,
    slate_populate_candidates,
    slate_preprocess_inputs,
)


async def _slate_dive(owner_engine, lab, **kwargs):
    device, _ = await device_with_camera(owner_engine, lab)
    return await dive(
        owner_engine,
        lab,
        slate=await slate_template(owner_engine),
        device=device,
        **kwargs,
    )


async def _next(app_engine, lab):
    async with tenant_transaction(app_engine, lab) as conn:
        candidate = await next_dive_for_slate_preprocessing(conn, lab)
    return None if candidate is None else candidate.dive_id


async def _resolved(app_engine, lab, dive_id):
    async with tenant_transaction(app_engine, lab) as conn:
        inputs = await slate_preprocess_inputs(conn, lab, dive_id)
    return [c.capture_id for c in inputs.captures]


async def _candidates(app_engine, lab, dive_id):
    async with tenant_transaction(app_engine, lab) as conn:
        return await slate_populate_candidates(conn, lab, dive_id)


# ---------- stage 9: the cohort ----------


async def test_a_detected_slate_frame_in_an_unlabelled_dive_selects_it(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    await slate_presence(owner_engine, lab, await capture(owner_engine, lab, only))

    assert await _next(app_engine, lab) == only


@pytest.mark.parametrize("probability", [0.49, None])
async def test_below_the_threshold_or_an_abstention_is_not_a_slate(
    owner_engine, app_engine, probability
):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await slate_presence(owner_engine, lab, frame, probability=probability)

    assert await _next(app_engine, lab) is None


async def test_the_latest_prediction_counts_whatever_its_version(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    was_slate = await _slate_dive(owner_engine, lab, created_at=later(1))
    now_slate = await _slate_dive(owner_engine, lab, created_at=later(2))
    first = await capture(owner_engine, lab, was_slate)
    await slate_presence(owner_engine, lab, first, probability=0.9)
    await slate_presence(owner_engine, lab, first, probability=0.1, model_version=2)
    second = await capture(owner_engine, lab, now_slate)
    await slate_presence(owner_engine, lab, second, probability=0.1)
    await slate_presence(owner_engine, lab, second, probability=0.9, model_version=2)

    assert await _next(app_engine, lab) == now_slate


async def test_a_dive_a_person_marked_is_left_to_the_marker_path(
    owner_engine, app_engine
):
    """Its marked frame already has its task; the detector's frames are not
    added beside a person's."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    marked = await capture(owner_engine, lab, only)
    await species(owner_engine, lab, marked)
    await slate_label(owner_engine, lab, marked, completed=False)
    await slate_presence(owner_engine, lab, await capture(owner_engine, lab, only))

    assert await _next(app_engine, lab) is None


async def test_a_dive_with_a_persons_slate_label_is_not_fed_by_the_detector(
    owner_engine, app_engine
):
    """A v1 slate label (no marker left) is a person's slate work too."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    await slate_label(
        owner_engine, lab, await capture(owner_engine, lab, only), completed=True
    )
    await slate_presence(owner_engine, lab, await capture(owner_engine, lab, only))

    assert await _next(app_engine, lab) is None


@pytest.mark.parametrize(
    "label", [{"superseded": True}, {"project": None, "source": "import"}]
)
async def test_a_dead_or_sentinel_slate_label_is_not_a_persons_work(
    owner_engine, app_engine, label
):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    await slate_label(
        owner_engine, lab, await capture(owner_engine, lab, only), **label
    )
    await slate_presence(owner_engine, lab, await capture(owner_engine, lab, only))

    assert await _next(app_engine, lab) == only


async def test_a_superseded_marker_does_not_hand_the_dive_to_a_person(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    await species(
        owner_engine, lab, await capture(owner_engine, lab, only), superseded=True
    )
    await slate_presence(owner_engine, lab, await capture(owner_engine, lab, only))

    assert await _next(app_engine, lab) == only


async def test_the_detectors_own_rows_do_not_close_the_dive(owner_engine, app_engine):
    """A queued frame is labelled; the dive's other detected frame is still
    work: its rows are the detector's, not a person's slate labels."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    queued = await capture(owner_engine, lab, only)
    prediction = await slate_presence(owner_engine, lab, queued)
    await slate_label(owner_engine, lab, queued, completed=True, detected_by=prediction)
    pending = await capture(owner_engine, lab, only)
    await slate_presence(owner_engine, lab, pending)

    assert await _next(app_engine, lab) == only
    assert await _resolved(app_engine, lab, only) == [pending]


async def test_a_queued_frame_drops_out_once_its_task_exists(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    prediction = await slate_presence(owner_engine, lab, frame)
    await slate_label(owner_engine, lab, frame, completed=False, detected_by=prediction)

    assert await _next(app_engine, lab) is None


async def test_stage_9s_other_terms_still_hold(owner_engine, app_engine):
    """High priority, a slate template, canonical frames."""
    lab = await tenant(owner_engine)
    low = await _slate_dive(owner_engine, lab, priority="low")
    await slate_presence(owner_engine, lab, await capture(owner_engine, lab, low))
    device, _ = await device_with_camera(owner_engine, lab)
    no_template = await dive(owner_engine, lab, device=device)
    await slate_presence(
        owner_engine, lab, await capture(owner_engine, lab, no_template)
    )
    duplicate = await _slate_dive(owner_engine, lab)
    await slate_presence(
        owner_engine,
        lab,
        await capture(owner_engine, lab, duplicate, canonical=False),
    )

    assert await _next(app_engine, lab) is None


# ---------- stage 9: the resolver ----------


async def test_the_resolver_draws_the_detected_frames(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frames = [await capture(owner_engine, lab, only) for _ in range(3)]
    await slate_presence(owner_engine, lab, frames[0], probability=0.6)
    await slate_presence(owner_engine, lab, frames[1], probability=0.2)
    await slate_presence(owner_engine, lab, frames[2], probability=0.99)

    assert await _resolved(app_engine, lab, only) == [frames[0], frames[2]]


async def test_the_resolver_leaves_a_marked_dive_to_its_markers(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    marked = await capture(owner_engine, lab, only)
    await species(owner_engine, lab, marked, content=SLATE_MARKER)
    await slate_presence(owner_engine, lab, await capture(owner_engine, lab, only))

    assert await _resolved(app_engine, lab, only) == [marked]


# ---------- populate ----------


async def test_populate_queues_the_detected_frames_with_their_prediction(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    first = await capture(owner_engine, lab, only, captured_at=later(1))
    second = await capture(owner_engine, lab, only, captured_at=later(2))
    not_slate = await capture(owner_engine, lab, only, captured_at=later(3))
    p1 = await slate_presence(owner_engine, lab, first)
    await slate_presence(owner_engine, lab, second, probability=0.1)
    p2 = await slate_presence(owner_engine, lab, second, probability=0.8)
    await slate_presence(owner_engine, lab, not_slate, probability=0.1)

    candidates = await _candidates(app_engine, lab, only)

    assert [(c.capture_id, c.slate_presence_prediction_id) for c in candidates] == [
        (first, p1),
        (second, p2),
    ]


async def test_a_completed_queued_frame_is_not_queued_again(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    done = await capture(owner_engine, lab, only, captured_at=later(1))
    prediction = await slate_presence(owner_engine, lab, done)
    await slate_label(owner_engine, lab, done, completed=True, detected_by=prediction)
    open_task = await capture(owner_engine, lab, only, captured_at=later(2))
    prediction = await slate_presence(owner_engine, lab, open_task)
    await slate_label(
        owner_engine, lab, open_task, completed=False, detected_by=prediction
    )

    candidates = await _candidates(app_engine, lab, only)

    assert [c.capture_id for c in candidates] == [open_task]


async def test_a_marked_frame_carries_no_prediction(owner_engine, app_engine):
    """The marker path is unchanged: a marked dive's candidates are its marked
    frames, a person's, and none of the detector's."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    marked = await capture(owner_engine, lab, only, captured_at=later(1))
    await species(owner_engine, lab, marked)
    await slate_presence(owner_engine, lab, marked)
    await slate_presence(
        owner_engine, lab, await capture(owner_engine, lab, only, captured_at=later(2))
    )

    (candidate,) = await _candidates(app_engine, lab, only)

    assert (candidate.capture_id, candidate.slate_presence_prediction_id) == (
        marked,
        None,
    )


async def _provenance(owner_engine, frame):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT slate_presence_prediction_id FROM slate_labels "
                    "WHERE capture_id = :c"
                ),
                {"c": frame},
            )
        ).scalar_one()


async def test_recording_a_queued_frames_task_records_its_provenance(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    first = await slate_presence(owner_engine, lab, frame)

    async with tenant_transaction(app_engine, lab) as conn:
        await record_slate_label(
            conn, lab, frame, ls_project_id=66, ls_task_id=9101,
            image_url="s3://b/k", slate_presence_prediction_id=first,
        )  # fmt: skip
    assert await _provenance(owner_engine, frame) == first

    latest = await slate_presence(owner_engine, lab, frame, probability=0.8)
    async with tenant_transaction(app_engine, lab) as conn:
        await record_slate_label(
            conn, lab, frame, ls_project_id=66, ls_task_id=9101,
            image_url="s3://b/k", slate_presence_prediction_id=latest,
        )  # fmt: skip
    assert await _provenance(owner_engine, frame) == latest


async def test_a_marked_frames_task_records_none(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)

    async with tenant_transaction(app_engine, lab) as conn:
        await record_slate_label(
            conn, lab, frame, ls_project_id=66, ls_task_id=9102, image_url="s3://b/k"
        )

    assert await _provenance(owner_engine, frame) is None


async def test_provenance_is_the_tenants_own_prediction(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    reef = await tenant(owner_engine, "reef")
    frame = await capture(owner_engine, lab, await _slate_dive(owner_engine, lab))
    theirs = await slate_presence(
        owner_engine,
        reef,
        await capture(owner_engine, reef, await dive(owner_engine, reef)),
    )

    with pytest.raises(IntegrityError, match="foreign key"):
        async with tenant_transaction(app_engine, lab) as conn:
            await record_slate_label(
                conn, lab, frame, ls_project_id=66, ls_task_id=9103,
                image_url="s3://b/k", slate_presence_prediction_id=theirs,
            )  # fmt: skip
