"""The phone species' orchestrator activities, against fakes.

- the plan finds the tenant by slug, refuses a region with no list, and keeps
  only the numbers asked for;
- a page gives BioCLIP each owed fish's photo by presigned URL, cropped to its
  mask's box, choosing among the region's fish; a mask that won't read is
  counted and left out; a tenant with no bucket is counted;
- persist records each prediction against the measurement it was cropped to,
  dropping a fallback worker's "no upgrade".
"""

from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.phone_species_store import (
    PHONE_SPECIES_STATUSES,
    OwedSpeciesCapture,
    OwedSpeciesMission,
)
from fishsense_services_api.tenant_stores import NoTenantStore
from fishsense_services_contracts.species_prediction import (
    SPECIES_STATUS_NO_UPGRADE_AVAILABLE,
    SPECIES_STATUSES,
    SpeciesPredictionResult,
    SpeciesScore,
)
from fishsense_services_orchestrator.phone_species.activities import (
    PhoneSpeciesActivities,
    PhoneSpeciesTarget,
)

TENANT, MISSION = uuid.uuid4(), uuid.uuid4()
SOCAL = "Southern California, USA"
OFFERED = [
    ("Kelp Bass", "Paralabrax clathratus"),
    ("Calico Rockfish", "Sebastes dallii"),
]


def test_the_api_repeats_the_contracts_statuses():
    assert PHONE_SPECIES_STATUSES == SPECIES_STATUSES


def _mask(box=(411, 626, 1362, 992), size=(1920, 1440)) -> bytes:
    image = Image.new("L", size, 0)
    image.paste(255, box)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _owed():
    c = uuid.uuid4()
    return OwedSpeciesCapture(
        capture_id=c, phone_measurement_id=uuid.uuid4(),
        bucket="fishsense-tenant-ccfrp", rgb_key=f"mobile/captures/{c}/rgb_jpeg.jpg",
        mask_bucket="fishsense-tenant-ccfrp", mask_key=f"masks/phone/{c}/m.png",
        mask_width=1920, mask_height=1440,
    )  # fmt: skip


class _Catalog:
    def __init__(self, rows=()):
        self.rows = sorted(rows, key=lambda r: r.capture_id)
        self.recorded = []

    async def member_tenants(self):
        return [TENANT]

    async def tenant_slug(self, t):
        return "ccfrp-ucsd"

    async def species_list(self, t, region):
        if region != SOCAL:
            raise ValueError(f"no species list for the region {region!r}")
        return OFFERED

    async def owed_missions(self, t, region, *, predictor_version):
        return [
            OwedSpeciesMission(MISSION, 534, 2),
            OwedSpeciesMission(uuid.uuid4(), 9, 1),
        ]

    async def owed_captures(self, t, m, region, *, predictor_version, after=None,
                            limit=100):  # fmt: skip
        return [r for r in self.rows if after is None or r.capture_id > after][:limit]

    async def record(self, t, region, results):
        self.recorded.append((region, list(results)))
        return len(results)


class _Objects:
    bucket = "fishsense-tenant-ccfrp"

    def __init__(self):
        self.data = {}

    def presign_get(self, key, *, expires_seconds):
        return f"https://g/get/{key}"

    def get(self, key):
        return self.data.get(key)


class _Stores:
    def __init__(self, objects):
        self.objects = objects

    def for_tenant(self, slug):
        if self.objects is None:
            raise NoTenantStore(slug)
        return self.objects


def _acts(rows=(), bucket=True):
    catalog, objects = _Catalog(rows), (_Objects() if bucket else None)
    acts = PhoneSpeciesActivities(catalog=catalog, stores=_Stores(objects))
    return acts, catalog, objects


async def _act(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


TARGET = PhoneSpeciesTarget(TENANT, MISSION, SOCAL)


async def test_the_plan_is_the_tenants_region_and_the_numbers_asked_for():
    acts, _, _ = _acts()
    plan = await _act(acts.plan_phone_species, "ccfrp-ucsd", SOCAL, [534])
    assert (plan["tenant_id"], plan["region"]) == (str(TENANT), SOCAL)
    assert plan["species"] == len(OFFERED)
    assert plan["missions"] == [
        {"mission_id": str(MISSION), "number": 534, "captures": 2}
    ]


async def test_a_region_with_no_list_is_refused():
    acts, _, _ = _acts()
    with pytest.raises(Exception, match="Atlantis"):
        await _act(acts.plan_phone_species, "ccfrp-ucsd", "Atlantis", [])


async def test_an_unknown_tenant_is_refused():
    acts, _, _ = _acts()
    with pytest.raises(Exception, match="no tenant"):
        await _act(acts.plan_phone_species, "nobody", SOCAL, [])


async def test_a_page_crops_each_photo_to_its_masks_box_among_the_regions_fish():
    rows = [_owed(), _owed()]
    acts, _, objects = _acts(rows)
    for r in rows:
        objects.data[r.mask_key] = _mask()

    page = await _act(acts.resolve_phone_species_page, TARGET, None)

    by_id = {i.capture_id: i for i in page.payload.images}
    for r in rows:
        image = by_id[r.capture_id]
        assert image.jpeg_url == f"https://g/get/{r.rgb_key}"
        assert image.mask_bbox == [411, 626, 1362, 992]
        assert image.headtail_prediction_id == r.phone_measurement_id
    assert [(c.choice, c.scientific_name) for c in page.payload.candidates] == [
        ("Fish, Kelp Bass (Paralabrax clathratus)", "Paralabrax clathratus"),
        ("Fish, Calico Rockfish (Sebastes dallii)", "Sebastes dallii"),
    ]
    assert page.last == str(max(r.capture_id for r in rows))


async def test_a_mask_that_wont_read_or_is_empty_or_another_size_is_left_out():
    gone, empty, other, good = _owed(), _owed(), _owed(), _owed()
    acts, _, objects = _acts([gone, empty, other, good])
    objects.data[empty.mask_key] = _mask(box=(0, 0, 0, 0))
    objects.data[other.mask_key] = _mask(size=(1440, 1920))
    objects.data[good.mask_key] = _mask()

    page = await _act(acts.resolve_phone_species_page, TARGET, None)

    assert [i.capture_id for i in page.payload.images] == [good.capture_id]
    assert page.unreadable == 3


async def test_no_bucket_is_counted_not_failed():
    acts, _, _ = _acts([_owed(), _owed()], bucket=False)
    page = await _act(acts.resolve_phone_species_page, TARGET, None)
    assert page.payload is None and page.no_bucket and page.unavailable == 2
    assert page.last is not None


def _result(r, status="predicted"):
    choice = "Fish, Calico Rockfish (Sebastes dallii)"
    return SpeciesPredictionResult(
        capture_id=r.capture_id, headtail_prediction_id=r.phone_measurement_id,
        status=status, predictor_version=1,
        model_id="hf-hub:imageomics/bioclip-2.5-vith14",
        predicted_choice=choice if status == "predicted" else None,
        top1_probability=0.8 if status == "predicted" else None,
        margin=0.5 if status == "predicted" else None,
        top5=[SpeciesScore(choice=choice, probability=0.8)] if status == "predicted" else [],
    )  # fmt: skip


async def test_persist_records_each_prediction_against_its_measurement():
    a, b = _owed(), _owed()
    acts, catalog, _ = _acts()

    written = await _act(
        acts.persist_phone_species,
        TARGET,
        [_result(a), _result(b, SPECIES_STATUS_NO_UPGRADE_AVAILABLE)],
    )

    assert written == 1
    [(region, [row])] = catalog.recorded
    assert region == SOCAL
    assert (row.capture_id, row.phone_measurement_id) == (
        a.capture_id,
        a.phone_measurement_id,
    )
    assert row.predicted_choice == "Fish, Calico Rockfish (Sebastes dallii)"
    assert row.top5 == [
        {"choice": "Fish, Calico Rockfish (Sebastes dallii)", "probability": 0.8}
    ]


async def test_a_mask_in_another_bucket_is_left_out():
    from dataclasses import replace

    elsewhere = replace(_owed(), mask_bucket="fishsense-tenant-lab")
    acts, _, objects = _acts([elsewhere])
    objects.data[elsewhere.mask_key] = _mask()

    page = await _act(acts.resolve_phone_species_page, TARGET, None)

    assert page.payload.images == [] and page.unreadable == 1
