"""The checked borrow: an aim with no calibration takes a neighbor's beam when
it runs along the aim's own dot line and reads the aim's own posed slates at
their depth (0082; the owner, 2026-10-29).

Mission 383 (FiveSeas, FSL03, 2024-06-26) moved its laser at 07:01; its
hogfish is at 07:14. Its after-move slate-pose fit was refused (2026-10-29,
v0.73.0: two posed frames that disagree with the mission). Mission 386, the
same camera the same day, has an accepted slate-pose fit; its beam runs 3-9
px off 383's after-move dot line and 30-38 px off its before-move one, and
reads +1.1% at 0.75 m to +3.9% at 2 m against 383's human calibration
(fitted on the after-move slates). 374's beam, the same day too, runs 13-19
px off it and reads 5-7% away at 1.5-2 m: a different aim.

The rule, per aim no calibration serves (its own fits, and for aim 0 its
link's, come first, in ``automatic_measurement_calibrations``):

* the aim's dot line: the line most of its automatic dots agree on
  (`beam_moves`' fit and tolerance), held by at least `MIN_LINE_DOTS`, with
  no second line, through the dots off it, `RIVAL_SHARE` as well held (a
  reflection, wrong dots: 77's) -- else ``aim_line_unclear``;
* the lenders: the current accepted slate-pose fits, with their error bar at
  each range, of another mission on its device that started within a day,
  fitted under its current pinhole camera. Never a borrow (no chains), never
  size constancy (no bar to widen). With none there is nothing to try: no
  row, and the aim is owed a borrow again only once one is current;
* a lender's beam, projected through that camera at `BORROW_RANGES_M`, the
  fish's ranges, must lie within `beam_moves.TOL_PX` of the line at each --
  else ``no_matching_beam``. The closest beam is the candidate
  (`automatic_borrow_candidate`);
* the check (the processor's `automatic_calibration.borrow`): the aim's own
  slate frames, posed on the GPU, read through the candidate's beam; the
  median over slate holds of its depth over the pose's, less one, within 5%
  -- else ``slates_disagree`` (or ``too_few_posed``, or no template or slate
  frame to pose). Its verdict is written (`persist_automatic_borrow`): an
  accepted borrow takes the lender's beam, and the lender's error bar at
  each range widened by the check, ``hypot(lender's SD, |ratio|)``; it
  serves only while the lender's row is the lender's current one.

Why the check. The line cannot see where along it a range falls: two beams
through one image line lie in one plane through the camera and may differ
within it, in scale. On 33 missions with their own slate-pose fit hidden
(prod, 2026-10-29), the line-checked borrow read a median 1.2-3.2% from the
own fit at 0.75-2 m, but 5 were 8-19% off at 2 m (470<-472, 475<-478,
284<-290, 408<-414, 461<-457); neither the line offset (Spearman 0.05) nor
the time apart (0.19) predicted them. The 5% check kept 24, within 7.8% at 2
m, and dropped all five. Of the 24, the 17 with a lender bar were within the
widened bar 82-100% of the time by range (0.5-5 m), and all within two;
within the lender's bar alone, 71-82% at 1.5 m and beyond. Against human
calibrations (n=4) the line-checked borrow read 0.1-3.8% off at 0.75-2 m.

What it cannot see: the check measures the beam against poses, not against
a known length, so a pose's own error is in it; and slates at a few ranges
say little about the beam beyond them.

The unchecked borrow (0083; the owner, 2026-10-30). An aim with no slates to
check the candidate on (`UNCHECKED`: no template, no slate frame, too few
posed points) takes it on the line alone, with a wider bar: the lender's,
widened by `LINE_ONLY_SD_PER_M` times the range. On 2026-10-30 that was 33 of
the 88 aims tried, every one within the line's 10 px. The bar is the same
control's: the 23 line-checked borrows with a lender bar, unchecked, read
1.5% per meter from the own fit (median, at every range -- the shift along
the line the line cannot see grows with range), p90 6-8% per meter. 4% per
meter, with both fits' bars, held 87% within one bar at each range and
91-100% within two (fitted on those 23, not held out: at 1 m, 4%; 2 m, 8%; 5
m, 20%, before the lender's). The three worst (475<-478, 284<-290,
408<-414) read 9% short at 1 m, 34% at 5 m: within 2.3 bars.

What it cannot see beyond the checked borrow's: the control's missions all
had slates (their own fits are the answer), the aims it serves have none;
whether they differ otherwise, say in how far the beam was moved between
dives, is unmeasured.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api import beam_moves
from fishsense_services_api.automatic_results_store import (
    AUTOMATIC_CALIBRATION_CAPTURES,
    BORROW_VERSION,
    AutomaticCalibrationRow,
    InvalidAutomaticResults,
    _BORROW_WORK,
    _aims,
    _borrow_work,
    _lenders,
    _pinhole_camera,
    persist_automatic_calibration,
)

__all__ = [
    "BORROW_RANGES_M",
    "BORROW_VERSION",
    "BORROW_WORK",
    "BorrowCandidate",
    "BorrowCheck",
    "LINE_ONLY_SD_PER_M",
    "MIN_LINE_DOTS",
    "RIVAL_SHARE",
    "UNCHECKED",
    "automatic_borrow_candidate",
    "beam_line_px",
    "dot_line",
    "persist_automatic_borrow",
]

#: Dots the aim's line must hold: a side of a move (`beam_moves.MIN_SIDE`);
#: 383's after-move aim holds 16 of its 26.
MIN_LINE_DOTS = beam_moves.MIN_SIDE
#: A second line this share as well held makes the aim's unclear (383: 6
#: dots against 16).
RIVAL_SHARE = 0.5
#: Where a lender's beam must lie along the line: the fish's ranges, meters.
BORROW_RANGES_M = (0.75, 1.0, 1.5, 2.0, 3.0)
#: The borrow's work term, over mission `d` (the cohort's).
BORROW_WORK = _BORROW_WORK
#: The check's refusals that are no check at all: nothing to pose (0083).
UNCHECKED = frozenset({"no_slate_template", "no_slate_frames", "too_few_posed"})
#: An unchecked borrow's bar beyond the lender's, relative, per meter of
#: range (see the module).
LINE_ONLY_SD_PER_M = 0.04
#: The fish's ranges, over which `scale_sd` is the widest of the bar (the
#: slate pose's, `pose._FISH_RANGES_M`).
_FISH_RANGES_M = (0.75, 3.0)


@dataclass(frozen=True)
class BorrowCandidate:
    """The beam an aim would borrow: the lender's row and its beam, and how
    far that beam runs off the aim's dot line."""

    lender_id: uuid.UUID
    laser_position: list[float]
    laser_axis: list[float]
    line_px: float


@dataclass(frozen=True)
class BorrowCheck:
    """The check's verdict on a candidate (the processor's report): the
    median over holds of beam depth / pose depth - 1, on how many holds, and
    the posed frames it read."""

    outcome: str
    refusal_reason: str | None = None
    depth_ratio: float | None = None
    holds: int = 0
    capture_ids: list[uuid.UUID] = field(default_factory=list)


def dot_line(dots) -> np.ndarray | None:
    """The aim's dot line ``(a, b, c)``, or None when its dots name no clear
    one (`MIN_LINE_DOTS`, `RIVAL_SHARE`)."""
    xy = np.asarray(dots, float).reshape(-1, 2)
    if len(xy) < MIN_LINE_DOTS:
        return None
    rng = np.random.default_rng(0)
    on, line = beam_moves._best_line(xy, rng)
    if line is None or on.sum() < MIN_LINE_DOTS:
        return None
    rest = xy[~on]
    if len(rest) >= 2:
        rival, _ = beam_moves._best_line(rest, rng)
        if rival is not None and rival.sum() >= RIVAL_SHARE * on.sum():
            return None
    return line


def beam_line_px(camera_matrix, position, axis, line) -> float:
    """How far off `line` the beam from `position` along `axis` runs through
    `camera_matrix` at the farthest of `BORROW_RANGES_M`, pixels."""
    k = np.asarray(camera_matrix, float)
    o, a = np.asarray(position, float), np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    pixels = []
    for z in BORROW_RANGES_M:
        p = k @ (o + ((z - o[2]) / a[2]) * a)
        pixels.append(p[:2] / p[2])
    return float(beam_moves._distances(np.array(pixels), line).max())


async def _owed(conn, tenant_id, mission_id, aim: int) -> bool:
    return (
        await conn.execute(
            text(f"""
                SELECT {_borrow_work(_aims("x.aim = CAST(:aim AS smallint)"))}
                FROM missions d WHERE d.tenant_id = :t AND d.id = :d
                """),
            {"t": tenant_id, "d": mission_id, "aim": aim},
        )
    ).scalar_one()


async def _lender_rows(conn, tenant_id, mission_id):
    return (
        await conn.execute(
            text(f"""
                SELECT l.id, l.laser_position, l.laser_axis, x.scale_sd,
                       x.scale_sd_by_range
                FROM missions d
                CROSS JOIN LATERAL {_lenders("d")}
                JOIN automatic_laser_calibrations x
                  ON x.tenant_id = l.tenant_id AND x.id = l.id
                WHERE d.tenant_id = :t AND d.id = :d
                ORDER BY l.created_at, l.id
                """),
            {"t": tenant_id, "d": mission_id},
        )
    ).all()


def _refused(reason: str, aim: int, camera_id, **extra) -> AutomaticCalibrationRow:
    return AutomaticCalibrationRow(
        outcome="refused", algorithm_version=BORROW_VERSION, refusal_reason=reason,
        camera_calibration_id=camera_id, method="borrowed", aim=aim, **extra,
    )  # fmt: skip


async def automatic_borrow_candidate(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID, *, aim: int
) -> BorrowCandidate | None:
    """The beam the mission's `aim` would borrow, to be checked on its posed
    slates; nothing is written for it. None when no borrow is owed (a
    calibration serves the aim, or no lender is current that it has not
    tried at this version), or when the line refuses -- that refusal is
    written (``aim_line_unclear``, ``no_matching_beam``)."""
    if not await _owed(conn, tenant_id, mission_id, aim):
        return None
    camera = await _pinhole_camera(conn, tenant_id, mission_id)
    dots = [
        (r.laser_x, r.laser_y)
        for r in await conn.execute(
            text(AUTOMATIC_CALIBRATION_CAPTURES),
            {"t": tenant_id, "d": mission_id, "aim": aim},
        )
    ]
    line = dot_line(dots)
    if line is None:
        row = _refused("aim_line_unclear", aim, camera.id, frames_used=len(dots))
        await persist_automatic_calibration(conn, tenant_id, mission_id, row)
        return None
    off, lender = min(
        ((beam_line_px(camera.camera_matrix, r.laser_position, r.laser_axis, line), r)
         for r in await _lender_rows(conn, tenant_id, mission_id)),
        key=lambda pair: pair[0],
    )  # fmt: skip
    if off > beam_moves.TOL_PX:
        row = _refused("no_matching_beam", aim, camera.id, frames_used=len(dots),
                       borrowed_from=lender.id, borrow_line_px=round(off, 2))  # fmt: skip
        await persist_automatic_calibration(conn, tenant_id, mission_id, row)
        return None
    return BorrowCandidate(
        lender_id=lender.id,
        laser_position=[float(v) for v in lender.laser_position],
        laser_axis=[float(v) for v in lender.laser_axis],
        line_px=float(off),
    )


async def persist_automatic_borrow(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    candidate: BorrowCandidate,
    check: BorrowCheck,
    *,
    aim: int,
) -> uuid.UUID:
    """Write the check's verdict on `candidate` as the aim's borrow; its id.
    The candidate must still be one of the aim's lenders; an accepted borrow
    takes its beam, and its error bar widened by the check -- or, with no
    check to make (`UNCHECKED`), by `LINE_ONLY_SD_PER_M`."""
    lender = next(
        (r for r in await _lender_rows(conn, tenant_id, mission_id)
         if r.id == candidate.lender_id),
        None,
    )  # fmt: skip
    if lender is None:
        raise InvalidAutomaticResults(
            f"lender {candidate.lender_id} is not a current lender of {mission_id}"
        )
    camera = await _pinhole_camera(conn, tenant_id, mission_id)
    found = {
        "borrowed_from": lender.id, "borrow_line_px": round(candidate.line_px, 2),
        "borrow_depth_ratio": check.depth_ratio,
        "borrow_holds": check.holds if check.depth_ratio is not None else None,
        "capture_ids": list(check.capture_ids), "frames_used": len(check.capture_ids),
    }  # fmt: skip
    unchecked = check.outcome != "accepted" and check.refusal_reason in UNCHECKED
    if check.outcome != "accepted" and not unchecked:
        row = _refused(check.refusal_reason or "slates_disagree", aim, camera.id,
                       **found)  # fmt: skip
        return await persist_automatic_calibration(conn, tenant_id, mission_id, row)
    if unchecked:
        by_range = [[r, math.hypot(sd, LINE_ONLY_SD_PER_M * r)]
                    for r, sd in lender.scale_sd_by_range]  # fmt: skip
        lo, hi = _FISH_RANGES_M
        scale_sd = max(sd for r, sd in by_range if lo <= r <= hi)
    else:
        widen = abs(check.depth_ratio)
        by_range = [[r, math.hypot(sd, widen)] for r, sd in lender.scale_sd_by_range]
        scale_sd = math.hypot(lender.scale_sd, widen)
    row = AutomaticCalibrationRow(
        outcome="accepted", algorithm_version=BORROW_VERSION,
        camera_calibration_id=camera.id, method="borrowed", aim=aim,
        laser_position=[float(v) for v in lender.laser_position],
        laser_axis=[float(v) for v in lender.laser_axis],
        o_mag_m=float(np.linalg.norm(lender.laser_position)),
        scale_sd=scale_sd, scale_sd_by_range=by_range, **found,
    )  # fmt: skip
    return await persist_automatic_calibration(conn, tenant_id, mission_id, row)
