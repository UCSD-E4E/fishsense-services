"""The laser stage is v3 (2026-10-24): ``dive_pipeline_status_rows()`` is
recreated so its laser stale term compares against 3.

``LASER_PREDICTOR_VERSION`` went 2 -> 3 because the colour rule is one of its
inputs and it changed: the colour is now fishsense-core 4.3.0's
`laser_color`, which scales a saturated dot's margin by the brightest
*usable* pixel rather than the blown-out core, so some saturated abstentions
now vote (core measured 6 of 1,600 synthetic dots, 5 right and 1 wrong;
unsaturated dots are identical).

The function's body is frozen text (0029), so 0063's rendering still says
``IS DISTINCT FROM 2`` in the laser cohort's stale term, and the view's
``laser_prediction_pending`` would disagree with the store's selector
(`laser_store.laser_prediction_cohort`, which binds the version) from the
moment the bump deploys. This recreates 0063's function with that one term
changed. The head/tail cohort's ``IS DISTINCT FROM 2`` is
``HEADTAIL_PREDICTOR_VERSION``, unchanged, and stays.

``CREATE OR REPLACE FUNCTION`` keeps the function's owner, grants and
settings (JIT off), and the view over it is untouched, so the app's and
Superset's (0030) reads go on as before. The downgrade restores 0063's body
exactly. Pinned by tests/test_mission_pipeline_status_view.py and
tests/test_migration_0068.py.

Revision ID: 0068
Revises: 0067
"""

import importlib.util
from pathlib import Path

from alembic import op

revision = "0068"
down_revision = "0067"


def _migration(filename: str):
    spec = importlib.util.spec_from_file_location(
        f"_fishsense_migration_{Path(filename).stem}",
        Path(__file__).with_name(filename),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_0063 = _migration("0063_reviews_and_skills.py")

#: The stages' predictor versions frozen into the function as of this
#: migration: the laser store's `LASER_PREDICTOR_VERSION` and the contracts'
#: `HEADTAIL_PREDICTOR_VERSION`. Pinned to both by
#: tests/test_mission_pipeline_status_view.py.
LASER_PREDICTOR_VERSION = 3
HEADTAIL_PREDICTOR_VERSION = 2

#: The laser cohort's stale term: the capture's current prediction's version.
_LASER_STALE = "ORDER BY p.seq DESC LIMIT 1\n) IS DISTINCT FROM {}"

PREVIOUS_FUNCTION_SQL = _0063.FUNCTION_SQL


def _replaced(sql: str, old: str, new: str, count: int) -> str:
    assert sql.count(old) == count, (old, sql.count(old))
    return sql.replace(old, new)


FUNCTION_SQL = _replaced(
    PREVIOUS_FUNCTION_SQL,
    _LASER_STALE.format(2),
    _LASER_STALE.format(LASER_PREDICTOR_VERSION),
    1,
)
RENDERED_SHA256 = "81b4d9cced25a57d0f50ba1016239dca2d16edf06cc4afaadf58121432cafe84"


def upgrade() -> None:
    op.execute(FUNCTION_SQL)


def downgrade() -> None:
    op.execute(PREVIOUS_FUNCTION_SQL)
