"""A tenant's numbers for its Today page (docs/design/README.md, Home pages),
tenant-scoped.

* **Lengths** are the tenant's observations (one current length per fish in a
  frame, 0045's view), and **wild fish** those of a species rather than a fish
  model: a model is a decoy of known length, measured in pool trials.
* **Frames** are canonical captures. The same frames filed in several NAS dumps
  are copies (``captures.is_canonical`` marks one), so a frame counts once.
* **Unknown ports** are missions whose camera port nobody has stated or ruled:
  the stages refuse them (2b-2), so a person needs to look.
* **Latest activity** is the newest capture's arrival (an ingest or a sync).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = ["TenantSummary", "tenant_summary"]


@dataclass(frozen=True)
class TenantSummary:
    lengths: int
    wild_fish: int
    species: int
    missions: int
    deployments: int
    frames: int
    unknown_ports: int
    latest_activity: datetime | None


#: One pass over `observations` and one over `captures`: each pass over the
#: view rebuilds every current measurement in the tenant (0.8-1.7 s each on
#: production, 2026-10-21, against 0.9 s for all three counts at once).
_SUMMARY = """
    SELECT o.lengths, o.wild_fish, o.species, c.frames, c.latest_activity,
      (SELECT count(*) FROM missions m WHERE m.tenant_id = :t) AS missions,
      (SELECT count(*) FROM deployments d WHERE d.tenant_id = :t) AS deployments,
      (SELECT count(*) FROM missions m
        WHERE m.tenant_id = :t AND m.port = 'unknown'
          AND m.source = 'lite') AS unknown_ports
    FROM (
      SELECT count(*) AS lengths,
             count(*) FILTER (WHERE fish_model IS NULL) AS wild_fish,
             count(DISTINCT scientific_name) AS species
      FROM observations WHERE tenant_id = :t
    ) o, (
      SELECT count(*) FILTER (WHERE is_canonical) AS frames,
             max(created_at) AS latest_activity
      FROM captures WHERE tenant_id = :t
    ) c
"""


async def tenant_summary(conn: AsyncConnection, tenant_id: uuid.UUID) -> TenantSummary:
    row = (await conn.execute(text(_SUMMARY), {"t": tenant_id})).one()
    return TenantSummary(**row._mapping)
