"""Withdrawing the automatic lengths of a pool trial's fish models (the owner,
2026-11-10).

The lab's automatic map showed 11 fish in Southern California: automatic
lengths on frames of mission 509 (Nathan's Pool, 2023-08-18), a pool trial
whose 162 labeled fish are all fish models. Nobody labeled those 11 frames, so
nothing said they were decoys.

Automatic results are append-only, so a length is withdrawn by appending a
refusal (``fish_model``, 0089) that becomes the frame's current row, carrying
the length's provenance; the length stays in the history. Only a frame nobody
measured is withdrawn: a frame a person labeled keeps its automatic length,
which against a fish model's known length is a control. The CLI
(``withdraw-fish-model-lengths --tenant --missions``) lists them, and with
``--apply`` writes exactly the listed ones in the same transaction; a row no
longer current, or outside the named missions, is left alone, and the CLI
writes nothing if that leaves a different count. Run as the owner, as
`zoom_gate`'s withdrawals are.
"""

import uuid
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "REFUSAL",
    "FishModelLength",
    "fish_model_automatic_lengths",
    "withdraw_fish_model_lengths",
]

REFUSAL = "fish_model"

_LENGTHS = """
    FROM current_automatic_measurements m
    JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
    JOIN missions d ON d.tenant_id = c.tenant_id AND d.id = c.mission_id
    WHERE m.tenant_id = :t AND d.number = ANY(CAST(:numbers AS integer[]))
      AND m.length_m IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM observations o
                      WHERE o.tenant_id = c.tenant_id AND o.capture_id = c.id)
"""


@dataclass(frozen=True)
class FishModelLength:
    mission: int
    capture_id: uuid.UUID
    measurement_id: uuid.UUID
    length_m: float


async def fish_model_automatic_lengths(
    conn: AsyncConnection, tenant_id: uuid.UUID, numbers: Sequence[int]
) -> list[FishModelLength]:
    """Every current automatic length in the tenant's missions ``numbers`` on
    a frame nobody measured."""
    rows = await conn.execute(
        text(f"""
            SELECT d.number, m.capture_id, m.id, m.length_m
            {_LENGTHS}
            ORDER BY d.number, c.captured_at, c.id
            """),
        {"t": tenant_id, "numbers": list(numbers)},
    )
    return [FishModelLength(*r) for r in rows]


async def withdraw_fish_model_lengths(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    numbers: Sequence[int],
    measurement_ids: Sequence[uuid.UUID],
) -> int:
    """Append a ``fish_model`` refusal after each listed length that is still
    one `fish_model_automatic_lengths` lists. How many were written."""
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
                   m.camera_calibration_id, NULL, NULL, :reason, m.algorithm,
                   m.algorithm_version, m.core_version
            {_LENGTHS}
              AND m.id = ANY(CAST(:ids AS uuid[]))
            RETURNING id
            """),
        {"t": tenant_id, "numbers": list(numbers), "ids": list(measurement_ids),
         "reason": REFUSAL},  # fmt: skip
    )
    return len(rows.all())
