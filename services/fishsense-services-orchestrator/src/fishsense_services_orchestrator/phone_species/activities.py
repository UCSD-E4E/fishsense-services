"""The phone species' orchestrator activities (the owner, 2026-11-11: "let's
add the missing species and run it").

BioCLIP names the fish in each phone capture the server measured, through
the species stage's processor workflow (`PredictSpeciesImagesWorkflow`),
unchanged: the photo by presigned URL (the tenant's bucket, which the
processor holds no key for) and the box of the server measurement's mask,
which the species crop pads by 20% as it does every fish. The contract's
``headtail_prediction_id`` carries the phone measurement's id: the result
comes back naming the measurement it was cropped to.

- `plan_phone_species`: the tenant's missions with phone fish owed a species
  in the region the run is told (a phone capture has no folder to find one
  from, and CCFRP's have no GPS), and how many;
- `resolve_phone_species_page`: a page of one mission's owed fish, each
  mask read to find its box. A mask that won't read, is empty, or is not the
  size the row records is counted and left out;
- `persist_phone_species`: each prediction recorded
  (`phone_species_predictions`), unvalidated.

The mask is in the camera's own grid (the measurement ignores the JPEG's EXIF
orientation) and the species crop decodes the JPEG with it applied: the same
grid for orientation 1, which every CCFRP photo has (checked 2026-11-09).
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass
from typing import Any, List, Optional

from PIL import Image
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.phone_species_store import PhoneSpeciesResult
from fishsense_services_api.tenant_stores import RENDER_EXPIRES_SECONDS
from fishsense_services_contracts.species_prediction import (
    SPECIES_PREDICTOR_VERSION,
    SPECIES_STATUSES,
    PredictSpeciesImage,
    PredictSpeciesImagesInput,
    SpeciesCandidate,
    SpeciesPredictionResult,
)
from fishsense_services_orchestrator.object_store.tenant_buckets import tenant_objects
from fishsense_services_orchestrator.species_predict.candidates import FISH_BRANCH

__all__ = [
    "PAGE",
    "PhoneSpeciesActivities",
    "PhoneSpeciesPage",
    "PhoneSpeciesTarget",
    "mask_box",
]

#: Fish per page (and per processor child): each carries one presigned GET.
PAGE = 100


@dataclass
class PhoneSpeciesTarget:
    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    region: str


@dataclass
class PhoneSpeciesPage:
    payload: Optional[PredictSpeciesImagesInput]
    #: The page's last capture id: the next page starts after it.
    last: Optional[str] = None
    #: Fish left out: the tenant has no bucket.
    unavailable: int = 0
    no_bucket: bool = False
    #: Fish left out: the mask would not read, was empty, or another size.
    unreadable: int = 0


def mask_box(data: Optional[bytes], width: int, height: int) -> Optional[List[int]]:
    """The box ``[x_min, y_min, x_max, y_max)`` of a mask's fish, or None for
    a mask that is missing, won't read, is empty or isn't ``width x height``."""
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.size != (width, height):
                return None
            box = image.convert("L").getbbox()
    except (OSError, ValueError):
        return None
    return None if box is None else list(box)


def _candidates(offered) -> List[SpeciesCandidate]:
    return [
        SpeciesCandidate(choice=f"{FISH_BRANCH}, {common} ({scientific})",
                         scientific_name=scientific)  # fmt: skip
        for common, scientific in offered
    ]


class PhoneSpeciesActivities:
    def __init__(self, *, catalog: Any, stores: Any) -> None:
        #: `PhoneSpeciesCatalog`.
        self._catalog = catalog
        #: `TenantStores`, by slug.
        self._stores = stores

    @activity.defn(name="plan_phone_species")
    async def plan_phone_species(
        self, tenant: str, region: str, numbers: Optional[List[int]] = None
    ) -> dict:
        tenant_id = None
        for t in await self._catalog.member_tenants():
            if await self._catalog.tenant_slug(t) == tenant:
                tenant_id = t
        if tenant_id is None:
            raise ApplicationError(f"no tenant {tenant!r}", type="NoSuchTenant",
                                   non_retryable=True)  # fmt: skip
        try:
            offered = await self._catalog.species_list(tenant_id, region)
        except ValueError as exc:
            raise ApplicationError(str(exc), type="NoSuchRegion",
                                   non_retryable=True) from exc  # fmt: skip
        missions = await self._catalog.owed_missions(
            tenant_id, region, predictor_version=SPECIES_PREDICTOR_VERSION
        )
        if numbers:
            missions = [m for m in missions if m.number in set(numbers)]
        return {
            "tenant": tenant, "tenant_id": str(tenant_id), "region": region,
            "species": len(offered),
            "missions": [{"mission_id": str(m.mission_id), "number": m.number,
                          "captures": m.captures} for m in missions],
        }  # fmt: skip

    @activity.defn(name="resolve_phone_species_page")
    async def resolve_phone_species_page(
        self, target: PhoneSpeciesTarget, after: Optional[str] = None
    ) -> PhoneSpeciesPage:
        rows = await self._catalog.owed_captures(
            target.tenant_id, target.mission_id, target.region,
            predictor_version=SPECIES_PREDICTOR_VERSION,
            after=None if after is None else uuid.UUID(after), limit=PAGE,
        )  # fmt: skip
        if not rows:
            return PhoneSpeciesPage(payload=None)
        last = str(rows[-1].capture_id)
        objects = await tenant_objects(self._catalog, self._stores, target.tenant_id)
        if objects is None:
            return PhoneSpeciesPage(payload=None, last=last, unavailable=len(rows),
                                    no_bucket=True)  # fmt: skip
        images, unreadable = [], 0
        for r in rows:
            data = objects.get(r.mask_key) if r.mask_bucket == objects.bucket else None
            box = mask_box(data, r.mask_width, r.mask_height)
            if box is None:
                unreadable += 1
                continue
            images.append(PredictSpeciesImage(
                capture_id=r.capture_id, headtail_prediction_id=r.phone_measurement_id,
                jpeg_url=objects.presign_get(r.rgb_key,
                                             expires_seconds=RENDER_EXPIRES_SECONDS),
                mask_bbox=box,
            ))  # fmt: skip
        candidates = _candidates(
            await self._catalog.species_list(target.tenant_id, target.region)
        )
        return PhoneSpeciesPage(
            payload=PredictSpeciesImagesInput(
                tenant_id=target.tenant_id,
                mission_id=target.mission_id,
                candidates=candidates,
                images=images,
            ),
            last=last,
            unreadable=unreadable,
        )

    @activity.defn(name="persist_phone_species")
    async def persist_phone_species(
        self, target: PhoneSpeciesTarget, results: List[SpeciesPredictionResult]
    ) -> int:
        rows = [
            PhoneSpeciesResult(
                capture_id=r.capture_id, phone_measurement_id=r.headtail_prediction_id,
                status=r.status, predictor_version=r.predictor_version,
                model_id=r.model_id, predicted_choice=r.predicted_choice,
                top1_probability=r.top1_probability, margin=r.margin,
                top5=[s.model_dump() for s in r.top5],
            )  # fmt: skip
            for r in results
            if r.status in SPECIES_STATUSES
        ]
        if any(r.predictor_version is None or not r.model_id for r in rows):
            raise ApplicationError("a species result names no version or model",
                                   type="BadSpeciesResult", non_retryable=True)  # fmt: skip
        written = (
            await self._catalog.record(target.tenant_id, target.region, rows)
            if rows
            else 0
        )
        activity.logger.info("phone species mission=%s: %d results recorded",
                             target.mission_id, written)  # fmt: skip
        return written
