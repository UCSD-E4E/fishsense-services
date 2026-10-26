"""Head/tail v5's keypoints step can record its failure (2026-10-26).

0074 added a `keypoints` step to the automatic chain, but
`automatic_results_failures.step` still allowed only 0040's four. On the v5
rerun, mission 347's keypoints persist timed out, and recording it violated
the check, so the mission's run failed outright instead of taking a strike.
The check now allows `keypoints`; tests pin that every step the workflow runs
through `_step` is allowed.

Revision ID: 0075
Revises: 0074
"""

from alembic import op

revision = "0075"
down_revision = "0074"

_BEFORE = ("frames", "calibration", "lengths", "species")
_AFTER = ("frames", "keypoints", "calibration", "lengths", "species")


def _check(steps) -> None:
    listed = ", ".join(f"'{s}'" for s in steps)
    op.execute(f"""
        ALTER TABLE automatic_results_failures
            DROP CONSTRAINT automatic_results_failures_step_check,
            ADD CONSTRAINT automatic_results_failures_step_check
                CHECK (step IN ({listed}))
        """)


def upgrade() -> None:
    _check(_AFTER)


def downgrade() -> None:
    _check(_BEFORE)
