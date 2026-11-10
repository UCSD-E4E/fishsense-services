"""The phone measurement's orchestrator activities, against fakes.

- the plan finds the tenant by slug and keeps only the numbers asked for;
- a page gives each owed capture presigned GETs for its JPEG and depth, its
  depth geometry and K, and a mask upload; a tenant with no bucket is counted;
- persist records every result, naming only masks that read back as
  reported; a mask that doesn't is deleted and its result kept without it.
"""

from __future__ import annotations

import hashlib
import struct
import uuid
from types import SimpleNamespace

import pytest
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.phone_measurement_store import (
    PHONE_STATUSES as API_STATUSES,
    OwedMission,
    OwedPhoneCapture,
)
from fishsense_services_api.tenant_stores import NoTenantStore
from fishsense_services_contracts.masks import StoredMask, mask_key
from fishsense_services_contracts.phone_measurement import (
    PHONE_ALGORITHM,
    PHONE_STATUSES,
    PhoneMeasured,
)
from fishsense_services_orchestrator.phone_measurement.activities import (
    PhoneMeasurementActivities,
    PhoneMeasurementTarget,
)

TENANT, MISSION = uuid.uuid4(), uuid.uuid4()
K = (1604.2147, 0.0, 956.5816, 0.0, 1604.2147, 717.7617, 0.0, 0.0, 1.0)


def test_the_api_repeats_the_contracts_statuses():
    assert API_STATUSES == PHONE_STATUSES


def _owed(capture_id=None):
    c = capture_id or uuid.uuid4()
    return OwedPhoneCapture(capture_id=c, bucket="fishsense-tenant-ccfrp",
                            rgb_key=f"mobile/captures/{c}/rgb_jpeg.jpg",
                            depth_key=f"mobile/captures/{c}/depth_f32.bin",
                            depth_width=256, depth_height=192, depth_row_bytes=1024,
                            intrinsics=K)  # fmt: skip


class _Catalog:
    def __init__(self, rows=()):
        self.rows = sorted(rows, key=lambda r: r.capture_id)
        self.recorded = []

    async def member_tenants(self):
        return [TENANT]

    async def tenant_slug(self, t):
        return "ccfrp-ucsd"

    async def owed_missions(self, t):
        return [OwedMission(MISSION, 3, 2), OwedMission(uuid.uuid4(), 4, 1)]

    async def owed_captures(self, t, m, *, after=None, limit=100):
        return [r for r in self.rows if after is None or r.capture_id > after][:limit]

    async def record(self, t, results):
        self.recorded.extend(results)
        return len(results)


def _png(w=1920, h=1440):
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", w, h)
            + bytes([8, 0, 0, 0, 0]) + b"\x00" * 8)  # fmt: skip


class _Objects:
    bucket = "fishsense-tenant-ccfrp"

    def __init__(self):
        self.data, self.deleted = {}, []

    def presign_get(self, key, *, expires_seconds):
        return f"https://g/get/{key}"

    def presign_mask_put(self, key):
        return SimpleNamespace(url=f"https://g/put/{key}")

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.deleted.append(key)


class _Stores:
    def __init__(self, objects):
        self.objects = objects

    def for_tenant(self, slug):
        if self.objects is None:
            raise NoTenantStore(slug)
        return self.objects


def _acts(rows=(), bucket=True):
    catalog, objects = _Catalog(rows), (_Objects() if bucket else None)
    return (
        PhoneMeasurementActivities(catalog=catalog, stores=_Stores(objects)),
        catalog,
        objects,
    )


async def _act(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


async def test_the_plan_is_the_tenants_and_the_numbers_asked_for():
    acts, _, _ = _acts()
    plan = await _act(acts.plan_phone_measurement, "ccfrp-ucsd", [3])
    assert plan["tenant_id"] == str(TENANT)
    assert plan["missions"] == [
        {"mission_id": str(MISSION), "number": 3, "captures": 2}
    ]


async def test_an_unknown_tenant_is_refused():
    acts, _, _ = _acts()
    with pytest.raises(Exception, match="no tenant"):
        await _act(acts.plan_phone_measurement, "nobody", [])


async def test_a_page_reads_each_captures_files_by_url_with_its_depth_and_k():
    rows = [_owed(), _owed()]
    acts, _, _ = _acts(rows)
    page = await _act(acts.resolve_phone_measurement_page,
                      PhoneMeasurementTarget(TENANT, MISSION), None)  # fmt: skip
    frames = page.payload.frames
    by_id = {f.capture_id: f for f in frames}
    for r in rows:
        f = by_id[r.capture_id]
        assert f.rgb_url == f"https://g/get/{r.rgb_key}"
        assert f.depth_url == f"https://g/get/{r.depth_key}"
        assert (f.depth_width, f.depth_height, f.depth_row_bytes) == (256, 192, 1024)
        assert tuple(f.intrinsics) == K
        assert f.mask.key.startswith(f"masks/phone/{r.capture_id}/")
    assert page.last == str(max(r.capture_id for r in rows))


async def test_the_end_is_an_empty_page():
    acts, _, _ = _acts([])
    page = await _act(acts.resolve_phone_measurement_page,
                      PhoneMeasurementTarget(TENANT, MISSION), None)  # fmt: skip
    assert page.payload is None and page.last is None


async def test_no_bucket_is_counted_not_failed():
    acts, _, _ = _acts([_owed(), _owed()], bucket=False)
    page = await _act(acts.resolve_phone_measurement_page,
                      PhoneMeasurementTarget(TENANT, MISSION), None)  # fmt: skip
    assert page.payload is None and page.no_bucket and page.unavailable == 2
    assert page.last is not None


def _measured(capture_id, mask=None):
    return PhoneMeasured(capture_id=capture_id, status="measured", length_m=0.28,
                         snout_x=1.0, snout_y=2.0, fork_x=3.0, fork_y=4.0,
                         snout_depth_m=0.4, fork_depth_m=0.4, model_id="fishial/1@x",
                         core_version="4.3.2", algorithm=PHONE_ALGORITHM, mask=mask)  # fmt: skip


async def test_persist_records_every_result_and_names_only_masks_that_read_back():
    acts, catalog, objects = _acts()
    good_c, bad_c, none_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    good_key, bad_key = mask_key("phone", good_c, uuid.uuid4()), mask_key(
        "phone", bad_c, uuid.uuid4()
    )
    data = _png()
    objects.data[good_key] = data
    objects.data[bad_key] = b"not what was reported"

    def stored(key):
        return StoredMask(key=key, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                          width=1920, height=1440)  # fmt: skip

    results = [_measured(good_c, stored(good_key)), _measured(bad_c, stored(bad_key)),
               PhoneMeasured(capture_id=none_c, status="no_fish")]  # fmt: skip
    written = await _act(acts.persist_phone_measurements,
                         PhoneMeasurementTarget(TENANT, MISSION), results)  # fmt: skip

    assert written == 3
    got = {r.capture_id: r for r in catalog.recorded}
    assert (
        got[good_c].mask.key == good_key and got[good_c].mask.bucket == objects.bucket
    )
    assert got[bad_c].mask is None and got[bad_c].length_m == 0.28
    assert got[none_c].status == "no_fish" and got[none_c].mask is None
    assert objects.deleted == [bad_key]


async def test_a_mask_under_another_captures_key_is_not_named():
    acts, catalog, objects = _acts()
    mine, other = uuid.uuid4(), uuid.uuid4()
    key = mask_key("phone", other, uuid.uuid4())
    objects.data[key] = data = _png()
    stored = StoredMask(key=key, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                        width=1920, height=1440)  # fmt: skip
    await _act(acts.persist_phone_measurements, PhoneMeasurementTarget(TENANT, MISSION),
               [_measured(mine, stored)])  # fmt: skip
    assert catalog.recorded[0].mask is None and objects.deleted == [key]
