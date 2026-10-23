"""The SimCC head/tail keypoint replay's database side (owner, 2026-10-23):
read-only. The work is each canonical capture's current automatic head/tail
prediction that found a fish (``predicted``), with the SAM mask box the model
crops from and the render it was made on (version and camera: the box is in
that render's pixels). Nothing is written: the replay's results go to a file
for comparison with the SAM rule's before any switch."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.service_principal import ServicePrincipal

__all__ = [
    "HeadtailKeypointCatalog",
    "KeypointReplayFrame",
    "KeypointReplayMission",
    "keypoint_replay_frames",
    "keypoint_replay_missions",
]

_WORK = """
    SELECT p.id AS prediction_id, p.seq, p.capture_id, c.mission_id, p.mask_bbox,
           p.jpeg_render_version AS render_version,
           p.jpeg_camera_calibration_id AS camera_calibration_id
    FROM current_automatic_head_tail_predictions p
    JOIN captures c ON c.tenant_id = p.tenant_id AND c.id = p.capture_id
    WHERE p.tenant_id = :t AND c.is_canonical AND p.status = 'predicted'
      AND p.mask_bbox IS NOT NULL AND p.jpeg_render_version IS NOT NULL
    """


@dataclass(frozen=True)
class KeypointReplayMission:
    mission_id: uuid.UUID
    number: int
    frames: int


@dataclass(frozen=True)
class KeypointReplayFrame:
    prediction_id: uuid.UUID
    seq: int
    capture_id: uuid.UUID
    mask_bbox: list[int]
    render_version: int
    camera_calibration_id: uuid.UUID | None


async def keypoint_replay_missions(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> list[KeypointReplayMission]:
    """Each mission with work, and how many frames, by number."""
    rows = await conn.execute(
        text(f"""
            SELECT d.id, d.number, count(*) AS n
            FROM ({_WORK}) w JOIN missions d ON d.tenant_id = :t AND d.id = w.mission_id
            GROUP BY d.id, d.number ORDER BY d.number
            """),
        {"t": tenant_id},
    )
    return [KeypointReplayMission(r.id, r.number, r.n) for r in rows]


async def keypoint_replay_frames(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    *,
    after_seq: int = 0,
    limit: int = 200,
) -> list[KeypointReplayFrame]:
    """A page of one mission's work, in `seq` order."""
    rows = await conn.execute(
        text(f"""
            SELECT * FROM ({_WORK}) w WHERE w.mission_id = :d AND w.seq > :after
            ORDER BY w.seq LIMIT :limit
            """),
        {"t": tenant_id, "d": mission_id, "after": after_seq, "limit": limit},
    )
    return [
        KeypointReplayFrame(prediction_id=r.prediction_id, seq=r.seq,
                            capture_id=r.capture_id, mask_bbox=list(r.mask_bbox),
                            render_version=r.render_version,
                            camera_calibration_id=r.camera_calibration_id)  # fmt: skip
        for r in rows
    ]


class HeadtailKeypointCatalog(ServicePrincipal):
    """The replay's database side, as the orchestrator's service principal."""

    async def keypoint_replay_missions(self, tenant_id):
        async with self._tenant(tenant_id) as conn:
            return await keypoint_replay_missions(conn, tenant_id)

    async def keypoint_replay_frames(self, tenant_id, mission_id, *, after_seq=0,
                                     limit=200):  # fmt: skip
        async with self._tenant(tenant_id) as conn:
            return await keypoint_replay_frames(
                conn, tenant_id, mission_id, after_seq=after_seq, limit=limit
            )
