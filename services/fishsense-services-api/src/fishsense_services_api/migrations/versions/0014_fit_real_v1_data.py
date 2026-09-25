"""Correct two constraints that real v1 data showed to be wrong.

Profiling the 2026-09-25 production dump against the schema found:

- ``dive_laser_lines.line_confidence`` is an unbounded stability signal (v1's
  values run ~2.6 to ~270 000), not a probability; 0009 bounded it to [0, 1].
  It stays non-negative.
- ``laser_predictions.gate_verdict`` also takes ``auto_accepted``, which
  production holds but the schema inventory missed (0011).

A new migration rather than an edit, because 0009 and 0011 are already
published and a database may already be at them.

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision = "0014"
down_revision = "0013"

GATE_VERDICTS = (
    "auto_accepted",
    "off_line",
    "along_line_outlier",
    "audit_sample",
    "dive_ineligible",
    "no_prediction",
)


def upgrade() -> None:
    op.execute("""
        ALTER TABLE dive_laser_lines
            DROP CONSTRAINT dive_laser_lines_line_confidence_check,
            ADD CONSTRAINT dive_laser_lines_line_confidence_check
                CHECK (line_confidence >= 0)
        """)
    verdicts = ", ".join(f"'{v}'" for v in GATE_VERDICTS)
    op.execute(f"""
        ALTER TABLE laser_predictions
            DROP CONSTRAINT laser_predictions_gate_verdict_check,
            ADD CONSTRAINT laser_predictions_gate_verdict_check
                CHECK (gate_verdict IN ({verdicts}))
        """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE laser_predictions
            DROP CONSTRAINT laser_predictions_gate_verdict_check,
            ADD CONSTRAINT laser_predictions_gate_verdict_check
                CHECK (gate_verdict IN ('off_line', 'along_line_outlier',
                                        'audit_sample', 'dive_ineligible',
                                        'no_prediction'))
        """)
    op.execute("""
        ALTER TABLE dive_laser_lines
            DROP CONSTRAINT dive_laser_lines_line_confidence_check,
            ADD CONSTRAINT dive_laser_lines_line_confidence_check
                CHECK (line_confidence BETWEEN 0 AND 1)
        """)
