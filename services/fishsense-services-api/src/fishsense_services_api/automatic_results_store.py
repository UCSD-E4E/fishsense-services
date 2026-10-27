"""The database side of the automatic-results track, tenant-scoped.

New in v2 (no v1 counterpart): fish lengths with no human label, by the chain
cscw-fishsense2027@96a8da07 validated (PAPER.md §6; e2e_measurement/
run_e2e.py, score.py, tail/evaluate.py). Decided 2026-10-06:

* **a separate track** (migration 0036): its own append-only tables,
  producer ``automatic``. Nothing here reads a label as an input or writes a
  label, a human-path prediction or a `measurements` row; the human path is
  exactly as it was;
* **the backlog cohort** (`AUTOMATIC_RESULTS_COHORT`): a dive of **any**
  priority, renderable (`camera_sql.RECTIFIABLE_MISSION`), with automatic work
  outstanding; those with no human measurement (no `measurements` row on any
  of their captures) first, then the oldest, so the orchestrator can take the
  oldest across the tenants it serves. Human-measured dives were left out
  until 2026-10-24, when the owner had them kept current: they are where the
  automatic results are checked against people, and a cut-mask fix at
  version 4 had never reached them. Work is: a canonical frame with no automatic
  head/tail at the current version; a kept mask with no automatic species at
  the current version; no automatic calibration at the current version, or one
  older than the dive's newest automatic head/tail (its candidates may have
  changed); a predicted fish with no current automatic length while a
  calibration exists. Every one of those drains: abstentions, refused
  calibrations and refused lengths are rows;
* **slate frames** come from the slate-presence detector
  (`slate_presence_store.slate_frames`, wired in the orchestrator's stage),
  through `SlateFrames` -- ``(conn, tenant, dive) -> [(capture_id, p)]``.
  At ``p >= 0.5`` (`SLATE_FRAME_THRESHOLD`) a frame is not measured as a fish
  and is a candidate for the label-free calibration (the slate is the rigid
  object the paper validated it on, §4.3);
* **the processor's output is checked** (PLAN.md §9.11): a row for a capture
  outside the dive, or naming another capture's automatic head/tail, is
  refused and nothing is written.
"""

from __future__ import annotations

import json
import math
import uuid

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.prediction_masks_store import (
    PredictionMask,
    insert_prediction_masks,
)
from fishsense_services_api.camera_sql import RECTIFIABLE_MISSION
from fishsense_services_api.service_principal import ServicePrincipal
from fishsense_services_api.slate_presence_store import detector_slate_frame

__all__ = [
    "KEYPOINT_DISAGREEMENT",
    "KEYPOINTS_PREDICTOR_VERSION",
    "AutomaticKeypointFrame",
    "AutomaticKeypointRow",
    "AutomaticMeasureRefusal",
    "automatic_keypoint_inputs",
    "persist_automatic_keypoints",
    "missions_with_automatic_dots",
    "AUTOMATIC_PRODUCER",
    "AUTOMATIC_RESULTS_COHORT",
    "AUTOMATIC_FRAME_MAX_FAILURES",
    "AUTOMATIC_RESULTS_MAX_FAILURES",
    "AUTOMATIC_RESULTS_STEPS",
    "automatic_results_cohort",
    "AutomaticRunPlan",
    "AutomaticRunPlanMission",
    "automatic_run_plan",
    "record_automatic_frame_failures",
    "record_automatic_results_failure",
    "SLATE_FRAME_THRESHOLD",
    "AutomaticCalibrationCandidate",
    "AutomaticCalibrationInputs",
    "AutomaticCalibrationRow",
    "AutomaticCandidate",
    "AutomaticFrame",
    "AutomaticFramesInputs",
    "AutomaticHeadTailRow",
    "AutomaticMeasureCapture",
    "AutomaticMeasureInputs",
    "AutomaticMeasurementRow",
    "AutomaticResultsCatalog",
    "AutomaticSpeciesCapture",
    "AutomaticSpeciesRow",
    "ForeignCapture",
    "ForeignHeadTail",
    "InvalidAutomaticResults",
    "MeasurementCalibration",
    "SlateFrames",
    "automatic_calibration_inputs",
    "automatic_frames_inputs",
    "automatic_measure_inputs",
    "automatic_species_captures",
    "automatic_slate_pose_owed",
    "next_mission_for_automatic_results",
    "no_slate_frames",
    "persist_automatic_calibration",
    "persist_automatic_head_tails",
    "persist_automatic_measurements",
    "persist_automatic_species",
]

AUTOMATIC_PRODUCER = "automatic"

#: A frame the slate-presence detector scores at or above this is a slate
#: frame: excluded from fish measurement, a calibration candidate.
SLATE_FRAME_THRESHOLD = 0.5

#: ``(conn, tenant_id, dive_id) -> [(capture_id, probability)]``: the
#: slate-presence detector's store function, once it lands.
SlateFrames = Callable[
    [AsyncConnection, uuid.UUID, uuid.UUID], Awaitable[list[tuple[uuid.UUID, float]]]
]


async def no_slate_frames(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID
) -> list[tuple[uuid.UUID, float]]:
    """No frame is known to show a slate: the catalog's default, for tests
    and tools that don't read the detector. Production passes the detector's
    `slate_frames` (orchestrator `automatic_results.stage`)."""
    del conn, tenant_id, mission_id
    return []


async def _slate_probabilities(
    slate_frames: SlateFrames,
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
) -> dict[uuid.UUID, float]:
    return {c: float(p) for c, p in await slate_frames(conn, tenant_id, mission_id)}


class InvalidAutomaticResults(ValueError):
    """The processor's output doesn't fit the dive it was made for."""


class ForeignCapture(InvalidAutomaticResults):
    """A row for a capture that is not the dive's."""


class ForeignHeadTail(InvalidAutomaticResults):
    """A row naming an automatic head/tail that is not its capture's."""


# -- the cohort -------------------------------------------------------------------


#: The capture's latest automatic head/tail, `h`, at the version `:hv`.
def _latest(table: str, capture: str = "c") -> str:
    """Capture `capture`'s latest row of `table`, as its `current_*` view
    picks it, but as one index lookup on (tenant_id, capture_id, seq). Read
    through the view, correlated per frame or per dive, Postgres rebuilt it --
    sorting the whole table -- every time: the shape that took the slate
    cohort past the app role's 30 s timeout once its backlog drained
    (2026-10-14)."""
    return f"""(
        SELECT latest.* FROM {table} latest
        WHERE latest.tenant_id = {capture}.tenant_id
          AND latest.capture_id = {capture}.id
        ORDER BY latest.seq DESC
        LIMIT 1
    )"""


#: The head/tail stage's window around the dot (contracts
#: `headtail.HEADTAIL_CROP_WIDTH` / `_HEIGHT`; the API image has no contracts,
#: so they are repeated here and pinned by the orchestrator's tests).
HEADTAIL_CROP_WIDTH = 1800
HEADTAIL_CROP_HEIGHT = 1350

#: The automatic version that grows a cut mask's window to the whole photo
#: (contracts `AUTOMATIC_HEADTAIL_PREDICTOR_VERSION` 4, 2026-10-22).
CUT_MASK_FIX_VERSION = 4


def _cut_by_window(h: str) -> str:
    """Row `h`'s kept mask ran into an edge of the window that is not the
    photo's own (processor `geometry.cut_by_window`, by the half-open box):
    the fish went on past what SAM saw. A row with a kept mask and no box
    (``mask_unscored``) may have been, so it counts as cut."""
    return f"""(
        ({h}.mask_bbox IS NULL AND {h}.status = 'mask_unscored')
        OR ({h}.mask_bbox IS NOT NULL AND (
            ({h}.crop_x > 0 AND {h}.mask_bbox[1] <= {h}.crop_x)
         OR ({h}.crop_x + {HEADTAIL_CROP_WIDTH} < {h}.width
             AND {h}.mask_bbox[3] >= {h}.crop_x + {HEADTAIL_CROP_WIDTH})
         OR ({h}.crop_y > 0 AND {h}.mask_bbox[2] <= {h}.crop_y)
         OR ({h}.crop_y + {HEADTAIL_CROP_HEIGHT} < {h}.height
             AND {h}.mask_bbox[4] >= {h}.crop_y + {HEADTAIL_CROP_HEIGHT})))
    )"""


def head_tail_current(h: str) -> str:
    """Row `h` is the frame's answer at version `:hv`. Version 4 changed only
    frames whose mask the window cut, so a version-3 row of any other frame is
    what 4 would write, and stands: the drain re-runs only the cut ones
    (2026-10-22: 1,346 of 38,923 masks, instead of every frame)."""
    return f"""(
        {h}.predictor_version = :hv
        OR (:hv = {CUT_MASK_FIX_VERSION}
            AND {h}.predictor_version = {CUT_MASK_FIX_VERSION - 1}
            AND NOT {_cut_by_window(h)})
    )"""


_FRAME_DONE = f"""EXISTS (
    SELECT 1 FROM {_latest("automatic_head_tail_predictions")} h
    WHERE {head_tail_current("h")}
)"""

#: Capture `c` is one the slate-presence detector currently calls a slate
#: (0035). The same rule as `slate_frames`, in SQL, so the cohort and the
#: inputs agree.
_DETECTOR_SLATE = detector_slate_frame(SLATE_FRAME_THRESHOLD)


def _calibration_session(tenant: str, mission: str) -> str:
    """The dive is another dive's calibration source (decided 2026-10-15): its
    slate frames feed the label-free fit its borrowers use, and nothing in it
    is segmented, named or measured as a fish."""
    return f"""EXISTS (
    SELECT 1 FROM missions borrower
    WHERE borrower.tenant_id = {tenant}
      AND borrower.calibration_source_mission_id = {mission}
      AND borrower.id <> {mission}
)"""


_SESSION = _calibration_session("d.tenant_id", "d.id")

#: An operator listed the dive as one to leave alone (0039).
_EXCLUDED = """EXISTS (
    SELECT 1 FROM automatic_results_exclusions x
    WHERE x.tenant_id = d.tenant_id AND x.mission_id = d.id
)"""

#: A dive's failed runs before the cohort leaves it for an operator (0040).
AUTOMATIC_RESULTS_MAX_FAILURES = 3
#: Runs a frame may be left out of (0044) before it is a terminal
#: ``frame_failed`` abstention. Separate runs, each after the processor's own
#: tries: a transient fault rarely hits one frame three runs running.
AUTOMATIC_FRAME_MAX_FAILURES = 3

#: The steps a failure is recorded against (0040's check).
AUTOMATIC_RESULTS_STEPS = ("frames", "keypoints", "calibration", "lengths", "species")


#: The dive failed as often as the cohort allows (0040).
def _mission_rows_newest(table: str) -> str:
    """When the newest `table` row on dive `d`'s captures was written: each
    capture's newest row by its `(tenant_id, capture_id, seq)` index, never a
    join over the table (the cohort's plan test pins it)."""
    return f"""(SELECT max(x.created_at) FROM captures xc
            CROSS JOIN LATERAL (
                SELECT latest.created_at FROM {table} latest
                WHERE latest.tenant_id = xc.tenant_id AND latest.capture_id = xc.id
                ORDER BY latest.seq DESC LIMIT 1
            ) x
            WHERE xc.tenant_id = d.tenant_id AND xc.mission_id = d.id)"""


#: When dive `d` last got further: its newest automatic row of any kind.
_LAST_PROGRESS = f"""greatest(
        {_mission_rows_newest("automatic_head_tail_predictions")},
        {_mission_rows_newest("automatic_species_predictions")},
        {_mission_rows_newest("automatic_measurements")},
        (SELECT max(lc.created_at) FROM automatic_laser_calibrations lc
         WHERE lc.tenant_id = d.tenant_id AND lc.mission_id = d.id))"""


def _strikes(since: str = "NULL") -> str:
    return f"""(
        SELECT count(*) FROM automatic_results_failures f
        WHERE f.tenant_id = d.tenant_id AND f.mission_id = d.id
          AND f.created_at > coalesce({since}, '-infinity'::timestamptz)
    )"""


#: Dive `d` failed `AUTOMATIC_RESULTS_MAX_FAILURES` runs with nothing of it
#: written since the first of them. Strikes before the dive last got further
#: don't count (review of #85): three transient faults weeks apart, each run
#: between getting further, are not a bad dive. Progress is only looked up
#: for a dive with that many strikes at all (CASE evaluates in order), so
#: the hourly cohort stays one index probe per dive for the rest.
_FAILED_OUT = f"""CASE
    WHEN {_strikes()} < {AUTOMATIC_RESULTS_MAX_FAILURES} THEN false
    ELSE {_strikes(_LAST_PROGRESS)} >= {AUTOMATIC_RESULTS_MAX_FAILURES}
END"""

#: Dive `d` has a human measurement: any `measurements` row on its captures.
_HUMAN_MEASURED = """EXISTS (
    SELECT 1 FROM measurements m
    JOIN captures mc ON mc.tenant_id = m.tenant_id AND mc.id = m.capture_id
    WHERE mc.tenant_id = d.tenant_id AND mc.mission_id = d.id
)"""

#: A canonical frame with no automatic head/tail at the current version; in
#: a calibration session, only its slate frames.
_FRAMES_WORK = f"""EXISTS (
        SELECT 1 FROM captures c
        WHERE c.tenant_id = d.tenant_id AND c.mission_id = d.id AND c.is_canonical
          AND NOT {_FRAME_DONE}
          AND (NOT {_SESSION} OR {_DETECTOR_SLATE})
    )"""

#: A kept mask with no automatic species at the current version (never in a
#: calibration session).
_SPECIES_WORK = f"""NOT {_SESSION} AND EXISTS (
        SELECT 1 FROM captures c
        CROSS JOIN LATERAL {_latest("automatic_head_tail_predictions")} h
        WHERE c.tenant_id = d.tenant_id AND c.mission_id = d.id AND c.is_canonical
          AND h.status = 'predicted' AND NOT {_DETECTOR_SLATE}
          -- Its pixels are in a render species can crop (0049).
          AND h.jpeg_render_version IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM {_latest("automatic_species_predictions")} s
              WHERE s.automatic_head_tail_prediction_id = h.id
                AND s.predictor_version = :sv
          )
    )"""


def _calibration_work(current: str) -> str:
    """No current automatic calibration `current` accepts (SQL over `a`), or
    one older than the dive's newest frame or slate score, or one fitted
    through a frame the zoom gate (0070) now leaves out."""
    return f"""(
        EXISTS (
            SELECT 1 FROM captures c
            WHERE c.tenant_id = d.tenant_id AND c.mission_id = d.id AND c.is_canonical
        )
        AND NOT EXISTS (
            SELECT 1 FROM current_automatic_laser_calibrations a
            WHERE a.tenant_id = d.tenant_id AND a.mission_id = d.id
              AND {current}
              -- Stale against new frames, and against a slate score written
              -- after it: the detector's slate frames are the candidates. A
              -- capture's newest row is its current one (append-only), so
              -- "any row newer" is "the newest is newer".
              AND NOT EXISTS (
                  SELECT 1 FROM captures c
                  WHERE c.tenant_id = d.tenant_id AND c.mission_id = d.id
                    AND (
                      EXISTS (
                          SELECT 1 FROM automatic_head_tail_predictions h
                          WHERE h.tenant_id = c.tenant_id AND h.capture_id = c.id
                            AND h.created_at > a.created_at
                      )
                      OR EXISTS (
                          SELECT 1 FROM slate_presence_predictions sp
                          WHERE sp.tenant_id = c.tenant_id AND sp.capture_id = c.id
                            AND sp.created_at > a.created_at
                      )
                    )
              )
              -- Fitted before the zoom gate, through a frame it now leaves
              -- out (2026-10-25: mission 237, 2 of 18): refitting drops it.
              AND NOT EXISTS (
                  SELECT 1 FROM captures c
                  WHERE c.tenant_id = a.tenant_id AND c.id = ANY(a.capture_ids)
                    AND NOT (focal_length_calibrated(c.focal_length_mm)
                         AND before_beam_move(c.tenant_id, c.mission_id, c.captured_at))
              )
        )
    )"""


#: The size-constancy fit at the current version (`:cv`).
_CALIBRATION_WORK = _calibration_work(
    "a.method = 'size_constancy' AND a.algorithm_version = :cv"
)

#: The slate-pose fit (calibration v4, 0078; the owner, 2026-10-26), only
#: with its switch on (`:pv`, its version; NULL: off) and for a mission with
#: a slate template: at that version, posed with the mission's template.
_SLATE_POSE_WORK = f"""(
        CAST(:pv AS text) IS NOT NULL AND d.slate_template_id IS NOT NULL
        AND {_calibration_work(
            "a.method = 'slate_pose' AND a.algorithm_version = :pv "
            # The current view keeps 0036's columns (0078): the template
            # is read off the row itself, by its primary key.
            "AND (SELECT p.slate_template_id FROM automatic_laser_calibrations p "
            "WHERE p.id = a.id) = d.slate_template_id")}
    )"""

#: Automatic head/tail v5 (owner, 2026-10-24/25): fishsense-core's SimCC
#: keypoints on the frame's SAM box replace the SAM rule's extreme points for
#: every frame but the fish models'. Its rows are `automatic_keypoint_
#: predictions` (0074), beside the head/tail rows, which stay as they are.
KEYPOINTS_PREDICTOR_VERSION = 5
#: Where SimCC's and the SAM rule's lengths differ by more than this share of
#: the SAM rule's, the frame goes to review instead of being measured (owner,
#: 2026-10-24: 15%; 11% of non-model frames on the replay).
KEYPOINT_DISAGREEMENT = 0.15

#: Capture `c` is a fish model's frame, which keeps the SAM rule: its mission
#: is a ``known_length`` one or its human species label names a Fish Model --
#: exactly the 29 model missions, 2,717 frames (2026-10-25).
_MODEL_FRAME = """(
    EXISTS (SELECT 1 FROM missions mm
            WHERE mm.tenant_id = c.tenant_id AND mm.id = c.mission_id
              AND mm.purpose = 'known_length')
    OR EXISTS (SELECT 1 FROM measurement_subjects ms
               WHERE ms.tenant_id = c.tenant_id AND ms.capture_id = c.id
                 AND ms.model_name IS NOT NULL)
)"""

#: Prediction `h`'s latest keypoints, as `k` (LATERAL), by the (tenant,
#: prediction, seq) index -- not through the current view, which the planner
#: rebuilds whole (2026-10-21's 145 s measure query).
_KEYPOINTS = """LATERAL (
        SELECT k.* FROM automatic_keypoint_predictions k
        WHERE k.tenant_id = h.tenant_id AND k.automatic_head_tail_prediction_id = h.id
        ORDER BY k.seq DESC
        LIMIT 1
    ) k"""
#: `k` is at the current version.
_KEYPOINTS_CURRENT = f"k.predictor_version = {KEYPOINTS_PREDICTOR_VERSION}"

#: A non-model frame's rendered SAM prediction with no keypoints yet (never
#: in a calibration session, never a slate).
_KEYPOINTS_WORK = f"""NOT {_SESSION} AND EXISTS (
        SELECT 1 FROM captures c
        CROSS JOIN LATERAL {_latest("automatic_head_tail_predictions")} h
        LEFT JOIN {_KEYPOINTS} ON true
        WHERE c.tenant_id = d.tenant_id AND c.mission_id = d.id AND c.is_canonical
          AND h.status = 'predicted' AND h.jpeg_render_version IS NOT NULL
          AND NOT {_DETECTOR_SLATE} AND NOT {_MODEL_FRAME}
          AND (k.id IS NULL OR NOT {_KEYPOINTS_CURRENT})
    )"""

#: A predicted fish with no current automatic length while a calibration
#: exists (never in a calibration session). A non-model frame is owed one
#: only once it has keypoints, and a length is current only for the
#: keypoints it was measured from (NULL: the SAM rule's, a model frame's).
_LENGTHS_WORK = f"""NOT {_SESSION} AND EXISTS (
        SELECT 1 FROM automatic_measurement_calibrations mc
        JOIN captures c ON c.tenant_id = mc.tenant_id AND c.mission_id = mc.mission_id
        CROSS JOIN LATERAL {_latest("automatic_head_tail_predictions")} h
        LEFT JOIN {_KEYPOINTS} ON true
        WHERE mc.tenant_id = d.tenant_id AND mc.mission_id = d.id AND c.is_canonical
          AND h.status = 'predicted'
          AND ({_MODEL_FRAME} OR {_KEYPOINTS_CURRENT})
          -- A zoomed frame is never measured (the zoom gate, 0070).
          AND focal_length_calibrated(c.focal_length_mm)
          AND before_beam_move(c.tenant_id, c.mission_id, c.captured_at)
          -- A frame the detector now calls a slate is never measured
          -- (automatic_measure_inputs), so it is no length outstanding.
          AND NOT {_DETECTOR_SLATE}
          -- current_automatic_measurements: the latest length whose head/tail
          -- and calibration are still the frame's and the mission's.
          AND (
              SELECT m.algorithm_version FROM automatic_measurements m
              WHERE m.tenant_id = c.tenant_id AND m.capture_id = c.id
                AND m.automatic_head_tail_prediction_id = h.id
                AND m.calibration_source = mc.calibration_source
                AND m.automatic_laser_calibration_id
                    IS NOT DISTINCT FROM mc.automatic_laser_calibration_id
                AND m.laser_calibration_id IS NOT DISTINCT FROM mc.laser_calibration_id
                AND m.automatic_keypoint_prediction_id IS NOT DISTINCT FROM k.id
              ORDER BY m.seq DESC
              LIMIT 1
          ) IS DISTINCT FROM :mv
    )"""


def automatic_results_cohort(*, species: bool = True) -> str:
    """The backlog cohort over dive `d`, but for the tenant term the selector
    adds. Any priority. `:hv`, `:sv`, `:cv`, `:pv`, `:mv` are the stages'
    current versions (`:sv` unused with `species` off; `:pv` NULL with the
    slate pose off). Species has its own switch (2026-10-15): off, a dive
    whose only work is species is not in it."""
    work = [_FRAMES_WORK, _KEYPOINTS_WORK, _CALIBRATION_WORK, _SLATE_POSE_WORK,
            _LENGTHS_WORK]  # fmt: skip
    if species:
        work.insert(1, _SPECIES_WORK)
    return f"""{RECTIFIABLE_MISSION}
    AND NOT {_EXCLUDED}
    AND NOT {_FAILED_OUT}
    AND ({" OR ".join(f"({w})" for w in work)})"""


#: The cohort with species on, the default.
AUTOMATIC_RESULTS_COHORT = automatic_results_cohort()


@dataclass(frozen=True)
class AutomaticCandidate:
    mission_id: uuid.UUID
    created_at: datetime


# -- the per-mission input queries --------------------------------------------------
#
# 2026-10-21: these joined the `current_automatic_*` views wholesale, and the
# planner -- expecting one row from each -- rebuilt the tenant's every current
# row once per frame: mission 312's measure inputs took 145 s, past the 30 s
# statement timeout, so lengths stopped being written as the run filled the
# tables (12 s for a 9-frame mission). Each frame's current row is now a
# LATERAL lookup on the view filtered by its capture -- its DISTINCT ON key, so
# the filter reaches the (tenant_id, capture_id, seq) index -- with LIMIT 1, or
# the planner flattens it back into the join: 0.29 s for 312, the same rows. Named, so their plans are tested
# (`test_a_missions_inputs_read_each_frames_current_rows_by_its_index`).


def _current_row(view: str, alias: str, where: str = "") -> str:
    """A LATERAL subquery: capture `c`'s current row of `view`, as `alias`,
    filtered by `where` after the view picks it. ``LIMIT 1`` keeps the
    planner from flattening it back into the wholesale join (each view has at
    most one row per capture, so it drops nothing)."""
    return (
        f"LATERAL (SELECT {alias}.* FROM {view} {alias} "
        f"WHERE {alias}.tenant_id = c.tenant_id AND {alias}.capture_id = c.id"
        f"{' AND ' + where if where else ''} LIMIT 1) {alias}"
    )


AUTOMATIC_MEASURE_CAPTURES = f"""
    SELECT h.capture_id, h.id, h.laser_x, h.laser_y, h.head_x, h.head_y,
           h.tail_x, h.tail_y, {_MODEL_FRAME} AS model_frame, k.id AS keypoint_id,
           k.status AS keypoint_status, k.head_x AS k_head_x, k.head_y AS k_head_y,
           k.tail_x AS k_tail_x, k.tail_y AS k_tail_y
    FROM captures c
    CROSS JOIN {_current_row("current_automatic_head_tail_predictions", "h")}
    LEFT JOIN {_KEYPOINTS} ON true
    LEFT JOIN {_current_row("current_automatic_measurements", "m",
                            "m.algorithm_version = :mv AND "
                            "m.automatic_keypoint_prediction_id IS NOT DISTINCT FROM k.id")
              } ON true
    WHERE c.tenant_id = :t AND c.mission_id = :d AND c.is_canonical
      AND h.status = 'predicted' AND m.id IS NULL
      -- head/tail v5: a non-model frame waits for its keypoints
      AND ({_MODEL_FRAME} OR {_KEYPOINTS_CURRENT})
      -- the zoom gate (0070): its depth assumes intrinsics it was not shot at
      AND focal_length_calibrated(c.focal_length_mm)
      AND before_beam_move(c.tenant_id, c.mission_id, c.captured_at)
      AND NOT {_calibration_session(":t", ":d")}
    ORDER BY c.number
    """

AUTOMATIC_SPECIES_CAPTURES = f"""
    SELECT c.id, c.checksum, c.v1_id IS NOT NULL AS from_v1,
           h.id AS head_tail_id, h.mask_bbox,
           s.id IS NOT NULL AS has_existing, h.jpeg_render_version,
           h.jpeg_camera_calibration_id
    FROM captures c
    CROSS JOIN {_current_row("current_automatic_head_tail_predictions", "h")}
    LEFT JOIN {_current_row("current_automatic_species_predictions", "s")} ON true
    WHERE c.tenant_id = :t AND c.mission_id = :d AND c.is_canonical
      AND h.status = 'predicted' AND NOT {_DETECTOR_SLATE}
      AND h.jpeg_render_version IS NOT NULL
      AND NOT {_calibration_session(":t", ":d")}
      AND (s.id IS NULL OR s.automatic_head_tail_prediction_id <> h.id
           OR s.predictor_version IS DISTINCT FROM :sv)
    ORDER BY c.number
    """

AUTOMATIC_CALIBRATION_CAPTURES = f"""
    SELECT c.id, c.checksum, c.v1_id IS NOT NULL AS from_v1,
           h.laser_x, h.laser_y, h.jpeg_render_version,
           h.jpeg_camera_calibration_id
    FROM captures c
    CROSS JOIN {_current_row("current_automatic_head_tail_predictions", "h")}
    WHERE c.tenant_id = :t AND c.mission_id = :d AND c.is_canonical
      AND h.laser_x IS NOT NULL
      -- the zoom gate (0070): a zoomed frame's dot sits on another line
      AND focal_length_calibrated(c.focal_length_mm)
      AND before_beam_move(c.tenant_id, c.mission_id, c.captured_at)
    ORDER BY c.number
    """


async def next_mission_for_automatic_results(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    headtail_version: int,
    species_version: int,
    calibration_version: str,
    measurement_version: str,
    species: bool = True,
    exclude: Sequence[uuid.UUID] = (),
    slate_pose_version: str | None = None,
) -> AutomaticCandidate | None:
    """The tenant's next dive in the backlog cohort, other than `exclude`
    (the dives the run already took): one nobody has measured first, then the
    oldest. `slate_pose_version`: the slate-pose fit's, with it switched on
    (0078); None, off."""
    row = (
        await conn.execute(
            text(f"""
                SELECT d.id, d.created_at FROM missions d
                WHERE d.tenant_id = :t AND {automatic_results_cohort(species=species)}
                  AND NOT d.id = ANY(CAST(:exclude AS uuid[]))
                ORDER BY {_HUMAN_MEASURED}, d.created_at, d.id
                LIMIT 1
                """),
            {"t": tenant_id, "hv": headtail_version, "sv": species_version,
             "cv": calibration_version, "mv": measurement_version,
             "pv": slate_pose_version, "exclude": list(exclude)},
        )
    ).one_or_none()  # fmt: skip
    return None if row is None else AutomaticCandidate(row.id, row.created_at)


async def automatic_slate_pose_owed(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID, *, version: str
) -> bool:
    """Whether the mission's slate-pose fit is owed at `version` (0078): the
    cohort's term, for one mission, so the chain poses only when it is."""
    return (
        await conn.execute(
            text(f"""
                SELECT {_SLATE_POSE_WORK} FROM missions d
                WHERE d.tenant_id = :t AND d.id = :m
                """),
            {"t": tenant_id, "m": mission_id, "pv": version},
        )
    ).scalar_one()


async def record_automatic_results_failure(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    *,
    step: str,
    error: str,
) -> None:
    """A failed run of the dive's `step` (0040); at
    `AUTOMATIC_RESULTS_MAX_FAILURES` with nothing of the dive written since
    (`_FAILED_OUT`), the cohort leaves the dive."""
    await conn.execute(
        text(
            "INSERT INTO automatic_results_failures "
            "(tenant_id, mission_id, step, error) VALUES (:t, :d, :step, :error)"
        ),
        {"t": tenant_id, "d": mission_id, "step": step, "error": error[:4000]},
    )


async def record_automatic_frame_failures(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    failures: Sequence[tuple[uuid.UUID, str]],
    *,
    headtail_version: int,
) -> list[uuid.UUID]:
    """Frames the processor left out of a run (0044). Never one of the
    dive's strikes: the frame stays owed, so the next run asks for it again.
    A frame left out at `AUTOMATIC_FRAME_MAX_FAILURES` runs of this version
    gets its terminal ``frame_failed`` head/tail here, in the same
    transaction, so it drains and the dive goes on. Returns those frames."""
    failures = list(failures)
    if not failures:
        return []
    await _check_captures(conn, tenant_id, mission_id, [c for c, _ in failures])
    for capture_id, error in failures:
        await conn.execute(
            text(
                "INSERT INTO automatic_frame_failures "
                "(tenant_id, capture_id, predictor_version, error) "
                "VALUES (:t, :c, :hv, :error)"
            ),
            {"t": tenant_id, "c": capture_id, "hv": headtail_version,
             "error": error[:4000]},
        )  # fmt: skip
    exhausted = list(
        (
            await conn.execute(
                text("""
                    SELECT f.capture_id FROM automatic_frame_failures f
                    WHERE f.tenant_id = :t AND f.capture_id = ANY(:ids)
                      AND f.predictor_version = :hv
                    GROUP BY f.capture_id
                    HAVING count(*) >= :max
                    """),
                {"t": tenant_id, "ids": [c for c, _ in failures],
                 "hv": headtail_version, "max": AUTOMATIC_FRAME_MAX_FAILURES},
            )
        ).scalars()
    )  # fmt: skip
    await _insert(
        conn,
        "automatic_head_tail_predictions",
        tenant_id,
        [
            AutomaticHeadTailRow(
                capture_id=c, status="frame_failed", predictor_version=headtail_version
            )
            for c in exhausted
        ],
    )
    return exhausted


# -- a by-hand run of named dives (Phase C) -----------------------------------------


@dataclass(frozen=True)
class AutomaticRunPlanMission:
    """A named dive as a by-hand run would take it."""

    number: int
    mission_id: uuid.UUID
    folder: str
    calibration_session: bool
    excluded: bool
    human_measured: bool
    rectifiable: bool
    frames: int
    frames_owed: int


@dataclass(frozen=True)
class AutomaticRunPlan:
    missions: list[AutomaticRunPlanMission]
    #: Numbers with no dive in the tenant.
    missing: list[int]


async def automatic_run_plan(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    numbers: Sequence[int],
    *,
    headtail_version: int,
) -> AutomaticRunPlan:
    """The named dives, calibration sessions first -- their slate frames feed
    the label-free fits their borrowers use -- then in the order given; what
    the operator reviews before a by-hand run (the validation's paper dives).
    An excluded dive is still planned (by hand it runs, 0039), and said so."""
    rows = {
        r.number: r
        for r in await conn.execute(
            text(f"""
                SELECT d.number, d.id, d.source_path,
                       {_SESSION} AS calibration_session,
                       {_EXCLUDED} AS excluded,
                       {_HUMAN_MEASURED} AS human_measured,
                       {RECTIFIABLE_MISSION} AS rectifiable,
                       (SELECT count(*) FROM captures c
                        WHERE c.tenant_id = d.tenant_id AND c.mission_id = d.id
                          AND c.is_canonical) AS frames,
                       (SELECT count(*) FROM captures c
                        WHERE c.tenant_id = d.tenant_id AND c.mission_id = d.id
                          AND c.is_canonical AND NOT {_FRAME_DONE}
                          AND (NOT {_SESSION} OR {_DETECTOR_SLATE})) AS frames_owed
                FROM missions d
                WHERE d.tenant_id = :t AND d.number = ANY(:numbers)
                  AND d.source = 'lite'
                """),
            {"t": tenant_id, "numbers": list(numbers), "hv": headtail_version},
        )
    }
    planned = [
        AutomaticRunPlanMission(
            number=r.number, mission_id=r.id, folder=r.source_path,
            calibration_session=r.calibration_session, excluded=r.excluded,
            human_measured=r.human_measured, rectifiable=r.rectifiable,
            frames=r.frames, frames_owed=r.frames_owed,
        )
        for n in dict.fromkeys(numbers)
        if (r := rows.get(n)) is not None
    ]  # fmt: skip
    planned.sort(key=lambda p: not p.calibration_session)  # stable: given order
    return AutomaticRunPlan(
        missions=planned, missing=[n for n in dict.fromkeys(numbers) if n not in rows]
    )


# -- frames: the GPU stage's input --------------------------------------------------


@dataclass(frozen=True)
class AutomaticFrame:
    capture_id: uuid.UUID
    checksum: str
    #: Migrated from v1: its JPEG may be where v1 wrote it.
    from_v1: bool
    #: The slate-presence detector's score; None with no detector.
    slate_probability: float | None = None

    @property
    def is_slate(self) -> bool:
        return (
            self.slate_probability is not None
            and self.slate_probability >= SLATE_FRAME_THRESHOLD
        )


@dataclass(frozen=True)
class AutomaticFramesInputs:
    camera_matrix: list[list[float]]
    distortion_coefficients: list[float]
    frames: list[AutomaticFrame]
    #: The calibration those intrinsics are: part of the render's key.
    camera_calibration_id: uuid.UUID | None = None


def _flat(values) -> list[float]:
    if values and all(isinstance(v, list) for v in values):
        return [float(x) for row in values for x in row]
    return [float(x) for x in values]


async def _pinhole_camera(conn: AsyncConnection, tenant_id, mission_id):
    row = (
        await conn.execute(
            text("""
                SELECT cc.id, cc.camera_matrix, cc.distortion_coefficients
                FROM missions d
                JOIN current_camera_calibrations cc
                  ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
                WHERE d.tenant_id = :t AND d.id = :d AND cc.camera_model = 'pinhole'
                """),
            {"t": tenant_id, "d": mission_id},
        )
    ).one_or_none()
    if row is None:
        raise ValueError(f"dive {mission_id} has no pinhole camera calibration")
    return row


async def automatic_frames_inputs(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    *,
    headtail_version: int,
    slate_frames: SlateFrames = no_slate_frames,
) -> AutomaticFramesInputs:
    """The dive's canonical frames with no automatic head/tail at the current
    version, in capture order, each with its slate score; and the intrinsics
    the raw is rectified with."""
    camera = await _pinhole_camera(conn, tenant_id, mission_id)
    slate = await _slate_probabilities(slate_frames, conn, tenant_id, mission_id)
    rows = await conn.execute(
        text(f"""
            SELECT c.id, c.checksum, c.v1_id IS NOT NULL AS from_v1
            FROM captures c
            WHERE c.tenant_id = :t AND c.mission_id = :d AND c.is_canonical
              AND NOT {_FRAME_DONE}
              AND (NOT {_calibration_session(":t", ":d")} OR {_DETECTOR_SLATE})
            ORDER BY c.number
            """),
        {"t": tenant_id, "d": mission_id, "hv": headtail_version},
    )
    return AutomaticFramesInputs(
        camera_matrix=[[float(x) for x in r] for r in camera.camera_matrix],
        distortion_coefficients=_flat(camera.distortion_coefficients),
        frames=[
            AutomaticFrame(r.id, r.checksum, r.from_v1, slate.get(r.id)) for r in rows
        ],
        camera_calibration_id=camera.id,
    )


@dataclass(frozen=True)
class AutomaticHeadTailRow:
    """One frame's automatic dot and head/tail (or abstention) to append."""

    capture_id: uuid.UUID
    status: str
    predictor_version: int
    laser_x: float | None = None
    laser_y: float | None = None
    laser_confidence: float | None = None
    laser_predictor_version: int | None = None
    laser_checkpoint: str | None = None
    #: The dot's colour as the laser kernel read it, and how clearly (0072).
    laser_color: str | None = None
    laser_color_margin: float | None = None
    head_x: float | None = None
    head_y: float | None = None
    tail_x: float | None = None
    tail_y: float | None = None
    width: int | None = None
    height: int | None = None
    mask_area_px: int | None = None
    silhouette_ratio: float | None = None
    crop_x: int | None = None
    crop_y: int | None = None
    mask_bbox: list[int] | None = None
    sam_score: float | None = None
    slate_probability: float | None = None
    checkpoint: str | None = None
    core_version: str | None = None
    #: The render of the raw the dot and mask were found in: its version and
    #: the camera calibration it rectified with (0049).
    jpeg_render_version: int | None = None
    jpeg_camera_calibration_id: uuid.UUID | None = None


async def _check_captures(conn, tenant_id, mission_id, captures) -> None:
    captures = set(captures)
    owned = set(
        (
            await conn.execute(
                text("""
                    SELECT id FROM captures
                    WHERE tenant_id = :t AND mission_id = :d AND id = ANY(:ids)
                    """),
                {"t": tenant_id, "d": mission_id, "ids": list(captures)},
            )
        ).scalars()
    )
    if foreign := captures - owned:
        raise ForeignCapture(
            f"not captures of dive {mission_id}: {sorted(map(str, foreign))}"
        )


async def _check_head_tails(conn, tenant_id, rows) -> None:
    named = {r.automatic_head_tail_prediction_id for r in rows}
    owner = {
        r.id: r.capture_id
        for r in await conn.execute(
            text("""
                SELECT id, capture_id FROM automatic_head_tail_predictions
                WHERE tenant_id = :t AND id = ANY(:ids)
                """),
            {"t": tenant_id, "ids": list(named)},
        )
    }
    for r in rows:
        if owner.get(r.automatic_head_tail_prediction_id) != r.capture_id:
            raise ForeignHeadTail(
                f"automatic head/tail {r.automatic_head_tail_prediction_id} is not "
                f"capture {r.capture_id}'s"
            )


async def _insert(conn, table: str, tenant_id, rows: Sequence) -> list[uuid.UUID]:
    ids = []
    for r in rows:
        values = asdict(r)
        columns = ", ".join(["tenant_id", *values])
        params = ", ".join([":tenant_id", *(f":{k}" for k in values)])
        ids.append(
            (
                await conn.execute(
                    text(f"INSERT INTO {table} ({columns}) VALUES ({params}) "
                         "RETURNING id"),
                    {"tenant_id": tenant_id, **values},
                )
            ).scalar_one()
        )  # fmt: skip
    return ids


async def persist_automatic_head_tails(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    rows: Sequence[AutomaticHeadTailRow],
    masks: Sequence[PredictionMask | None] = (),
) -> int:
    """Append the dive's automatic head/tails, abstentions included, all or
    nothing, in the caller's transaction, and name each stored mask -- `masks`
    parallel to `rows`, or empty -- against its row's prediction (0059).
    Returns how many rows were written."""
    rows = list(rows)
    if not rows:
        if masks:
            raise ValueError("masks with no prediction to name them against")
        return 0
    await _check_captures(conn, tenant_id, mission_id, [r.capture_id for r in rows])
    ids = await _insert(conn, "automatic_head_tail_predictions", tenant_id, rows)
    if masks:
        await insert_prediction_masks(conn, tenant_id, "automatic", rows, ids, masks)
    return len(ids)


# -- keypoints (head/tail v5) ----------------------------------------------------------

AUTOMATIC_KEYPOINT_CAPTURES = f"""
    SELECT c.id, c.checksum, c.v1_id IS NOT NULL AS from_v1, h.id AS head_tail_id,
           h.mask_bbox, h.jpeg_render_version, h.jpeg_camera_calibration_id
    FROM captures c
    CROSS JOIN {_current_row("current_automatic_head_tail_predictions", "h")}
    LEFT JOIN {_KEYPOINTS} ON true
    WHERE c.tenant_id = :t AND c.mission_id = :d AND c.is_canonical
      AND h.status = 'predicted' AND h.jpeg_render_version IS NOT NULL
      AND NOT {_DETECTOR_SLATE} AND NOT {_MODEL_FRAME}
      AND NOT {_calibration_session(":t", ":d")}
      AND (k.id IS NULL OR NOT {_KEYPOINTS_CURRENT})
    ORDER BY c.number
    """


@dataclass(frozen=True)
class AutomaticKeypointFrame:
    """A non-model frame's SAM prediction owed keypoints: SimCC's crop is cut
    from `mask_bbox`, in pixels of the render named."""

    capture_id: uuid.UUID
    checksum: str
    from_v1: bool
    prediction_id: uuid.UUID
    mask_bbox: list[int]
    jpeg_render_version: int
    jpeg_camera_calibration_id: uuid.UUID | None


async def automatic_keypoint_inputs(
    conn: AsyncConnection, tenant_id: uuid.UUID, mission_id: uuid.UUID
) -> list[AutomaticKeypointFrame]:
    """The dive's frames owed keypoints at `KEYPOINTS_PREDICTOR_VERSION`: each
    non-model frame's current, rendered SAM prediction (never a slate, never
    in a calibration session) with none yet."""
    rows = await conn.execute(
        text(AUTOMATIC_KEYPOINT_CAPTURES), {"t": tenant_id, "d": mission_id}
    )
    return [
        AutomaticKeypointFrame(r.id, r.checksum, r.from_v1, r.head_tail_id,
                               list(r.mask_bbox), r.jpeg_render_version,
                               r.jpeg_camera_calibration_id)  # fmt: skip
        for r in rows
    ]


@dataclass(frozen=True)
class AutomaticKeypointRow:
    """SimCC's answer for one prediction: ``predicted`` (points in render
    px) or ``refused`` (the model refused the box)."""

    prediction_id: uuid.UUID
    capture_id: uuid.UUID
    status: str
    head: list[float] | None = None
    tail: list[float] | None = None
    confidence: float | None = None
    crop: list[int] | None = None
    checkpoint: str | None = None


async def persist_automatic_keypoints(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    rows: Sequence[AutomaticKeypointRow],
) -> int:
    """Append keypoints for predictions still their frames' current ones,
    flagging a disagreement with the SAM rule over `KEYPOINT_DISAGREEMENT`
    (``keypoints_disagree``) and a refused box (``keypoints_refused``); both
    lengths are recorded. Any other status (the render was not there) writes
    nothing: the frame stays owed. Returns how many rows were written."""
    rows = [r for r in rows if r.status in ("predicted", "refused")]
    if not rows:
        return 0
    await _check_captures(conn, tenant_id, mission_id, [r.capture_id for r in rows])
    current = {
        r.id: r
        for r in await conn.execute(
            text("""
                SELECT id, capture_id, head_x, head_y, tail_x, tail_y
                FROM current_automatic_head_tail_predictions
                WHERE tenant_id = :t AND capture_id = ANY(:c) AND status = 'predicted'
                """),
            {"t": tenant_id, "c": [r.capture_id for r in rows]},
        )
    }
    written = 0
    for r in rows:
        h = current.get(r.prediction_id)
        if h is None or h.capture_id != r.capture_id:
            continue  # superseded since it was planned: its frame is owed again
        sam = float(math.hypot(h.head_x - h.tail_x, h.head_y - h.tail_y))
        values = dict(
            tenant_id=tenant_id, capture_id=r.capture_id, prediction=r.prediction_id,
            head_x=None, head_y=None, tail_x=None, tail_y=None,
            confidence=r.confidence, crop=r.crop, sam=sam, simcc=None,
            disagreement=None, version=KEYPOINTS_PREDICTOR_VERSION,
            checkpoint=r.checkpoint, status="keypoints_refused",
        )  # fmt: skip
        if r.status == "predicted":
            simcc = float(math.hypot(r.head[0] - r.tail[0], r.head[1] - r.tail[1]))
            off = abs(simcc - sam) / sam if sam > 0 else float("inf")
            values.update(
                head_x=r.head[0], head_y=r.head[1], tail_x=r.tail[0], tail_y=r.tail[1],
                simcc=simcc, disagreement=off,
                status="keypoints_disagree" if off > KEYPOINT_DISAGREEMENT else "predicted",
            )  # fmt: skip
        await conn.execute(
            text("""
                INSERT INTO automatic_keypoint_predictions (
                    tenant_id, capture_id, automatic_head_tail_prediction_id, status,
                    head_x, head_y, tail_x, tail_y, confidence, crop,
                    sam_rule_length_px, simcc_length_px, disagreement,
                    predictor_version, checkpoint)
                VALUES (:tenant_id, :capture_id, :prediction, :status, :head_x,
                        :head_y, :tail_x, :tail_y, :confidence, :crop, :sam, :simcc,
                        :disagreement, :version, :checkpoint)
                """),
            values,
        )
        written += 1
    return written


# -- species ------------------------------------------------------------------------


@dataclass(frozen=True)
class AutomaticSpeciesCapture:
    capture_id: uuid.UUID
    checksum: str
    from_v1: bool
    automatic_head_tail_prediction_id: uuid.UUID
    mask_bbox: list[int]
    has_existing_prediction: bool
    #: The render the mask's box is in pixels of (0049).
    jpeg_render_version: int | None = None
    jpeg_camera_calibration_id: uuid.UUID | None = None


async def automatic_species_captures(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    *,
    species_version: int,
) -> list[AutomaticSpeciesCapture]:
    """The dive's kept automatic masks with no automatic species at the
    current version cropped from them, in capture order."""
    rows = await conn.execute(
        text(AUTOMATIC_SPECIES_CAPTURES),
        {"t": tenant_id, "d": mission_id, "sv": species_version},
    )
    return [
        AutomaticSpeciesCapture(r.id, r.checksum, r.from_v1, r.head_tail_id,
                                list(r.mask_bbox), r.has_existing,
                                r.jpeg_render_version,
                                r.jpeg_camera_calibration_id)  # fmt: skip
        for r in rows
    ]


@dataclass(frozen=True)
class AutomaticSpeciesRow:
    capture_id: uuid.UUID
    automatic_head_tail_prediction_id: uuid.UUID
    status: str
    predictor_version: int
    model_id: str
    predicted_choice: str | None = None
    top1_probability: float | None = None
    margin: float | None = None
    top5: list[dict] = field(default_factory=list)


async def persist_automatic_species(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    rows: Sequence[AutomaticSpeciesRow],
) -> int:
    """Append zero-shot species for the dive's automatic masks; each must name
    its own capture's automatic head/tail."""
    rows = list(rows)
    if not rows:
        return 0
    await _check_captures(conn, tenant_id, mission_id, [r.capture_id for r in rows])
    await _check_head_tails(conn, tenant_id, rows)
    for r in rows:
        await conn.execute(
            text("""
                INSERT INTO automatic_species_predictions (
                    tenant_id, capture_id, automatic_head_tail_prediction_id, status,
                    predictor_version, model_id, predicted_choice, top1_probability,
                    margin, top5)
                VALUES (:t, :capture_id, :automatic_head_tail_prediction_id, :status,
                        :predictor_version, :model_id, :predicted_choice,
                        :top1_probability, :margin, CAST(:top5 AS jsonb))
                """),
            {"t": tenant_id, **asdict(r), "top5": json.dumps(r.top5)},
        )
    return len(rows)


# -- calibration ----------------------------------------------------------------------


@dataclass(frozen=True)
class AutomaticCalibrationCandidate:
    """A slate frame with its automatic dot."""

    capture_id: uuid.UUID
    checksum: str
    from_v1: bool
    x: float
    y: float
    #: The render the dot was found against; None: none recorded (0049).
    jpeg_render_version: int | None = None
    jpeg_camera_calibration_id: uuid.UUID | None = None


@dataclass(frozen=True)
class AutomaticCalibrationInputs:
    camera_calibration_id: uuid.UUID
    camera_matrix: list[list[float]]
    candidates: list[AutomaticCalibrationCandidate]
    #: Every automatic dot of the dive: the dive line is fitted through them.
    line_dots: list[list[float]]


async def automatic_calibration_inputs(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    *,
    slate_frames: SlateFrames = no_slate_frames,
) -> AutomaticCalibrationInputs:
    """What the label-free fit reads: the dive's slate frames (p >= 0.5) with
    a current automatic dot, in capture order, and every automatic dot."""
    camera = await _pinhole_camera(conn, tenant_id, mission_id)
    slate = await _slate_probabilities(slate_frames, conn, tenant_id, mission_id)
    rows = (
        await conn.execute(
            text(AUTOMATIC_CALIBRATION_CAPTURES),
            {"t": tenant_id, "d": mission_id},
        )
    ).all()
    return AutomaticCalibrationInputs(
        camera_calibration_id=camera.id,
        camera_matrix=[[float(x) for x in r] for r in camera.camera_matrix],
        candidates=[
            AutomaticCalibrationCandidate(r.id, r.checksum, r.from_v1, r.laser_x,
                                          r.laser_y, r.jpeg_render_version,
                                          r.jpeg_camera_calibration_id)  # fmt: skip
            for r in rows
            if slate.get(r.id, 0.0) >= SLATE_FRAME_THRESHOLD
        ],
        line_dots=[[r.laser_x, r.laser_y] for r in rows],
    )


async def missions_with_automatic_dots(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> list[tuple[uuid.UUID, int]]:
    """``(mission id, number)`` of every mission with a current automatic dot,
    by number: label-free calibration v3's replay (2026-10-22)."""
    rows = await conn.execute(
        text("""
            SELECT d.id, d.number FROM missions d
            WHERE d.tenant_id = :t AND EXISTS (
                SELECT 1 FROM current_automatic_head_tail_predictions a
                JOIN captures c ON c.tenant_id = a.tenant_id AND c.id = a.capture_id
                WHERE a.tenant_id = d.tenant_id AND c.mission_id = d.id
                  AND a.laser_x IS NOT NULL)
            ORDER BY d.number
            """),
        {"t": tenant_id},
    )
    return [(r.id, r.number) for r in rows]


@dataclass(frozen=True)
class AutomaticCalibrationRow:
    outcome: str
    algorithm_version: str
    refusal_reason: str | None = None
    camera_calibration_id: uuid.UUID | None = None
    laser_position: list[float] | None = None
    laser_axis: list[float] | None = None
    vanishing_px: float | None = None
    line_direction: list[float] | None = None
    line_offset_px: float | None = None
    o_mag_m: float | None = None
    frames_used: int = 0
    candidate_count: int = 0
    pair_count: int = 0
    size_ratio: float | None = None
    se_px: float | None = None
    pair_residual_sd: float | None = None
    capture_ids: list[uuid.UUID] = field(default_factory=list)
    core_version: str | None = None
    #: ``size_constancy``, or ``slate_pose`` (calibration v4, 0078), which
    #: names the template it posed with.
    method: str = "size_constancy"
    slate_template_id: uuid.UUID | None = None


async def persist_automatic_calibration(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    row: AutomaticCalibrationRow,
) -> uuid.UUID:
    """Append the dive's label-free calibration (or its refusal); the frames
    it names must be the dive's. Returns its id."""
    if row.capture_ids:
        await _check_captures(conn, tenant_id, mission_id, row.capture_ids)
    values = asdict(row)
    for key in ("laser_position", "laser_axis", "line_direction"):
        values[key] = None if values[key] is None else json.dumps(values[key])
    return (
        await conn.execute(
            text("""
                INSERT INTO automatic_laser_calibrations (
                    tenant_id, mission_id, outcome, algorithm_version, refusal_reason,
                    camera_calibration_id, laser_position, laser_axis, vanishing_px,
                    line_direction, line_offset_px, o_mag_m, frames_used,
                    candidate_count, pair_count, size_ratio, se_px,
                    pair_residual_sd, capture_ids, core_version, method,
                    slate_template_id)
                VALUES (:t, :d, :outcome, :algorithm_version, :refusal_reason,
                        :camera_calibration_id, CAST(:laser_position AS jsonb),
                        CAST(:laser_axis AS jsonb), :vanishing_px,
                        CAST(:line_direction AS jsonb), :line_offset_px, :o_mag_m,
                        :frames_used, :candidate_count, :pair_count, :size_ratio,
                        :se_px, :pair_residual_sd, :capture_ids, :core_version,
                        :method, :slate_template_id)
                RETURNING id
                """),
            {"t": tenant_id, "d": mission_id, **values},
        )
    ).scalar_one()


# -- measurement --------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementCalibration:
    """The calibration the dive's automatic lengths use now."""

    source: str
    automatic_laser_calibration_id: uuid.UUID | None
    laser_calibration_id: uuid.UUID | None
    laser_position: list[float]
    laser_axis: list[float]


@dataclass(frozen=True)
class AutomaticMeasureCapture:
    capture_id: uuid.UUID
    automatic_head_tail_prediction_id: uuid.UUID
    laser_x: float
    laser_y: float
    head_x: float
    head_y: float
    tail_x: float
    tail_y: float
    #: The keypoints these points are (v5); None: the SAM rule's, a model's.
    automatic_keypoint_prediction_id: uuid.UUID | None = None


@dataclass(frozen=True)
class AutomaticMeasureRefusal:
    """A frame its keypoints sent to review (v5): refused, not measured."""

    capture_id: uuid.UUID
    automatic_head_tail_prediction_id: uuid.UUID
    automatic_keypoint_prediction_id: uuid.UUID
    reason: str


@dataclass(frozen=True)
class AutomaticMeasureInputs:
    calibration: MeasurementCalibration | None
    camera_calibration_id: uuid.UUID | None
    camera_matrix: list[list[float]] | None
    captures: list[AutomaticMeasureCapture]
    refusals: list[AutomaticMeasureRefusal] = field(default_factory=list)


async def automatic_measure_inputs(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    *,
    algorithm_version: str,
    slate_frames: SlateFrames = no_slate_frames,
) -> AutomaticMeasureInputs:
    """The dive's predicted fish (never a slate frame, by its row or by the
    detector now) with no current automatic length at the current version,
    and the calibration to measure them with; nothing to measure without one."""
    cal = (
        await conn.execute(
            text("""
                SELECT * FROM automatic_measurement_calibrations
                WHERE tenant_id = :t AND mission_id = :d
                """),
            {"t": tenant_id, "d": mission_id},
        )
    ).one_or_none()
    if cal is None:
        return AutomaticMeasureInputs(None, None, None, [])
    rows = await conn.execute(
        text(AUTOMATIC_MEASURE_CAPTURES),
        {"t": tenant_id, "d": mission_id, "mv": algorithm_version},
    )
    slate = await _slate_probabilities(slate_frames, conn, tenant_id, mission_id)
    rows = [r for r in rows if slate.get(r.capture_id, 0.0) < SLATE_FRAME_THRESHOLD]
    captures, refusals = [], []
    for r in rows:
        if r.model_frame:
            captures.append(AutomaticMeasureCapture(
                r.capture_id, r.id, r.laser_x, r.laser_y,
                r.head_x, r.head_y, r.tail_x, r.tail_y))  # fmt: skip
        elif r.keypoint_status == "predicted":
            captures.append(AutomaticMeasureCapture(
                r.capture_id, r.id, r.laser_x, r.laser_y, r.k_head_x, r.k_head_y,
                r.k_tail_x, r.k_tail_y, r.keypoint_id))  # fmt: skip
        else:
            refusals.append(AutomaticMeasureRefusal(
                r.capture_id, r.id, r.keypoint_id, r.keypoint_status))  # fmt: skip
    return AutomaticMeasureInputs(
        calibration=MeasurementCalibration(
            source=cal.calibration_source,
            automatic_laser_calibration_id=cal.automatic_laser_calibration_id,
            laser_calibration_id=cal.laser_calibration_id,
            laser_position=[float(v) for v in cal.laser_position],
            laser_axis=[float(v) for v in cal.laser_axis],
        ),
        camera_calibration_id=cal.camera_calibration_id,
        camera_matrix=[[float(x) for x in r] for r in cal.camera_matrix],
        captures=captures,
        refusals=refusals,
    )


@dataclass(frozen=True)
class AutomaticMeasurementRow:
    capture_id: uuid.UUID
    automatic_head_tail_prediction_id: uuid.UUID
    calibration_source: str
    algorithm: str
    algorithm_version: str
    core_version: str
    automatic_laser_calibration_id: uuid.UUID | None = None
    laser_calibration_id: uuid.UUID | None = None
    camera_calibration_id: uuid.UUID | None = None
    length_m: float | None = None
    depth_m: float | None = None
    refusal: str | None = None
    #: The keypoints measured from (v5); None: the SAM rule's.
    automatic_keypoint_prediction_id: uuid.UUID | None = None


async def persist_automatic_measurements(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    mission_id: uuid.UUID,
    rows: Sequence[AutomaticMeasurementRow],
) -> int:
    """Append automatic lengths (and refusals); each must name its own
    capture's automatic head/tail."""
    rows = list(rows)
    if not rows:
        return 0
    await _check_captures(conn, tenant_id, mission_id, [r.capture_id for r in rows])
    await _check_head_tails(conn, tenant_id, rows)
    await _check_keypoints(conn, tenant_id, rows)
    return len(await _insert(conn, "automatic_measurements", tenant_id, rows))


async def _check_keypoints(conn, tenant_id, rows) -> None:
    """Each length's keypoints are its own head/tail's."""
    named = {r.automatic_keypoint_prediction_id for r in rows} - {None}
    if not named:
        return
    owner = dict(
        (await conn.execute(text("""
            SELECT id, automatic_head_tail_prediction_id
            FROM automatic_keypoint_predictions WHERE tenant_id = :t AND id = ANY(:ids)
            """), {"t": tenant_id, "ids": list(named)})).all()
    )  # fmt: skip
    for r in rows:
        k = r.automatic_keypoint_prediction_id
        if k is not None and owner.get(k) != r.automatic_head_tail_prediction_id:
            raise ForeignHeadTail(
                f"automatic keypoints {k} are not head/tail "
                f"{r.automatic_head_tail_prediction_id}'s"
            )


class AutomaticResultsCatalog(ServicePrincipal):
    """The automatic-results track's database side, as the orchestrator's
    service principal. `slate_frames` is the slate-presence detector's store
    function (`slate_presence_store.slate_frames` in production)."""

    def __init__(
        self, engine, *, sub: str, slate_frames: SlateFrames = no_slate_frames
    ):
        super().__init__(engine, sub=sub)
        self._slate_frames = slate_frames

    async def next_mission_for_automatic_results(
        self, tenant_id: uuid.UUID, **versions
    ) -> AutomaticCandidate | None:
        async with self._tenant(tenant_id) as conn:
            return await next_mission_for_automatic_results(conn, tenant_id, **versions)

    async def record_automatic_results_failure(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, *, step: str, error: str
    ) -> None:
        async with self._tenant(tenant_id) as conn:
            await record_automatic_results_failure(
                conn, tenant_id, mission_id, step=step, error=error
            )

    async def automatic_run_plan(
        self, tenant_id: uuid.UUID, numbers: Sequence[int], *, headtail_version: int
    ) -> AutomaticRunPlan:
        async with self._tenant(tenant_id) as conn:
            return await automatic_run_plan(
                conn, tenant_id, numbers, headtail_version=headtail_version
            )

    async def tenant_by_slug(self, slug: str) -> uuid.UUID | None:
        """A member tenant's id by its slug (the operator names tenants so)."""
        for tenant_id in await self.member_tenants():
            async with self._tenant(tenant_id) as conn:
                found = (
                    await conn.execute(
                        text("SELECT slug FROM tenants WHERE id = :t"), {"t": tenant_id}
                    )
                ).scalar_one_or_none()
            if found == slug:
                return tenant_id
        return None

    async def record_automatic_frame_failures(
        self,
        tenant_id: uuid.UUID,
        mission_id: uuid.UUID,
        failures: Sequence[tuple[uuid.UUID, str]],
        *,
        headtail_version: int,
    ) -> list[uuid.UUID]:
        async with self._tenant(tenant_id) as conn:
            return await record_automatic_frame_failures(
                conn, tenant_id, mission_id, failures, headtail_version=headtail_version
            )

    async def automatic_frames_inputs(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, *, headtail_version: int
    ) -> AutomaticFramesInputs:
        async with self._tenant(tenant_id) as conn:
            return await automatic_frames_inputs(
                conn, tenant_id, mission_id, headtail_version=headtail_version,
                slate_frames=self._slate_frames,
            )  # fmt: skip

    async def persist_automatic_head_tails(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, rows, masks=()
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await persist_automatic_head_tails(
                conn, tenant_id, mission_id, rows, masks
            )

    async def automatic_keypoint_inputs(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID
    ) -> list[AutomaticKeypointFrame]:
        async with self._tenant(tenant_id) as conn:
            return await automatic_keypoint_inputs(conn, tenant_id, mission_id)

    async def persist_automatic_keypoints(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, rows
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await persist_automatic_keypoints(conn, tenant_id, mission_id, rows)

    async def automatic_species_captures(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, *, species_version: int
    ) -> list[AutomaticSpeciesCapture]:
        async with self._tenant(tenant_id) as conn:
            return await automatic_species_captures(
                conn, tenant_id, mission_id, species_version=species_version
            )

    async def persist_automatic_species(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, rows
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await persist_automatic_species(conn, tenant_id, mission_id, rows)

    async def missions_with_automatic_dots(self, tenant_id):
        async with self._tenant(tenant_id) as conn:
            return await missions_with_automatic_dots(conn, tenant_id)

    async def automatic_calibration_inputs(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID
    ) -> AutomaticCalibrationInputs:
        async with self._tenant(tenant_id) as conn:
            return await automatic_calibration_inputs(
                conn, tenant_id, mission_id, slate_frames=self._slate_frames
            )

    async def automatic_slate_pose_owed(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, *, version: str
    ) -> bool:
        async with self._tenant(tenant_id) as conn:
            return await automatic_slate_pose_owed(
                conn, tenant_id, mission_id, version=version
            )

    async def persist_automatic_calibration(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, row: AutomaticCalibrationRow
    ) -> uuid.UUID:
        async with self._tenant(tenant_id) as conn:
            return await persist_automatic_calibration(conn, tenant_id, mission_id, row)

    async def automatic_measure_inputs(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, *, algorithm_version: str
    ) -> AutomaticMeasureInputs:
        async with self._tenant(tenant_id) as conn:
            return await automatic_measure_inputs(
                conn, tenant_id, mission_id, algorithm_version=algorithm_version,
                slate_frames=self._slate_frames,
            )  # fmt: skip

    async def persist_automatic_measurements(
        self, tenant_id: uuid.UUID, mission_id: uuid.UUID, rows
    ) -> int:
        async with self._tenant(tenant_id) as conn:
            return await persist_automatic_measurements(
                conn, tenant_id, mission_id, rows
            )
