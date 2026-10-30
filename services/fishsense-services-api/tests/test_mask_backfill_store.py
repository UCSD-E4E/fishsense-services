"""The mask backfill's database side, on real Postgres.

- the work is each current prediction that kept a mask and names none, with
  what regenerating it needs (dot, image, what it recorded); abstentions,
  masked and superseded predictions are not work;
- a mask is named against its existing prediction, never a new prediction;
  one for another capture is refused; a prediction already naming one is left;
- each tenant sees only its own (RLS).
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    capture,
    device,
    forget_identities,
    laser_label,
    mission,
    tenant,
)
from test_automatic_results_store import (
    _camera,
    _in,
    _mask,
    _predict,
    _predicted,
    _rows,
)
from fishsense_services_api.headtail_store import (
    HeadTailPredictionRow,
    persist_headtail_predictions,
)
from fishsense_services_api.mask_backfill_store import (
    MaskRegeneration,
    record_backfilled_masks,
    record_mask_regenerations,
    unmasked_missions,
    unmasked_predictions,
)


async def _mission(owner_engine, lab):
    return await mission(owner_engine, lab, device_id=await device(owner_engine, lab))


async def test_the_automatic_work_is_the_unmasked_kept_predictions(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    owed, masked, empty, older = [await capture(owner_engine, lab, m) for _ in range(4)]
    await _predict(app_engine, lab, m, _predicted(older))
    await _predict(app_engine, lab, m, _predicted(owed), _predicted(older, mask_area_px=777),
                   _predicted(empty, status="no_detections", mask_area_px=None,
                              crop_x=None, crop_y=None))  # fmt: skip
    camera = await _camera(app_engine, lab, m)
    from fishsense_services_api.automatic_results_store import (
        persist_automatic_head_tails,
    )

    await _in(app_engine, lab, persist_automatic_head_tails, m,
              [replace(_predicted(masked), jpeg_render_version=1,
                       jpeg_camera_calibration_id=camera)],
              masks=[_mask(masked)])  # fmt: skip

    (work,) = await _in(app_engine, lab, unmasked_missions, "automatic")
    assert (work.mission_id, work.predictions) == (m, 2)
    page = await _in(app_engine, lab, unmasked_predictions, "automatic", m)
    by = {p.capture_id: p for p in page}
    assert set(by) == {owed, older}
    assert by[older].mask_area_px == 777  # the current prediction, not the first
    p = by[owed]
    assert p.dot == (2000.0, 1500.0) and (p.crop_x, p.crop_y) == (1100, 825)
    assert (p.head_x, p.tail_x, p.checkpoint) == (1800.0, 2300.0, "sam3/3.1@abc")
    assert (p.render_version, p.camera_calibration_id) == (1, camera)


async def test_pages_follow_seq(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    caps = [await capture(owner_engine, lab, m) for _ in range(5)]
    await _predict(app_engine, lab, m, *[_predicted(c) for c in caps])
    first = await _in(app_engine, lab, unmasked_predictions, "automatic", m, limit=3)
    rest = await _in(app_engine, lab, unmasked_predictions, "automatic", m,
                     after_seq=first[-1].seq, limit=3)  # fmt: skip
    assert len(first) == 3 and len(rest) == 2
    assert {p.capture_id for p in first + rest} == set(caps)


async def _headtail(owner_engine, app_engine, lab, m, c, **extra):
    label = await laser_label(owner_engine, lab, c, x=2001.0, y=1499.0)
    row = HeadTailPredictionRow(
        capture_id=c, status="predicted", head_x=1800.0, head_y=1500.0,
        tail_x=2300.0, tail_y=1480.0, width=4000, height=3000, mask_area_px=40_000,
        crop_x=1101, crop_y=824, laser_label_id=label, predictor_version=2,
        checkpoint="/e4efs/cache/sam3/sam3.1_multiplex.pt", **extra,
    )  # fmt: skip
    await _in(app_engine, lab, persist_headtail_predictions, m, [row])
    return label


async def test_the_headtail_work_names_its_labeled_dot_and_jpeg(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    c = await capture(owner_engine, lab, m)
    label = await _headtail(owner_engine, app_engine, lab, m, c)
    (p,) = await _in(app_engine, lab, unmasked_predictions, "headtail", m)
    assert p.dot == (2001.0, 1499.0) and p.laser_label_id == label
    assert (p.crop_x, p.crop_y, p.mask_area_px) == (1101, 824, 40_000)
    assert p.checksum and p.from_v1 is False and p.render_version is None


async def test_a_mask_is_named_against_its_existing_prediction_once(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    c = await capture(owner_engine, lab, m)
    await _predict(app_engine, lab, m, _predicted(c))
    (p,) = await _in(app_engine, lab, unmasked_predictions, "automatic", m)
    before = await _rows(
        owner_engine, "SELECT count(*) AS n FROM automatic_head_tail_predictions"
    )

    assert await _in(app_engine, lab, record_backfilled_masks, "automatic",
                     [p.prediction_id], [_mask(c)]) == 1  # fmt: skip
    assert await _in(app_engine, lab, record_backfilled_masks, "automatic",
                     [p.prediction_id], [_mask(c, key="masks/automatic/abc/2.png")]) == 0  # fmt: skip

    (row,) = await _rows(owner_engine, "SELECT automatic_head_tail_prediction_id AS p, "
                         "object_key FROM prediction_masks")  # fmt: skip
    assert row == {"p": p.prediction_id, "object_key": "masks/automatic/abc/1.png"}
    assert (
        await _rows(
            owner_engine, "SELECT count(*) AS n FROM automatic_head_tail_predictions"
        )
        == before
    )
    assert await _in(app_engine, lab, unmasked_predictions, "automatic", m) == []


async def test_a_mask_of_another_capture_is_refused(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    c, other = await capture(owner_engine, lab, m), await capture(owner_engine, lab, m)
    await _predict(app_engine, lab, m, _predicted(c))
    (p,) = await _in(app_engine, lab, unmasked_predictions, "automatic", m)
    with pytest.raises(ValueError):
        await _in(app_engine, lab, record_backfilled_masks, "automatic",
                  [p.prediction_id], [_mask(other)])  # fmt: skip
    with pytest.raises(ValueError):
        await _in(app_engine, lab, record_backfilled_masks, "headtail",
                  [p.prediction_id], [_mask(c)])  # fmt: skip


async def test_a_headtail_mask_is_named_against_its_prediction(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    c = await capture(owner_engine, lab, m)
    await _headtail(owner_engine, app_engine, lab, m, c)
    (p,) = await _in(app_engine, lab, unmasked_predictions, "headtail", m)
    assert await _in(app_engine, lab, record_backfilled_masks, "headtail", [p.prediction_id],
                     [_mask(c, key="masks/headtail/abc/1.png")]) == 1  # fmt: skip
    assert await _in(app_engine, lab, unmasked_missions, "headtail") == []


async def test_a_tenant_sees_only_its_own_work(owner_engine, app_engine):
    lab, other = await tenant(owner_engine), await tenant(owner_engine, "reef")
    m = await _mission(owner_engine, lab)
    await _predict(app_engine, lab, m, _predicted(await capture(owner_engine, lab, m)))
    assert await _in(app_engine, other, unmasked_missions, "automatic") == []
    assert await _in(app_engine, other, unmasked_predictions, "automatic", m) == []


async def test_every_regeneration_is_recorded_with_what_it_got(
    owner_engine, app_engine
):
    """Owner, 2026-10-22: the regenerated head/tail is kept with every
    regeneration, stored or not, to measure the keypointer's repeatability."""
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    a, b = await capture(owner_engine, lab, m), await capture(owner_engine, lab, m)
    await _predict(app_engine, lab, m, _predicted(a), _predicted(b))
    pa, pb = sorted(await _in(app_engine, lab, unmasked_predictions, "automatic", m),
                    key=lambda p: p.capture_id == b)  # fmt: skip
    await _in(app_engine, lab, record_backfilled_masks, "automatic", [pa.prediction_id],
              [_mask(a)])  # fmt: skip
    written = await _in(app_engine, lab, record_mask_regenerations, "automatic", [
        MaskRegeneration(prediction_id=pa.prediction_id, capture_id=a, outcome="reproduced",
                         mask_area_px=50_010, crop_x=1100, crop_y=825, head_x=1801.0,
                         head_y=1500.0, tail_x=2300.0, tail_y=1480.0),
        MaskRegeneration(prediction_id=pb.prediction_id, capture_id=b, outcome="no_mask"),
    ])  # fmt: skip
    assert written == 2
    rows = await _rows(owner_engine, """
        SELECT r.automatic_head_tail_prediction_id AS p, r.outcome, r.head_x,
               r.prediction_mask_id IS NOT NULL AS stored
        FROM mask_regenerations r ORDER BY r.seq""")  # fmt: skip
    assert rows == [{"p": pa.prediction_id, "outcome": "reproduced", "head_x": 1801.0,
                     "stored": True},
                    {"p": pb.prediction_id, "outcome": "no_mask", "head_x": None,
                     "stored": False}]  # fmt: skip


async def test_a_regeneration_of_another_capture_is_refused(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    a, b = await capture(owner_engine, lab, m), await capture(owner_engine, lab, m)
    await _predict(app_engine, lab, m, _predicted(a))
    (p,) = await _in(app_engine, lab, unmasked_predictions, "automatic", m)
    with pytest.raises(ValueError):
        await _in(app_engine, lab, record_mask_regenerations, "automatic",
                  [MaskRegeneration(prediction_id=p.prediction_id, capture_id=b,
                                    outcome="mismatch")])  # fmt: skip
