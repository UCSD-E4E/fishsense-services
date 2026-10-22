"""The mask backfill's orchestrator activities, against fakes.

- the plan finds the tenant by slug and keeps only the numbers asked for;
- a page gives each automatic prediction the render it names (version and
  camera) by presigned GET, each head/tail prediction its stage-5.1 JPEG, and
  each a mask upload; a gone image is counted, a tenant with no bucket too;
- persist names only masks that read back as reported; the rest are deleted.
"""

from __future__ import annotations

import hashlib
import struct
import uuid
from types import SimpleNamespace

import pytest
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.mask_backfill_store import (
    UnmaskedMission,
    UnmaskedPrediction,
)
from fishsense_services_contracts.mask_backfill import BackfilledMask
from fishsense_services_contracts.masks import StoredMask, mask_key
from fishsense_services_contracts.object_store import HEADTAIL_JPEG_FOLDER, ObjectRef
from fishsense_services_orchestrator.mask_backfill.activities import (
    MaskBackfillActivities,
    MaskBackfillTarget,
)

TENANT, MISSION, CAMERA = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _row(seq, **kw):
    fields = dict(prediction_id=uuid.uuid4(), seq=seq, capture_id=uuid.uuid4(),
                  dot=(2000.0, 1500.0), laser_label_id=None, mask_area_px=100,
                  crop_x=1, crop_y=2, head_x=3.0, head_y=4.0, tail_x=5.0, tail_y=6.0,
                  checkpoint="sam3/3.1@abc", render_version=1,
                  camera_calibration_id=CAMERA, checksum=f"sum{seq}", from_v1=False)  # fmt: skip
    fields.update(kw)
    return UnmaskedPrediction(**fields)


class _Catalog:
    def __init__(self, rows=(), slugs=None):
        self.rows = list(rows)
        self.slugs = slugs or {TENANT: "lab"}
        self.recorded = []

    async def member_tenants(self):
        return list(self.slugs)

    async def tenant_slug(self, t):
        return self.slugs[t]

    async def unmasked_missions(self, t, source):
        return [UnmaskedMission(MISSION, 7, 3), UnmaskedMission(uuid.uuid4(), 8, 1)]

    async def unmasked_predictions(self, t, source, m, *, after_seq=0, limit=200):
        return [r for r in self.rows if r.seq > after_seq][:limit]

    async def record_backfilled_masks(self, t, source, predictions, masks):
        self.recorded.append((source, list(predictions), list(masks)))
        return len(predictions)


def _png(w=4014, h=3016):
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", w, h)
            + bytes([8, 0, 0, 0, 0]) + b"\x00" * 8)  # fmt: skip


class _Objects:
    bucket = "fishsense-tenant-lab"

    def __init__(self):
        self.data, self.deleted = {}, []

    def presign_mask_put(self, key):
        return SimpleNamespace(url=f"https://g/put/{key}")

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.deleted.append(key)


class _Renders:
    def __init__(self, rendered, objects=True):
        self.rendered, self.asked = rendered, []
        self._objects = _Objects() if objects else None

    async def objects(self, t):
        return self._objects

    async def at(self, t, ids, *, render_version, camera_calibration_id):
        self.asked.append((render_version, camera_calibration_id))
        return {c: SimpleNamespace(status="rendered", key=f"renders/{c}.JPG")
                for c in ids if c in self.rendered}  # fmt: skip

    async def presigner(self, t):
        return lambda key: f"https://g/get/{key}"


class _Store:
    def __init__(self, written):
        self.written = written

    async def locate_processed_jpeg(self, t, folder, checksum, *, from_v1):
        assert folder == HEADTAIL_JPEG_FOLDER
        return (
            ObjectRef(bucket="fishsense-labels", key=f"x/{checksum}.JPG")
            if checksum in self.written
            else None
        )


async def _act(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


def _acts(rows=(), rendered=None, objects=True, written=()):
    catalog = _Catalog(rows)
    rendered = {r.capture_id for r in rows} if rendered is None else rendered
    renders = _Renders(rendered, objects)
    return MaskBackfillActivities(catalog=catalog, renders=renders,
                                  store=_Store(set(written))), catalog, renders  # fmt: skip


async def test_the_plan_is_the_tenants_and_the_numbers_asked_for():
    acts, _, _ = _acts()
    plan = await acts.plan_mask_backfill("lab", "automatic", [7])
    assert plan["tenant_id"] == str(TENANT)
    assert plan["missions"] == [
        {"mission_id": str(MISSION), "number": 7, "predictions": 3}
    ]
    assert len((await acts.plan_mask_backfill("lab", "automatic"))["missions"]) == 2


async def test_an_automatic_page_reads_the_render_its_prediction_names():
    other_camera = uuid.uuid4()
    a, b, gone = (
        _row(1),
        _row(2, render_version=2, camera_calibration_id=other_camera),
        _row(3),
    )
    acts, _, renders = _acts([a, b, gone], rendered={a.capture_id, b.capture_id})
    page = await _act(
        acts.resolve_mask_backfill_page,
        MaskBackfillTarget(TENANT, MISSION, "automatic"),
    )
    assert sorted(renders.asked, key=str) == sorted(
        [(1, CAMERA), (2, other_camera)], key=str
    )
    by = {f.prediction_id: f for f in page.payload.frames}
    assert set(by) == {a.prediction_id, b.prediction_id}
    assert by[a.prediction_id].render_url == f"https://g/get/renders/{a.capture_id}.JPG"
    assert by[a.prediction_id].expect.mask_area_px == 100
    assert by[a.prediction_id].mask.key.startswith(f"masks/automatic/{a.capture_id}/")
    assert (page.last_seq, page.unavailable) == (3, 1)


async def test_a_headtail_page_reads_the_stage_jpeg():
    label = uuid.uuid4()
    a, gone = _row(1, laser_label_id=label), _row(2)
    acts, _, _ = _acts([a, gone], written={a.checksum})
    page = await _act(
        acts.resolve_mask_backfill_page, MaskBackfillTarget(TENANT, MISSION, "headtail")
    )
    (f,) = page.payload.frames
    assert f.jpeg.key == f"x/{a.checksum}.JPG" and f.render_url is None
    assert f.laser_label_ids == [label] and f.mask.key.startswith("masks/headtail/")
    assert page.unavailable == 1


async def test_no_bucket_is_counted_not_failed():
    acts, _, _ = _acts([_row(1)], objects=False)
    page = await _act(
        acts.resolve_mask_backfill_page,
        MaskBackfillTarget(TENANT, MISSION, "automatic"),
    )
    assert page.payload is None and page.no_bucket and page.unavailable == 1


async def test_the_end_is_an_empty_page():
    acts, _, _ = _acts([])
    page = await _act(
        acts.resolve_mask_backfill_page,
        MaskBackfillTarget(TENANT, MISSION, "automatic"),
    )
    assert page.payload is None and page.last_seq is None


async def test_persist_names_only_masks_that_read_back():
    acts, catalog, renders = _acts()
    objects = renders._objects
    good_c, bad_c = uuid.uuid4(), uuid.uuid4()
    good_key = mask_key("automatic", good_c, uuid.uuid4())
    bad_key = mask_key("automatic", bad_c, uuid.uuid4())
    data = _png()
    objects.data[good_key] = data
    objects.data[bad_key] = b"not what was reported"

    def stored(key):
        return StoredMask(key=key, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                          width=4014, height=3016)  # fmt: skip

    good = BackfilledMask(prediction_id=uuid.uuid4(), capture_id=good_c,
                          outcome="reproduced", mask=stored(good_key))  # fmt: skip
    bad = BackfilledMask(prediction_id=uuid.uuid4(), capture_id=bad_c,
                         outcome="reproduced", mask=stored(bad_key))  # fmt: skip
    miss = BackfilledMask(prediction_id=uuid.uuid4(), capture_id=uuid.uuid4(),
                          outcome="mismatch")  # fmt: skip
    named = await _act(acts.persist_mask_backfill, MaskBackfillTarget(TENANT, MISSION, "automatic"),
                                             [good, bad, miss])  # fmt: skip
    assert named == 1
    ((source, predictions, masks),) = catalog.recorded
    assert source == "automatic" and predictions == [good.prediction_id]
    assert masks[0].key == good_key and objects.deleted == [bad_key]
