"""What the validation harness reads, tenant-scoped and read-only.

New in v2. For the paper's dives (cscw-fishsense2027@96a8da07; by dive number)
the frames humans labelled, as the paper chose them:

* a canonical capture with a valid laser label and a valid head/tail label,
  the lowest-numbered of each (stage 14's choice, `measurement_work`);
* its species content (the live, highest-numbered species label): pool frames
  are fish models and the ruler (score.py: content matching ``Fish Model`` or
  ``Ruler``), the model being the content's last component, with its tape
  length from `current_fish_model_references`; reef frames are anything but a
  slate (tail/stage.py), labelled or not;
* green or red, from the laser label (evaluate.py);
* the camera matrix, the dive's effective stored calibration (0026's
  `dive_laser_geometry`) and its label-free one (own, else its link's);
* the automatic chain's current output for the frame
  (`current_automatic_head_tail_predictions`), unless `outputs` supplies it;
  then a frame `outputs` does not name was not run, and is left out (the
  paper sampled 200 frames of each of reef dives 362 and 366).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.automatic_validation import ValidationFrame

__all__ = ["validation_frames"]

_VALID_LASER = (
    "l.completed AND NOT l.superseded AND l.x IS NOT NULL AND l.y IS NOT NULL"
)
_VALID_HT = (
    "h.completed AND NOT h.superseded AND h.head_x IS NOT NULL AND h.head_y IS NOT NULL"
    " AND h.tail_x IS NOT NULL AND h.tail_y IS NOT NULL"
)

_FRAMES = text(f"""
    SELECT d.number AS dive_number, c.number AS capture_number,
           (SELECT s.number FROM dives s WHERE s.tenant_id = d.tenant_id
              AND s.id = d.calibration_source_dive_id) AS link_number,
           ll.x AS laser_x, ll.y AS laser_y, ll.label AS laser,
           ht.head_x, ht.head_y, ht.tail_x, ht.tail_y,
           sp.content_of_image AS content,
           cc.camera_matrix,
           g.laser_position AS stored_position, g.laser_axis AS stored_axis,
           lf.laser_position AS lf_position, lf.laser_axis AS lf_axis,
           a.status AS auto_status, a.laser_x AS auto_x, a.laser_y AS auto_y,
           a.head_x AS auto_head_x, a.head_y AS auto_head_y,
           a.tail_x AS auto_tail_x, a.tail_y AS auto_tail_y
    FROM dives d
    JOIN captures c ON c.tenant_id = d.tenant_id AND c.dive_id = d.id AND c.is_canonical
    JOIN current_camera_calibrations cc
      ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
     AND cc.camera_model = 'pinhole'
    CROSS JOIN LATERAL (
        SELECT l.x, l.y, l.label FROM laser_labels l
        WHERE l.tenant_id = c.tenant_id AND l.capture_id = c.id AND {_VALID_LASER}
        ORDER BY l.number LIMIT 1
    ) ll
    CROSS JOIN LATERAL (
        SELECT h.head_x, h.head_y, h.tail_x, h.tail_y FROM head_tail_labels h
        WHERE h.tenant_id = c.tenant_id AND h.capture_id = c.id AND {_VALID_HT}
        ORDER BY h.number LIMIT 1
    ) ht
    LEFT JOIN LATERAL (
        SELECT s.content_of_image FROM species_labels s
        WHERE s.tenant_id = c.tenant_id AND s.capture_id = c.id AND NOT s.superseded
          AND s.ls_project_id IS NOT NULL
        ORDER BY s.number DESC LIMIT 1
    ) sp ON true
    LEFT JOIN dive_laser_geometry g ON g.tenant_id = d.tenant_id AND g.dive_id = d.id
    LEFT JOIN LATERAL (
        SELECT x.laser_position, x.laser_axis
        FROM current_automatic_laser_calibrations x
        WHERE x.tenant_id = d.tenant_id AND x.outcome = 'accepted'
          AND x.dive_id IN (d.id, d.calibration_source_dive_id)
        ORDER BY x.dive_id = d.id DESC LIMIT 1
    ) lf ON true
    LEFT JOIN current_automatic_head_tail_predictions a
      ON a.tenant_id = c.tenant_id AND a.capture_id = c.id
    WHERE d.tenant_id = :t AND d.number = ANY(:dives)
    ORDER BY d.number, c.number
    """)


def _model(content: Optional[str]) -> Optional[str]:
    """score.py: a fish model or the ruler, named by the content's last part."""
    if content and ("Fish Model" in content or "Ruler" in content):
        return content.split(", ")[-1]
    return None


def _pair(v) -> Optional[tuple]:
    return None if v is None else tuple(float(x) for x in v)


def _calibration(position, axis):
    if position is None or axis is None:
        return None
    return ([float(v) for v in position], [float(v) for v in axis])


async def validation_frames(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    pool_dives: Sequence[int],
    reef_dives: Sequence[int],
    outputs: Optional[Mapping[int, Mapping[str, Any]]] = None,
    label_free: Optional[Mapping[int, tuple]] = None,
) -> list[ValidationFrame]:
    """The paper's frames. `outputs` (by capture number: ``auto_dot``,
    ``auto_head_tail``, ``auto_head_tail_humandot``) and `label_free` (by dive
    number, the dive's own or its calibration link's: ``(laser_position,
    laser_axis)``) replace the database's automatic
    results where given."""
    tape = {
        r.name: r.known_length_m
        for r in await conn.execute(
            text("SELECT name, known_length_m FROM current_fish_model_references")
        )
    }
    frames = []
    for r in await conn.execute(
        _FRAMES, {"t": tenant_id, "dives": [*pool_dives, *reef_dives]}
    ):
        is_pool = r.dive_number in pool_dives
        model = _model(r.content) if is_pool else None
        if is_pool and model is None:
            continue
        if not is_pool and (r.content or "").startswith("Slate"):
            continue
        auto_ht = (
            (r.auto_head_x, r.auto_head_y, r.auto_tail_x, r.auto_tail_y)
            if r.auto_status == "predicted"
            else None
        )
        given = (outputs or {}).get(r.capture_number)
        if outputs is not None:
            if given is None:
                continue  # never run: no evidence either way
            auto_dot = _pair(given.get("auto_dot"))
            auto_ht = _pair(given.get("auto_head_tail"))
            humandot = _pair(given.get("auto_head_tail_humandot"))
        else:
            auto_dot = None if r.auto_x is None else (r.auto_x, r.auto_y)
            humandot = None
        given_lf = label_free or {}
        lf = (
            given_lf.get(r.dive_number)
            or given_lf.get(r.link_number)
            or _calibration(r.lf_position, r.lf_axis)
        )
        frames.append(
            ValidationFrame(
                capture_number=r.capture_number,
                dive_number=r.dive_number,
                set="pool" if is_pool else "reef",
                model=model,
                true_length_m=tape.get(model) if model else None,
                green=(r.laser or "").startswith("Green"),
                camera_matrix=[[float(v) for v in row] for row in r.camera_matrix],
                human_dot=(r.laser_x, r.laser_y),
                human_head_tail=(r.head_x, r.head_y, r.tail_x, r.tail_y),
                auto_dot=auto_dot,
                auto_head_tail=auto_ht,
                stored_calibration=_calibration(r.stored_position, r.stored_axis),
                label_free_calibration=lf,
                auto_head_tail_humandot=humandot,
            )
        )
    return frames
