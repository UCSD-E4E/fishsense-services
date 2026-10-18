"""The automatic SAM gate refuses an unscored prediction.

0036's gate, ``status <> 'predicted' OR sam_score >= 0.5``, is NULL -- and so
passes -- for a ``predicted`` row whose ``sam_score`` is NULL: a mask nobody
showed scored >= 0.5 could be written as a prediction and measured (2026-10-15
gap audit). The gate now needs the score. The processor no longer produces
such a row either (an unscored mask is ``laser_off_all_fish``).

The table had no rows in production when this shipped (the chain had never
run), so the constraint is validated in place.

Revision ID: 0041
Revises: 0040
"""

from alembic import op

revision = "0041"
down_revision = "0040"

TABLE = "automatic_head_tail_predictions"
GATE = "automatic_head_tail_predictions_sam_gate_check"
SAM_SCORE_GATE = 0.5


def upgrade() -> None:
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {GATE}")
    op.execute(f"""
        ALTER TABLE {TABLE} ADD CONSTRAINT {GATE} CHECK (
            status <> 'predicted'
            OR (sam_score IS NOT NULL AND sam_score >= {SAM_SCORE_GATE}))
        """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {GATE}")
    op.execute(f"""
        ALTER TABLE {TABLE} ADD CONSTRAINT {GATE} CHECK (
            status <> 'predicted' OR sam_score >= {SAM_SCORE_GATE})
        """)
