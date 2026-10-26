"""A slate label may say why it was superseded (the owner, 2026-10-26).

Mission 436's slate was held upside down on 11 of its 12 slate frames, and
those frames' labels are mirrored: their reference points are a mirror image
of the template (the affine map template -> clicks has a negative
determinant), which a board seen from its taped face cannot be. Of all 483
live human slate labels they are the only ones; the automatic template fit
keeps the template's handedness on all 11. Labels are superseded, never
deleted or moved, with the reason recorded -- as head/tail labels have since
0063.

* ``slate_labels.superseded_reason``: why it was retired; only on a
  superseded label. A label retired with a reason is settled: stage 9 does
  not draw its frame again, and re-anchoring its task does not revive it
  (``slate_store``). A dead-lettered label (superseded, no reason) is
  revived as before.

The downgrade drops the column, and with it the reasons.

Revision ID: 0077
Revises: 0076
"""

from alembic import op

revision = "0077"
down_revision = "0076"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE slate_labels
            ADD COLUMN superseded_reason text,
            ADD CONSTRAINT slate_labels_superseded_reason_check
                CHECK (superseded_reason IS NULL OR superseded)
        """)


def downgrade() -> None:
    op.execute("ALTER TABLE slate_labels DROP COLUMN superseded_reason")
