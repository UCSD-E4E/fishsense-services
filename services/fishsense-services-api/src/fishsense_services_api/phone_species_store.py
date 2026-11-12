"""Species for the fish on phone captures: the database side (0090; the
owner, 2026-11-11: "let's add the missing species and run it").

BioCLIP names the fish in each phone capture the server measured
(`phone_measurement_store`), cropped to that measurement's mask. A phone
capture has no folder to find a region from, and CCFRP's have no GPS, so the
run is told its region (the owner: "all of the ccfrp photos were taken in
san diego"), and the fish offered are that region's 0085 list and the
species added for it (`regional_species_additions`). REEF's 13 targets are
not offered: they are Florida's, and a phone run is told where it was.

* `phone_species_list`: what a region offers, its list in rank order, then
  its additions;
* `owed_missions` / `owed_captures`: each measured phone fish with a mask
  and no prediction for its current measurement from this predictor, region
  and list;
* `record_phone_species`: each result appended. A prediction for a
  measurement that is not the capture's current one, or naming a fish the
  region does not offer, is refused: the processor runs on machines we
  don't own.

The result is unvalidated, like the length it sits beside.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "PHONE_SPECIES_LIST_VERSION",
    "PHONE_SPECIES_STATUSES",
    "OwedSpeciesCapture",
    "OwedSpeciesMission",
    "PhoneSpeciesCatalog",
    "PhoneSpeciesResult",
    "choice_scientific_name",
    "owed_captures",
    "owed_missions",
    "phone_species_list",
    "record_phone_species",
]

#: Bumped by hand whenever what a region offers a phone fish changes: a
#: list's or its additions' species. 1 (2026-11-11): 0085's lists and 0090's
#: additions.
PHONE_SPECIES_LIST_VERSION = 1

#: The species contract's (`SPECIES_STATUSES`), repeated: the API image has
#: no contracts package. The orchestrator pins them equal.
PHONE_SPECIES_STATUSES = ("predicted", "decode_failed")

_SCIENTIFIC = re.compile(r"\(([^()]*)\)\s*$")


def choice_scientific_name(choice: str) -> str | None:
    """The scientific name in a choice, "Fish, Calico Rockfish (Sebastes
    dallii)": its last parenthesized part."""
    found = _SCIENTIFIC.search(choice)
    return None if found is None else found.group(1)


async def phone_species_list(
    conn: AsyncConnection, region: str
) -> list[tuple[str, str]]:
    """What a region offers, as (common name, scientific name): its list in
    rank order, then its additions by common name. A region with neither is
    refused."""
    rows = await conn.execute(
        text("""
            SELECT common_name, scientific_name FROM (
                SELECT common_name, scientific_name, 0 AS part, rank, '' AS name
                FROM regional_species WHERE region = :r
                UNION ALL
                SELECT a.common_name, a.scientific_name, 1, 0, a.common_name
                FROM regional_species_additions a
                WHERE a.region = :r AND NOT EXISTS (
                    SELECT 1 FROM regional_species s
                    WHERE s.region = a.region
                      AND s.scientific_name = a.scientific_name)
            ) x ORDER BY part, rank, name
            """),
        {"r": region},
    )
    offered = [(r.common_name, r.scientific_name) for r in rows]
    if not offered:
        raise ValueError(f"no species list for the region {region!r}")
    return offered


#: Each measured phone fish with a mask, its current measurement, and the
#: photo and mask to crop; ``owed`` when no prediction for that measurement
#: comes from this predictor, region and list.
_OWED = f"""
    SELECT c.id AS capture_id, c.mission_id, p.id AS phone_measurement_id,
           rgb.bucket, rgb.object_key AS rgb_key,
           p.mask_bucket, p.mask_key, p.mask_width, p.mask_height
    FROM current_phone_measurements p
    JOIN captures c ON c.tenant_id = p.tenant_id AND c.id = p.capture_id
    JOIN capture_objects rgb ON rgb.tenant_id = c.tenant_id
         AND rgb.capture_id = c.id AND rgb.role = 'rgb_jpeg'
    WHERE p.tenant_id = :t AND p.status = 'measured' AND p.mask_key IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM phone_species_predictions s
          WHERE s.tenant_id = p.tenant_id AND s.phone_measurement_id = p.id
            AND s.predictor_version = :sv AND s.species_region = :region
            AND s.species_list_version = {PHONE_SPECIES_LIST_VERSION})
"""


@dataclass(frozen=True)
class OwedSpeciesMission:
    mission_id: uuid.UUID
    number: int
    captures: int


@dataclass(frozen=True)
class OwedSpeciesCapture:
    capture_id: uuid.UUID
    phone_measurement_id: uuid.UUID
    bucket: str
    rgb_key: str
    mask_bucket: str
    mask_key: str
    mask_width: int
    mask_height: int


@dataclass(frozen=True)
class PhoneSpeciesResult:
    capture_id: uuid.UUID
    phone_measurement_id: uuid.UUID
    status: str
    predictor_version: int
    model_id: str
    predicted_choice: str | None
    top1_probability: float | None
    margin: float | None
    top5: list[dict] = field(default_factory=list)


def _params(tenant_id, region, predictor_version) -> dict:
    return {"t": tenant_id, "region": region, "sv": predictor_version}


async def owed_missions(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    region: str,
    *,
    predictor_version: int,
) -> list[OwedSpeciesMission]:
    """Each mission with phone fish owed a species, and how many."""
    rows = await conn.execute(
        text(f"""
            SELECT d.id, d.number, count(*) AS n
            FROM ({_OWED}) o JOIN missions d ON d.tenant_id = :t AND d.id = o.mission_id
            GROUP BY d.id, d.number ORDER BY d.number
            """),
        _params(tenant_id, region, predictor_version),
    )
    return [OwedSpeciesMission(r.id, r.number, r.n) for r in rows]


async def owed_captures(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    region: str,
    *,
    predictor_version: int,
    after: uuid.UUID | None = None,
    limit: int = 100,
) -> list[OwedSpeciesCapture]:
    """A page of one mission's phone fish owed a species, by capture id."""
    rows = await conn.execute(
        text(f"""
            SELECT * FROM ({_OWED}) o
            WHERE o.mission_id = :d
              AND (CAST(:after AS uuid) IS NULL OR o.capture_id > CAST(:after AS uuid))
            ORDER BY o.capture_id LIMIT :limit
            """),
        {**_params(tenant_id, region, predictor_version), "d": mission_id,
         "after": after, "limit": limit},
    )  # fmt: skip
    return [
        OwedSpeciesCapture(
            capture_id=r.capture_id, phone_measurement_id=r.phone_measurement_id,
            bucket=r.bucket, rgb_key=r.rgb_key, mask_bucket=r.mask_bucket,
            mask_key=r.mask_key, mask_width=r.mask_width, mask_height=r.mask_height,
        )  # fmt: skip
        for r in rows
    ]


async def record_phone_species(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    region: str,
    results: Sequence[PhoneSpeciesResult],
) -> int:
    """Append each result; how many were written."""
    offered = {name for _, name in await phone_species_list(conn, region)}
    for r in results:
        if r.status not in PHONE_SPECIES_STATUSES:
            raise ValueError(f"species status {r.status!r} is not a prediction")
        named = [r.predicted_choice] if r.predicted_choice else []
        named += [s["choice"] for s in r.top5]
        if foreign := sorted(
            c for c in named if choice_scientific_name(c) not in offered
        ):
            raise ValueError(f"{foreign} are not offered in {region!r}")
        current = (
            await conn.execute(
                text("""
                    SELECT id FROM current_phone_measurements
                    WHERE tenant_id = :t AND capture_id = :c
                    """),
                {"t": tenant_id, "c": r.capture_id},
            )
        ).scalar_one_or_none()
        if current is None or current != r.phone_measurement_id:
            raise ValueError(
                f"measurement {r.phone_measurement_id} is not the capture's current "
                f"server measurement ({r.capture_id})"
            )
        await conn.execute(
            text("""
                INSERT INTO phone_species_predictions (tenant_id, capture_id,
                    phone_measurement_id, status, predictor_version, model_id,
                    predicted_choice, top1_probability, margin, top5,
                    species_region, species_list_version)
                VALUES (:t, :c, :p, :status, :sv, :model, :choice, :top1, :margin,
                    CAST(:top5 AS jsonb), :region, :lv)
                """),
            {"t": tenant_id, "c": r.capture_id, "p": r.phone_measurement_id,
             "status": r.status, "sv": r.predictor_version, "model": r.model_id,
             "choice": r.predicted_choice, "top1": r.top1_probability,
             "margin": r.margin, "top5": json.dumps(r.top5), "region": region,
             "lv": PHONE_SPECIES_LIST_VERSION},
        )  # fmt: skip
    return len(results)


class PhoneSpeciesCatalog(ServicePrincipal):
    """The phone species' database side, as the orchestrator's service
    principal."""

    async def species_list(self, tenant_id, region):
        async with self._tenant(tenant_id) as conn:
            return await phone_species_list(conn, region)

    async def owed_missions(self, tenant_id, region, *, predictor_version):
        async with self._tenant(tenant_id) as conn:
            return await owed_missions(
                conn, tenant_id, region, predictor_version=predictor_version
            )

    async def owed_captures(
        self, tenant_id, mission_id, region, *, predictor_version, after=None,
        limit=100,
    ):  # fmt: skip
        async with self._tenant(tenant_id) as conn:
            return await owed_captures(
                conn, tenant_id, mission_id, region,
                predictor_version=predictor_version, after=after, limit=limit,
            )  # fmt: skip

    async def record(self, tenant_id, region, results):
        async with self._tenant(tenant_id) as conn:
            return await record_phone_species(conn, tenant_id, region, results)
