"""A mission's purpose is what it was for, and a calibration board is not one.

0047 offered ``slate`` as a purpose. The owner, 2026-10-19: nearly every dive
has a calibration board in it, because every dive needs one to calibrate its
laser. So having a board says nothing about what a dive is for, and a single
purpose that recorded it would drop the rest: the paper's accuracy sessions
521 and 522 fit their own checkerboard calibration *and* hold the trout and
box frames, and the field dives 279-471 are surveys with their own slate.

* ``slate`` is no longer a purpose. A session that does nothing but calibrate
  (the 490 burst, ``082323_Slate_FSLxx``) is ``laser_calibration``; the board
  itself is the calibration target's business (``slate_template_id``,
  ``calibration_target_id``), as it always was.
* Nothing selects laser calibration by purpose: a stage finds the board in the
  frames. Purpose may select what a mission is *for* (the known-length
  accuracy set, the surveys).

Production has no purpose set yet (the 0047 backfill had not been applied,
2026-10-19), so nothing is relabelled; a ``slate`` row, were there one, would
become ``laser_calibration``.

**The downgrade does not undo that relabel.** It restores 0047's CHECK, but a
mission relabelled here stays ``laser_calibration``: nothing records which
were ``slate``, and on production there were none to record.

The purpose lists below are this migration's own, frozen as it ran; the live
list is ``mission_facts.PURPOSES``, pinned to the database's CHECK by
``test_the_databases_purposes_are_mission_facts``.

Revision ID: 0048
Revises: 0047
"""

from alembic import op

revision = "0048"
down_revision = "0047"

PURPOSES = ("survey", "lens_calibration", "laser_calibration", "known_length", "other")
OLD_PURPOSES = (
    "survey",
    "lens_calibration",
    "laser_calibration",
    "slate",
    "known_length",
    "other",
)


def _check(values) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"""
        ALTER TABLE missions ADD CONSTRAINT missions_purpose_check
            CHECK (purpose IN ({listed}))
        """


def upgrade() -> None:
    op.execute("ALTER TABLE missions DROP CONSTRAINT missions_purpose_check")
    op.execute(
        "UPDATE missions SET purpose = 'laser_calibration' WHERE purpose = 'slate'"
    )
    op.execute(_check(PURPOSES))


def downgrade() -> None:
    # Missions relabelled by upgrade() stay laser_calibration (see above).
    op.execute("ALTER TABLE missions DROP CONSTRAINT missions_purpose_check")
    op.execute(_check(OLD_PURPOSES))
