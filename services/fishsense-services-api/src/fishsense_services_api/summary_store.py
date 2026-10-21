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


_SUMMARY = """
    SELECT
      (SELECT count(*) FROM observations o WHERE o.tenant_id = :t) AS lengths,
      (SELECT count(*) FROM observations o
        WHERE o.tenant_id = :t AND o.fish_model IS NULL) AS wild_fish,
      (SELECT count(DISTINCT o.scientific_name) FROM observations o
        WHERE o.tenant_id = :t) AS species,
      (SELECT count(*) FROM missions m WHERE m.tenant_id = :t) AS missions,
      (SELECT count(*) FROM deployments d WHERE d.tenant_id = :t) AS deployments,
      (SELECT count(*) FROM captures c
        WHERE c.tenant_id = :t AND c.is_canonical) AS frames,
      (SELECT count(*) FROM missions m
        WHERE m.tenant_id = :t AND m.port = 'unknown'
          AND m.source = 'lite') AS unknown_ports,
      (SELECT max(c.created_at) FROM captures c WHERE c.tenant_id = :t)
        AS latest_activity
"""


async def tenant_summary(conn: AsyncConnection, tenant_id: uuid.UUID) -> TenantSummary:
    row = (await conn.execute(text(_SUMMARY), {"t": tenant_id})).one()
    return TenantSummary(**row._mapping)
