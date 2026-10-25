"""Each capture's focal length, for the zoom gate (owner, 2026-10-25).

The TG-6 zooms 4.5-18 mm and its intrinsics are calibrated at 4.5. A zoomed
frame's laser dots sit on another line, and its depth is wrong: missions 374,
384 and 386 mix 4.5 with up to 18 mm (EXIF read from the NAS, 484 frames of
the 63 V-Slate missions; 456 at 4.5). Nothing kept the focal length, so a
gate had nothing to read.

* ``captures.focal_length_mm``: EXIF FocalLength (0x920A). Ingest records it
  from now on; frames ingested before are read back from the NAS by
  ``ops.focal_lengths`` (a dry run first). NULL is unknown -- not the wide end
  -- and is what a phone capture keeps.

The gate itself (which frames calibration and measurement may use) is a later
migration, once the backfill has counted zoomed frames across every mission.

Revision ID: 0069
Revises: 0067
"""

from alembic import op

revision = "0069"
down_revision = "0067"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE captures ADD COLUMN focal_length_mm double precision "
        "CONSTRAINT captures_focal_length_mm_check CHECK (focal_length_mm > 0)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE captures DROP COLUMN focal_length_mm")
