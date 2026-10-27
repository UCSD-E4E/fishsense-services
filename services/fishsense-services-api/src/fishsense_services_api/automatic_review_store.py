"""Reviewing the automatic track: what it made of a frame and of a mission.

New in v2 (2026-10-21; the owner: "use this app to review the auto process").
Read-only. The automatic results stay in their own tables (0036): this reads
them, never writes them or turns them into anything else.

* A frame is its current prediction (``current_automatic_head_tail_predictions``)
  with its dot, head and tail, the render those pixels are on, its mask
  (0059), its current length and its species guess.
  - Its head and tail are SimCC's keypoints when it has them (head/tail v5,
    0074; the owner, 2026-10-26: "switch to this as the primary version"), so
    a reviewer sees and accepts the points its length is measured from. That
    holds for a frame sent to review (``keypoints_disagree``) too: on the
    held-out reef missions SimCC was the nearer of the two to the human
    clicks even there. A fish model's frame (no keypoints) and a refused box
    keep the SAM rule's. ``head_tail_source`` names which; the SAM rule's
    points come as ``sam_head``/``sam_tail`` either way.
  - The pixels are full-frame pixels of the render the frames stage segmented,
    named by ``jpeg_render_version`` and ``jpeg_camera_calibration_id``. That
    is not always the newest render: after a camera is recalibrated, the
    newer render is another image.
  - A length is only shown while it is current (``current_automatic_measurements``):
    a newer prediction, or a change of calibration, makes it stale.
* A mission is its run:
  - its frames by status, and how many the run hasn't reached;
  - its label-free calibration and what its lengths use now;
  - its lengths and refusals, and its failures;
  - how its lengths compare with the human lengths on the same frames, where
    the frame has exactly one human-measured fish. That is agreement with the
    human path, not error against a known length.

Runs in a tenant transaction, under RLS.
"""

from __future__ import annotations

import statistics
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.laser_colour_store import (
    dot_colour_columns,
    dot_colour_join,
)

__all__ = [
    "AutomaticCalibration",
    "AutomaticFailure",
    "AutomaticFrame",
    "MissionAutomatic",
    "automatic_frame",
    "mission_automatic",
]

Point = tuple[float, float]


def _point(x, y) -> Point | None:
    return None if x is None or y is None else (x, y)


@dataclass(frozen=True)
class AutomaticFrame:
    prediction_id: uuid.UUID
    #: ``predicted``, or why the run abstained (``no_laser_dot``, ...).
    status: str
    created_at: datetime
    #: Full-frame pixels of the render below.
    laser: Point | None
    laser_confidence: float | None
    #: The dot's color as the laser kernel read it, and how clearly (0072);
    #: None when it gave no reading, and on every row from before.
    laser_color: str | None
    laser_color_margin: float | None
    #: ``dot`` (read with the dot), ``render`` (backfilled off its render,
    #: 0076), or None when there is no reading.
    laser_color_source: str | None
    #: SimCC's points when the frame has them, else the SAM rule's.
    head: Point | None
    tail: Point | None
    #: ``simcc`` or ``sam_rule``; None when there are no points.
    head_tail_source: str | None
    #: The SAM rule's points (the mask's extremes), whichever is shown.
    sam_head: Point | None
    sam_tail: Point | None
    #: The frame's current keypoints: ``predicted``, ``keypoints_disagree``
    #: (sent to review), ``keypoints_refused``; None when it has none.
    keypoint_status: str | None
    #: |SimCC length / SAM-rule length - 1|.
    keypoint_disagreement: float | None
    keypoint_checkpoint: str | None
    mask_bbox: list[int] | None
    sam_score: float | None
    slate_probability: float | None
    predictor_version: int
    checkpoint: str | None
    core_version: str | None
    #: The render the pixels are on; None before renders existed (0049).
    #: The render's id: what a review names as the image its points are on.
    render_id: uuid.UUID | None
    render_bucket: str | None
    render_key: str | None
    render_width: int | None
    render_height: int | None
    #: The mask, a frame-sized PNG (0059); None before 2026-10-21.
    mask_bucket: str | None
    mask_key: str | None
    mask_width: int | None
    mask_height: int | None
    #: The current length, or the refusal; None when there is none now.
    length_m: float | None
    depth_m: float | None
    refusal: str | None
    calibration_source: str | None
    #: The length's calibration error, 1 SD: the length times its
    #: calibration's scale SD (0079); None where the calibration has none.
    length_sd_m: float | None
    measurement_algorithm: str | None
    measurement_algorithm_version: str | None
    species: str | None
    species_probability: float | None
    species_margin: float | None
    species_top5: list = field(default_factory=list)


async def automatic_frame(
    conn: AsyncConnection, tenant_id: uuid.UUID, capture_id: uuid.UUID
) -> AutomaticFrame | None:
    """What the automatic run made of the frame now; None if it never got to it."""
    row = (
        await conn.execute(
            text(f"""
                SELECT h.*, r.id AS render_id, r.bucket AS render_bucket,
                       r.key AS render_key,
                       r.width AS render_width, r.height AS render_height,
                       pm.bucket AS mask_bucket, pm.object_key AS mask_key,
                       pm.width AS mask_width, pm.height AS mask_height,
                       m.length_m, m.depth_m, m.refusal, m.calibration_source,
                       m.length_m * alc.scale_sd AS length_sd_m,
                       m.algorithm AS m_algorithm,
                       m.algorithm_version AS m_algorithm_version,
                       s.predicted_choice, s.top1_probability, s.margin, s.top5,
                       {dot_colour_columns("h")},
                       k.status AS k_status, k.head_x AS k_head_x,
                       k.head_y AS k_head_y, k.tail_x AS k_tail_x,
                       k.tail_y AS k_tail_y, k.disagreement AS k_disagreement,
                       k.checkpoint AS k_checkpoint
                FROM current_automatic_head_tail_predictions h
                {dot_colour_join("h")}
                LEFT JOIN LATERAL (
                    SELECT * FROM automatic_keypoint_predictions k
                    WHERE k.tenant_id = h.tenant_id
                      AND k.automatic_head_tail_prediction_id = h.id
                    ORDER BY k.seq DESC LIMIT 1
                ) k ON true
                LEFT JOIN LATERAL (
                    SELECT r.id, r.bucket, r.key, r.width, r.height FROM capture_renders r
                    WHERE r.tenant_id = h.tenant_id AND r.capture_id = h.capture_id
                      AND r.status = 'rendered'
                      AND r.render_version = h.jpeg_render_version
                      AND r.camera_calibration_id = h.jpeg_camera_calibration_id
                    ORDER BY r.seq DESC LIMIT 1
                ) r ON true
                LEFT JOIN prediction_masks pm
                  ON pm.tenant_id = h.tenant_id
                 AND pm.automatic_head_tail_prediction_id = h.id
                LEFT JOIN current_automatic_measurements m
                  ON m.tenant_id = h.tenant_id AND m.capture_id = h.capture_id
                 AND m.automatic_head_tail_prediction_id = h.id
                LEFT JOIN automatic_laser_calibrations alc
                  ON alc.tenant_id = m.tenant_id
                 AND alc.id = m.automatic_laser_calibration_id
                LEFT JOIN current_automatic_species_predictions s
                  ON s.tenant_id = h.tenant_id AND s.capture_id = h.capture_id
                 AND s.automatic_head_tail_prediction_id = h.id
                 AND s.status = 'predicted'
                WHERE h.tenant_id = :t AND h.capture_id = :c
                """),
            {"t": tenant_id, "c": capture_id},
        )
    ).first()
    if row is None:
        return None
    sam_head, sam_tail = _point(row.head_x, row.head_y), _point(row.tail_x, row.tail_y)
    simcc = row.k_head_x is not None
    head = _point(row.k_head_x, row.k_head_y) if simcc else sam_head
    tail = _point(row.k_tail_x, row.k_tail_y) if simcc else sam_tail
    return AutomaticFrame(
        prediction_id=row.id,
        status=row.status,
        created_at=row.created_at,
        laser=_point(row.laser_x, row.laser_y),
        laser_confidence=row.laser_confidence,
        laser_color=row.laser_color,
        laser_color_margin=row.laser_color_margin,
        laser_color_source=row.laser_color_source,
        head=head,
        tail=tail,
        head_tail_source=(None if head is None else "simcc" if simcc else "sam_rule"),
        sam_head=sam_head,
        sam_tail=sam_tail,
        keypoint_status=row.k_status,
        keypoint_disagreement=row.k_disagreement,
        keypoint_checkpoint=row.k_checkpoint,
        mask_bbox=None if row.mask_bbox is None else list(row.mask_bbox),
        sam_score=row.sam_score,
        slate_probability=row.slate_probability,
        predictor_version=row.predictor_version,
        checkpoint=row.checkpoint,
        core_version=row.core_version,
        render_id=row.render_id,
        render_bucket=row.render_bucket,
        render_key=row.render_key,
        render_width=row.render_width,
        render_height=row.render_height,
        mask_bucket=row.mask_bucket,
        mask_key=row.mask_key,
        mask_width=row.mask_width,
        mask_height=row.mask_height,
        length_m=row.length_m,
        depth_m=row.depth_m,
        refusal=row.refusal,
        calibration_source=row.calibration_source,
        length_sd_m=row.length_sd_m,
        measurement_algorithm=row.m_algorithm,
        measurement_algorithm_version=row.m_algorithm_version,
        species=row.predicted_choice,
        species_probability=row.top1_probability,
        species_margin=row.margin,
        species_top5=list(row.top5 or []),
    )


@dataclass(frozen=True)
class AutomaticCalibration:
    outcome: str
    refusal_reason: str | None
    algorithm_version: str
    frames_used: int | None
    pair_count: int | None
    size_ratio: float | None
    se_px: float | None
    created_at: datetime
    #: ``size_constancy`` or ``slate_pose`` (0078).
    method: str = "size_constancy"
    #: The lengths' scale SD on it (0079); None where none was measured.
    scale_sd: float | None = None


@dataclass(frozen=True)
class AutomaticFailure:
    step: str
    error: str
    created_at: datetime


@dataclass(frozen=True)
class MissionAutomatic:
    #: The mission's canonical frames.
    frames: int
    #: Their current predictions, by status.
    by_status: dict[str, int]
    #: Frames the run has not reached.
    unprocessed: int
    #: The mission's own label-free fit, if it has one.
    calibration: AutomaticCalibration | None
    #: What its lengths use now (``label_free`` or ``stored``); None: nothing.
    lengths_use: str | None
    #: Current lengths, and current refusals by reason.
    lengths: int
    refusals: dict[str, int]
    failures: list[AutomaticFailure]
    #: Why an operator excluded it from the backlog, if they did.
    excluded: str | None
    #: Frames with both an automatic and exactly one human length.
    compared: int
    #: (automatic - human) / human, in percent: the median, and the mean of
    #: its absolute value.
    median_error_pct: float | None
    mean_abs_error_pct: float | None
    within_5_pct: int
    within_10_pct: int


async def mission_automatic(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID
) -> MissionAutomatic:
    params = {"t": tenant_id, "m": mission_id}
    frames = (
        await conn.execute(
            text("""
                SELECT count(*) FROM captures
                WHERE tenant_id = :t AND mission_id = :m AND is_canonical
                """),
            params,
        )
    ).scalar_one()
    by_status = Counter(
        dict(
            (
                await conn.execute(
                    text("""
                        SELECT h.status, count(*) FROM current_automatic_head_tail_predictions h
                        JOIN captures c ON c.tenant_id = h.tenant_id AND c.id = h.capture_id
                        WHERE h.tenant_id = :t AND c.mission_id = :m AND c.is_canonical
                        GROUP BY h.status
                        """),
                    params,
                )
            ).all()
        )
    )
    calibration = (
        await conn.execute(
            text("""
                SELECT a.outcome, a.refusal_reason, a.algorithm_version,
                       a.frames_used, a.pair_count, a.size_ratio, a.se_px,
                       a.created_at, a.method,
                       (SELECT x.scale_sd FROM automatic_laser_calibrations x
                        WHERE x.tenant_id = a.tenant_id AND x.id = a.id) AS scale_sd
                FROM current_automatic_laser_calibrations a
                WHERE a.tenant_id = :t AND a.mission_id = :m
                -- One per method (0078): the one the lengths would use.
                ORDER BY a.outcome = 'accepted' DESC, a.method = 'slate_pose' DESC
                LIMIT 1
                """),
            params,
        )
    ).first()
    lengths_use = (
        await conn.execute(
            text("""
                SELECT calibration_source FROM automatic_measurement_calibrations
                WHERE tenant_id = :t AND mission_id = :m
                """),
            params,
        )
    ).scalar_one_or_none()
    measured = (
        await conn.execute(
            text("""
                SELECT m.capture_id, m.length_m, m.refusal,
                       (SELECT CASE WHEN count(*) = 1 THEN max(o.length_m) END
                        FROM observations o
                        WHERE o.tenant_id = m.tenant_id AND o.capture_id = m.capture_id
                          AND o.method = 'laser') AS human_m
                FROM current_automatic_measurements m
                JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
                WHERE m.tenant_id = :t AND c.mission_id = :m
                """),
            params,
        )
    ).all()
    failures = [
        AutomaticFailure(r.step, r.error, r.created_at)
        for r in await conn.execute(
            text("""
                SELECT step, error, created_at FROM automatic_results_failures
                WHERE tenant_id = :t AND mission_id = :m ORDER BY seq
                """),
            params,
        )
    ]
    excluded = (
        await conn.execute(
            text("""
                SELECT reason FROM automatic_results_exclusions
                WHERE tenant_id = :t AND mission_id = :m
                """),
            params,
        )
    ).scalar_one_or_none()
    errors = [
        (r.length_m - r.human_m) / r.human_m * 100
        for r in measured
        if r.length_m is not None and r.human_m
    ]
    return MissionAutomatic(
        frames=frames,
        by_status=dict(sorted(by_status.items())),
        unprocessed=frames - sum(by_status.values()),
        calibration=(
            None
            if calibration is None
            else AutomaticCalibration(**calibration._mapping)
        ),
        lengths_use=lengths_use,
        lengths=sum(1 for r in measured if r.length_m is not None),
        refusals=dict(Counter(r.refusal for r in measured if r.refusal)),
        failures=failures,
        excluded=excluded,
        compared=len(errors),
        median_error_pct=statistics.median(errors) if errors else None,
        mean_abs_error_pct=statistics.fmean(abs(e) for e in errors) if errors else None,
        within_5_pct=sum(1 for e in errors if abs(e) <= 5),
        within_10_pct=sum(1 for e in errors if abs(e) <= 10),
    )
