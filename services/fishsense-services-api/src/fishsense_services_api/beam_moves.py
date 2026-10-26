"""When the laser moved mid-dive: the detector (owner, 2026-10-25).

While the laser holds its aim, a mission's laser dots lie on one image line
(the beam's projection). On 21 of the 63 V-Slate missions they lay on two
parallel lines, 15-200 px apart, each over its own stretch of the dive: the
beam's direction changed by ~0.3-4 deg -- a bumped or loose laser. Human
clicks lie on both lines in time order (250, 425, 437, 456), and the zoom
explains none of them (all at 4.5 mm). One calibration cannot serve both
aims, and the owner's rule is a partial run: the part of the dive before the
move is calibrated and measured, the rest is refused ("generally, we won't be
able to repair the second half").

The detector, per mission, over its wide automatic dots in time order: for
each split, the line most dots before it agree on and the line most after
agree on (`TOL_PX`, production's off-line tolerance), each side at least
`MIN_SIDE` dots; a move is the split where the two lines hold the most dots
more than one line through all (`gain`), when that gain is worth a second
line (`MIN_GAIN`, or `MIN_GAIN_SHARE` of the dots) and the lines are
near-parallel (`MAX_ANGLE_DEG`: an aim change; a crossing line is a false dot
or a reflection -- 360, 400, 455 at 40-56 deg). Lines closer than ~2
`TOL_PX` are one line to the fit, so they gain nothing (the corpus study's
separate 12 px floor never decided). The move is placed in the
window between the split's two dots: frames inside it belong to neither aim.

Corpus-wide (read-only, 2026-10-25): 258 missions with >= `MIN_DOTS` wide
automatic dots, 36 moves. The same detector on human dots, as a control: of
185 missions with both, it found no move the automatic dots missed, and
agreed on the 3 where human dots cover both sides (250, 425, 437); in the
other 14 the human dots cover one side only.

Not handled: a second move, a laser that wobbles (several lines interleaved
in time: 361), and a move the slates alone show. Those missions keep their
refusals.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "BeamMove",
    "DETECTOR_VERSION",
    "Dot",
    "MIN_DOTS",
    "MissionDots",
    "current_moves",
    "detect_beam_move",
    "mission_dots",
    "record_beam_move",
]

DETECTOR_VERSION = "1"
#: Each side of a split needs this many dots, so a mission needs twice it.
MIN_SIDE = 8
MIN_DOTS = 2 * MIN_SIDE
TOL_PX = 10.0
MIN_GAIN = 6
MIN_GAIN_SHARE = 0.15
MAX_ANGLE_DEG = 3.0
#: Line hypotheses tried per fit, from a seeded generator (deterministic).
_TRIES = 150
#: Two dots closer than this name no direction.
_MIN_PAIR_PX = 20.0


@dataclass(frozen=True)
class Dot:
    at: datetime
    x: float
    y: float


@dataclass(frozen=True)
class BeamMove:
    #: The last dot before the move and the first after: a frame captured
    #: after `moved_after` is not the first aim's.
    moved_after: datetime
    moved_before: datetime
    detector_version: str
    gain: int
    angle_deg: float
    apart_px: float
    dots_before: int
    dots_after: int


def _line(xy: np.ndarray) -> np.ndarray:
    m = xy.mean(axis=0)
    u = np.linalg.svd(xy - m)[2][0]
    n = np.array([-u[1], u[0]])
    return np.array([n[0], n[1], -n @ m])


def _distances(xy: np.ndarray, line: np.ndarray) -> np.ndarray:
    return np.abs(xy @ line[:2] + line[2]) / np.hypot(line[0], line[1])


def _best_line(xy: np.ndarray, rng: np.random.Generator):
    """``(inliers, line)``: the line most dots agree on, refitted through
    them; ``(None, None)`` when no pair names a direction."""
    best = None
    for _ in range(_TRIES):
        i, j = rng.choice(len(xy), 2, replace=False)
        if np.hypot(*(xy[j] - xy[i])) < _MIN_PAIR_PX:
            continue
        inliers = _distances(xy, _line(xy[[i, j]])) <= TOL_PX
        if best is None or inliers.sum() > best.sum():
            best = inliers
    if best is None or best.sum() < 2:
        return None, None
    line = _line(xy[best])
    return _distances(xy, line) <= TOL_PX, line


def detect_beam_move(dots: Sequence[Dot]) -> Optional[BeamMove]:
    """The mission's move, or None (no move, or too few dots to say)."""
    ordered = sorted(dots, key=lambda d: (d.at, d.x, d.y))
    # Not only redundant with the split's sides: the whole mission's line fit
    # below needs two dots (2026-10-25: removed as redundant, the first
    # production dry run crashed on a one-dot mission).
    if len(ordered) < MIN_DOTS:
        return None
    xy = np.array([[d.x, d.y] for d in ordered])
    rng = np.random.default_rng(0)
    whole, _ = _best_line(xy, rng)
    one = 0 if whole is None else int(whole.sum())
    best = None
    for k in range(MIN_SIDE, len(ordered) - MIN_SIDE + 1):
        if ordered[k].at == ordered[k - 1].at:
            continue
        a, la = _best_line(xy[:k], rng)
        b, lb = _best_line(xy[k:], rng)
        if la is None or lb is None:
            continue
        gain = int(a.sum() + b.sum()) - one
        if best is None or gain > best[0]:
            best = (gain, k, la, lb, b)
    if best is None:
        return None
    gain, k, la, lb, b = best
    angle = float(np.degrees(np.arccos(min(1.0, abs(la[:2] @ lb[:2])))))
    after_on = xy[k:][b]
    apart = float(_distances(after_on.mean(axis=0)[None], la)[0])
    if gain < max(MIN_GAIN, MIN_GAIN_SHARE * len(ordered)) or angle >= MAX_ANGLE_DEG:
        return None
    return BeamMove(
        moved_after=ordered[k - 1].at,
        moved_before=ordered[k].at,
        detector_version=DETECTOR_VERSION,
        gain=gain,
        angle_deg=round(angle, 3),
        apart_px=round(apart, 1),
        dots_before=k,
        dots_after=len(ordered) - k,
    )


# -- the database side: as the owner, every tenant (the CLI's detect-beam-moves) --


@dataclass(frozen=True)
class MissionDots:
    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    number: int
    dots: list[Dot]


async def mission_dots(conn: AsyncConnection) -> list[MissionDots]:
    """Every mission's automatic dots on canonical frames at the calibrated
    focal length (a zoomed frame's dot is on another line for another reason,
    0070), in mission order."""
    rows = await conn.execute(text("""
            SELECT c.tenant_id, c.mission_id, m.number, c.captured_at,
                   h.laser_x, h.laser_y
            FROM captures c
            JOIN missions m ON m.tenant_id = c.tenant_id AND m.id = c.mission_id
            JOIN current_automatic_head_tail_predictions h
              ON h.tenant_id = c.tenant_id AND h.capture_id = c.id
            WHERE c.is_canonical AND h.laser_x IS NOT NULL AND h.laser_y IS NOT NULL
              AND focal_length_calibrated(c.focal_length_mm)
            ORDER BY c.tenant_id, m.number, c.captured_at
            """))
    by: dict = defaultdict(list)
    for r in rows:
        by[(r.tenant_id, r.mission_id, r.number)].append(
            Dot(at=r.captured_at, x=float(r.laser_x), y=float(r.laser_y))
        )
    return [MissionDots(t, m, n, dots) for (t, m, n), dots in by.items()]


async def current_moves(conn: AsyncConnection) -> dict[uuid.UUID, tuple]:
    """Mission -> its current ``(moved_after, detector_version)``."""
    rows = await conn.execute(
        text(
            "SELECT mission_id, moved_after, detector_version FROM current_mission_beam_moves"
        )
    )
    return {r.mission_id: (r.moved_after, r.detector_version) for r in rows}


async def record_beam_move(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID, move: BeamMove
) -> None:
    await conn.execute(
        text("""
            INSERT INTO mission_beam_moves (
                tenant_id, mission_id, moved_after, moved_before, detector_version,
                gain, angle_deg, apart_px, dots_before, dots_after)
            VALUES (:t, :m, :after, :before, :version, :gain, :angle, :apart,
                    :n_before, :n_after)
            """),
        {"t": tenant_id, "m": mission_id, "after": move.moved_after,
         "before": move.moved_before, "version": move.detector_version,
         "gain": move.gain, "angle": move.angle_deg, "apart": move.apart_px,
         "n_before": move.dots_before, "n_after": move.dots_after},
    )  # fmt: skip
