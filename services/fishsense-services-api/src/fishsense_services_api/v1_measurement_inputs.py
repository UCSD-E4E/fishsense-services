"""Link v1's measurements to the inputs v1 did not record (2026-10-22).

v1 recorded which calibration a measurement used and nothing else
(`v1_migration._results`), so a migrated fish's evidence -- its depth, laser
dot, head and tail -- showed nothing: `observation_store.observation` finds
them through the measurement's links. Both halves are recoverable:

- **The depth and dot, exactly.** v1 kept one depth per image under each
  calibration, and that depth names the laser label it was made from. The
  measurement's depth is the one v1 depth on its capture under its
  calibration; a capture with none, or more than one, is skipped.
- **The head/tail label, by reproducing the length.** v1 measured head to
  tail on the plane at the laser depth (`automatic_validation.length`, the
  same formula). The label is the one whose length, from that depth and the
  device's pinhole camera, matches v1's within ``TOLERANCE``. None matching,
  or more than one, is skipped.

On production (read-only, 2026-10-22) every one of the 3,125 current v1 laser
fish had exactly one v1 depth, and its best head/tail label reproduced v1's
length within 2.7e-6 (median 1.6e-7). On the 9 captures with two labels, the
other missed by more than 1e-4, so none was ambiguous.

`current_measurements` drops a measurement whose linked label is superseded,
so a link can take a fish out of the current results; the plan flags each
link that would (``leaves_current``), for the review to see.

A plan is read-only, and its fingerprint covers everything it would write.
Applying takes the fingerprint of the plan that was reviewed: it re-plans
under row locks and writes nothing if the two differ, and each write is
guarded on the exact old values, so a row that moved in between rolls the
whole apply back. `measurements` grants the app role no UPDATE, so the apply
runs as the schema owner.
"""

import math
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.automatic_validation import length
from fishsense_services_api.mission_backfill import PlanChanged, plan_fingerprint

__all__ = [
    "Link",
    "PlanChanged",
    "Skip",
    "TOLERANCE",
    "V1InputsPlan",
    "apply_v1_inputs",
    "plan_v1_inputs",
]

#: A recomputed length matches v1's within this, relatively. v1's own float
#: arithmetic stays under 3e-6; a different label misses by more than 1e-4.
TOLERANCE = 1e-4


@dataclass(frozen=True)
class Link:
    """The inputs one v1 measurement used."""

    measurement_id: uuid.UUID
    v1_id: int
    length_m: float
    laser_depth_id: uuid.UUID
    laser_label_id: uuid.UUID
    head_tail_label_id: uuid.UUID
    relative_error: float
    #: Linking it takes the fish out of `current_measurements` (a linked label
    #: is superseded).
    leaves_current: bool


@dataclass(frozen=True)
class Skip:
    measurement_id: uuid.UUID
    v1_id: int
    reason: str


@dataclass(frozen=True)
class V1InputsPlan:
    links: tuple[Link, ...]
    skipped: tuple[Skip, ...]
    fingerprint: str


async def plan_v1_inputs(
    conn: AsyncConnection, tenant_id: uuid.UUID, *, _lock: bool = False
) -> V1InputsPlan:
    """What the backfill would link in the tenant. Writes nothing."""
    params = {"t": tenant_id}
    rows = (
        await conn.execute(
            text("""
                SELECT m.id, m.v1_id, m.capture_id, m.laser_calibration_id,
                       m.length_m, cc.camera_matrix
                FROM measurements m
                JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
                JOIN missions d ON d.tenant_id = c.tenant_id AND d.id = c.mission_id
                LEFT JOIN current_camera_calibrations cc
                  ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
                 AND cc.camera_model = 'pinhole'
                WHERE m.tenant_id = :t AND m.v1_id IS NOT NULL
                  AND m.source = 'server' AND m.length_m IS NOT NULL
                  AND m.laser_depth_id IS NULL AND m.laser_label_id IS NULL
                  AND m.head_tail_label_id IS NULL
                ORDER BY m.v1_id
                """ + (" FOR UPDATE OF m" if _lock else "")),
            params,
        )
    ).all()
    captures = list({r.capture_id for r in rows})
    lock = " FOR SHARE" if _lock else ""
    depths = defaultdict(list)
    for d in await conn.execute(
        text(
            "SELECT id, capture_id, laser_calibration_id, laser_label_id, depth_m "
            "FROM laser_depths WHERE tenant_id = :t AND v1_id IS NOT NULL "
            "AND capture_id = ANY(:c)" + lock
        ),
        {**params, "c": captures},
    ):
        depths[(d.capture_id, d.laser_calibration_id)].append(d)
    head_tails = defaultdict(list)
    for h in await conn.execute(
        text(
            "SELECT id, capture_id, superseded, head_x, head_y, tail_x, tail_y "
            "FROM head_tail_labels WHERE tenant_id = :t AND capture_id = ANY(:c) "
            "AND head_x IS NOT NULL AND head_y IS NOT NULL "
            "AND tail_x IS NOT NULL AND tail_y IS NOT NULL ORDER BY id" + lock
        ),
        {**params, "c": captures},
    ):
        head_tails[h.capture_id].append(h)
    superseded_lasers = set(
        (
            await conn.execute(
                text(
                    "SELECT id FROM laser_labels WHERE tenant_id = :t "
                    "AND capture_id = ANY(:c) AND superseded"
                ),
                {**params, "c": captures},
            )
        ).scalars()
    )
    current = set(
        (
            await conn.execute(
                text(
                    "SELECT id FROM current_measurements "
                    "WHERE tenant_id = :t AND v1_id IS NOT NULL"
                ),
                params,
            )
        ).scalars()
    )

    links: list[Link] = []
    skipped: list[Skip] = []
    for row in rows:

        def skip(reason: str) -> None:
            skipped.append(Skip(row.id, row.v1_id, reason))

        if row.camera_matrix is None:
            skip("its device has no pinhole camera calibration")
            continue
        found = depths[(row.capture_id, row.laser_calibration_id)]
        if len(found) != 1:
            skip(f"{len(found)} v1 depths under its calibration, not one")
            continue
        [d] = found
        if d.laser_label_id is None:
            skip("its v1 depth names no laser label")
            continue
        errors = [
            (abs(length(row.camera_matrix, d.depth_m, h.head_x, h.head_y,
                        h.tail_x, h.tail_y) / row.length_m - 1), h)
            for h in head_tails[row.capture_id]
        ]  # fmt: skip
        matches = [(e, h) for e, h in errors if e < TOLERANCE]
        if not matches:
            closest = min((e for e, _ in errors), default=math.inf)
            skip(
                f"no head/tail label reproduces its length (of {len(errors)}, "
                f"the closest is off by {closest:.2e})"
            )
            continue
        if len(matches) > 1:
            skip(f"{len(matches)} head/tail labels reproduce its length")
            continue
        [(error, h)] = matches
        links.append(
            Link(
                measurement_id=row.id,
                v1_id=row.v1_id,
                length_m=row.length_m,
                laser_depth_id=d.id,
                laser_label_id=d.laser_label_id,
                head_tail_label_id=h.id,
                relative_error=error,
                leaves_current=row.id in current
                and (h.superseded or d.laser_label_id in superseded_lasers),
            )
        )
    return V1InputsPlan(
        links=tuple(links),
        skipped=tuple(skipped),
        fingerprint=plan_fingerprint(
            {
                "links": [asdict(link) for link in links],
                "skipped": [asdict(s) for s in skipped],
            }
        ),
    )


async def apply_v1_inputs(
    conn: AsyncConnection, tenant_id: uuid.UUID, *, expect: str
) -> int:
    """Write the reviewed plan, all or nothing, inside the caller's
    transaction (as the schema owner). Returns the measurements linked."""
    plan = await plan_v1_inputs(conn, tenant_id, _lock=True)
    if plan.fingerprint != expect:
        raise PlanChanged(
            f"the plan is now {plan.fingerprint}, not the reviewed {expect}: "
            "review it again"
        )
    for link in plan.links:
        written = await conn.execute(
            text("""
                UPDATE measurements
                SET laser_depth_id = :depth, laser_label_id = :laser,
                    head_tail_label_id = :head_tail
                WHERE tenant_id = :t AND id = :m AND v1_id = :v1
                  AND length_m = :length
                  AND laser_depth_id IS NULL AND laser_label_id IS NULL
                  AND head_tail_label_id IS NULL
                """),
            {
                "t": tenant_id,
                "m": link.measurement_id,
                "v1": link.v1_id,
                "length": link.length_m,
                "depth": link.laser_depth_id,
                "laser": link.laser_label_id,
                "head_tail": link.head_tail_label_id,
            },
        )
        if written.rowcount != 1:
            raise PlanChanged(f"measurement {link.v1_id} changed while being written")
    return len(plan.links)
