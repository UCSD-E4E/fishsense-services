"""`dive_pipeline_status` knows the port (PLAN.md §4.9, 2b-2).

Only missions behind the corrective optic are processed: every stage that
rectifies a frame does it with pinhole maths, which holds only there (the
owner, 2026-10-20). The selectors took the port term with 2b-2
(`camera_sql`). The function behind `dive_pipeline_status` keeps its body as
text, frozen when 0046 recreated it, so its ``*_pending`` columns still
showed a mission off the corrective optic as queued while no selector would
ever take it. Recreated here with the same terms the stores now read:

* ``CORRECTIVE_OPTIC`` for the stages whose pending column already asked for a
  pinhole camera (laser 0.1 and prediction, species 2, head/tail 5.1) and for
  clustering;
* ``RECTIFIABLE_MISSION`` (the port and a pinhole camera) for slate
  preprocessing and both laser calibrations, which asked only for some
  camera calibration;
* ``ADMITTED_PORT`` (the mission and the mission it borrows a calibration
  from) for laser depth and measurement.

The terms are spelled here, frozen as this migration ran, and
tests/test_mission_pipeline_status_view.py pins them to `camera_sql`: when
the stores' terms change, that test fails, and the function needs recreating
again. The outputs (Superset's columns) are unchanged.

Revision ID: 0051
Revises: 0050
"""

import importlib.util
import re
from pathlib import Path

from alembic import op

revision = "0051"
down_revision = "0050"


def _migration(filename: str):
    spec = importlib.util.spec_from_file_location(
        f"_fishsense_migration_{Path(filename).stem}",
        Path(__file__).with_name(filename),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_0046 = _migration("0046_dives_become_missions.py")

CORRECTIVE_OPTIC = "d.port = 'corrective_optic'"
ADMITTED_PORT = f"""({CORRECTIVE_OPTIC} AND NOT EXISTS (
    SELECT 1 FROM missions src
    WHERE src.tenant_id = d.tenant_id AND src.id = d.calibration_source_mission_id
      AND src.port <> 'corrective_optic'
))"""
RECTIFIABLE_MISSION = f"""({CORRECTIVE_OPTIC} AND EXISTS (
    SELECT 1 FROM current_camera_calibrations cc
    WHERE cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
      AND cc.camera_model = 'pinhole'
))"""

#: Each pending column's port term.
PENDING_TERMS = {
    "laser_preprocess_pending": CORRECTIVE_OPTIC,
    "laser_prediction_pending": CORRECTIVE_OPTIC,
    "clustering_pending": CORRECTIVE_OPTIC,
    "species_preprocess_pending": CORRECTIVE_OPTIC,
    "headtail_preprocess_pending": CORRECTIVE_OPTIC,
    "headtail_prediction_pending": CORRECTIVE_OPTIC,
    "slate_preprocess_pending": RECTIFIABLE_MISSION,
    "laser_calibration_pending": RECTIFIABLE_MISSION,
    "checkerboard_calibration_pending": RECTIFIABLE_MISSION,
    "laser_depth_pending": ADMITTED_PORT,
    "measurement_pending": ADMITTED_PORT,
}

#: A pending column, as 0029 rendered it: ``((d.priority = 'high' AND ...)) AS x``.
_PENDING = re.compile(
    r"\(\(d\.priority = 'high' AND (?P<cohort>.*?)\)\) AS (?P<name>\w+_pending)\b",
    re.DOTALL,
)


def _with_port(sql: str) -> str:
    seen = []

    def gate(m: re.Match) -> str:
        name = m.group("name")
        seen.append(name)
        term = PENDING_TERMS[name]
        return f"((d.priority = 'high' AND {term} AND {m.group('cohort')})) AS {name}"

    out = _PENDING.sub(gate, sql)
    assert sorted(seen) == sorted(PENDING_TERMS), seen
    return out


FUNCTION_SQL = _with_port(_0046.FUNCTION_SQL)
#: What `FUNCTION_SQL` rendered to; the status view's tests pin it.
RENDERED_SHA256 = "7a28d5ac02f4a5c4affe4e5b47d188c30b8ae0e511ec803241e19810d8942274"


def upgrade() -> None:
    op.execute(FUNCTION_SQL)


def downgrade() -> None:
    op.execute(_0046.FUNCTION_SQL)
