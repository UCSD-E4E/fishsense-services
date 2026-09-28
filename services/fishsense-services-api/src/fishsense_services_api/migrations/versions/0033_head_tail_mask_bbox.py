"""Head/tail predictions: the kept mask's box.

New in v2 (no v1 counterpart). The head/tail predictor keeps one SAM 3.1 mask,
the fish at the laser dot; contract 5 adds its box to the result
(`HeadtailPredictionResult.mask_bbox`, ``[x_min, y_min, x_max, y_max)`` in
rectified-frame pixels), and the BioCLIP species pre-annotation stage crops by
it (0034). Additive: nullable, NULL where no mask was kept and on every row
written before it.

`current_head_tail_predictions` is left as it is: a view's ``*`` was expanded
when 0011 created it, and widening it would pin the column there for good (a
replace may only append columns, so a downgrade could not drop it). Readers
of the box join the row by id (`species_prediction_store`).

Revision ID: 0033
Revises: 0032
"""

from alembic import op

revision = "0033"
down_revision = "0032"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE head_tail_predictions
            ADD COLUMN mask_bbox integer[]
            CONSTRAINT head_tail_predictions_mask_bbox_check CHECK (
                mask_bbox IS NULL
                OR (array_ndims(mask_bbox) = 1 AND cardinality(mask_bbox) = 4
                    AND array_position(mask_bbox, NULL) IS NULL)
            )
        """)


def downgrade() -> None:
    op.execute("ALTER TABLE head_tail_predictions DROP COLUMN mask_bbox")
