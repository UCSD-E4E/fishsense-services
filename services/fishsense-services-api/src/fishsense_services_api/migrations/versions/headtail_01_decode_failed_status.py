"""Head/tail predictions: the `decode_failed` abstention v1 emits.

v1's predictor (fishsense-lite@77e8f8e5 predict_headtail_image.py
`predict_from_jpeg`) abstains with `decode_failed` on a JPEG it cannot decode.
Migration 0011's status CHECK left it out, so the insert would fail, and an
abstention that cannot be recorded is re-predicted every hour: the cohort
selects on the row's absence. Additive: the CHECK only widens.

Revision ID: headtail_01
Revises: 0020
"""

from alembic import op

revision = "headtail_01"
down_revision = "0020"

_OLD = ("predicted", "no_detections", "laser_off_all_fish", "headtail_failed")
_NEW = (*_OLD, "decode_failed")


def _check(statuses: tuple[str, ...]) -> None:
    listed = ", ".join(f"'{s}'" for s in statuses)
    op.execute(
        "ALTER TABLE head_tail_predictions "
        "DROP CONSTRAINT head_tail_predictions_status_check"
    )
    op.execute(f"""
        ALTER TABLE head_tail_predictions
            ADD CONSTRAINT head_tail_predictions_status_check
            CHECK (status IN ({listed}))
        """)


def upgrade() -> None:
    _check(_NEW)


def downgrade() -> None:
    _check(_OLD)
