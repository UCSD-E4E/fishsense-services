"""Withdrawing the automatic lengths already on zoomed frames (the owner,
2026-10-25).

The zoom gate (migration 0070, ``focal_length_calibrated``) keeps a frame
shot zoomed in out of calibration and measurement from now on. When the
focal lengths were recorded, 152 automatic lengths already sat on such frames:
their depth and length assume intrinsics the frame was not shot at.

Automatic results are append-only, so a length is withdrawn by appending a
refusal (``zoomed``) that becomes the frame's current row, carrying the
length's provenance; the length stays in the history. The CLI
(``withdraw-zoomed-lengths``) lists them, and with ``--apply`` writes exactly
the listed ones -- a row no longer current, or a frame no longer zoomed, is
left alone, and the CLI writes nothing if that leaves a different count.

Run as the owner (it reads every tenant). Human lengths need no withdrawal:
none was on a zoomed frame (0 of 3,314), and the measurement view now keeps
them off one.
"""

import uuid
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "ZoomedLength",
    "withdraw_zoomed_automatic_lengths",
    "zoomed_automatic_lengths",
]

#: A current automatic length on a zoomed frame.
_ZOOMED = """
    FROM current_automatic_measurements m
    JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
    WHERE m.length_m IS NOT NULL
      AND NOT focal_length_calibrated(c.focal_length_mm)
"""


@dataclass(frozen=True)
class ZoomedLength:
    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    capture_id: uuid.UUID
    measurement_id: uuid.UUID
    length_m: float
    focal_length_mm: float


async def zoomed_automatic_lengths(conn: AsyncConnection) -> list[ZoomedLength]:
    """Every current automatic length on a zoomed frame, in every tenant."""
    rows = await conn.execute(text(f"""
            SELECT m.tenant_id, c.mission_id, m.capture_id, m.id, m.length_m,
                   c.focal_length_mm
            {_ZOOMED}
            ORDER BY m.tenant_id, c.mission_id, c.number
            """))
    return [ZoomedLength(*r) for r in rows]


async def withdraw_zoomed_automatic_lengths(
    conn: AsyncConnection, measurement_ids: Sequence[uuid.UUID]
) -> int:
    """Append a ``zoomed`` refusal after each listed length that is still
    current and still on a zoomed frame. How many were written."""
    rows = await conn.execute(
        text(f"""
            INSERT INTO automatic_measurements (
                tenant_id, producer, capture_id, automatic_head_tail_prediction_id,
                calibration_source, automatic_laser_calibration_id,
                laser_calibration_id, camera_calibration_id, length_m, depth_m,
                refusal, algorithm, algorithm_version, core_version)
            SELECT m.tenant_id, m.producer, m.capture_id,
                   m.automatic_head_tail_prediction_id, m.calibration_source,
                   m.automatic_laser_calibration_id, m.laser_calibration_id,
                   m.camera_calibration_id, NULL, NULL, 'zoomed', m.algorithm,
                   m.algorithm_version, m.core_version
            {_ZOOMED}
              AND m.id = ANY(CAST(:ids AS uuid[]))
            RETURNING id
            """),
        {"ids": list(measurement_ids)},
    )
    return len(rows.all())
