"""When a dive's declared calibration target last changed: how a refusal expires.

v1 (fishsense-lite@77e8f8e5 services/fishsense-api/src/fishsense_api/
controllers/dive_controller.py, `set_dive_slate`, `set_dive_calibration_target`
and `_clear_refusal`) cleared a dive's calibration refusal -- three columns on
``dive`` -- whenever the species label sync wrote the dive's slate template or
calibration target: a newly declared target makes the dive plausibly fittable
again, in a way the label timestamps the refusal otherwise expires by cannot
see. The sync is the only writer of either link, so leaving the clear to an
operator would strand a corrected dive outside the calibration cohorts with no
signal at all.

v2 cannot clear anything: a refusal is an append-only ``laser_calibrations``
row (0008), and the app role may not UPDATE or DELETE one. So the change is
recorded on the dive instead, and the refusal **expires** by comparison:

    a dive's current refused calibration row stands only while
    ``dives.calibration_links_changed_at`` is NULL or earlier than the
    refusal's ``created_at``.

Both sides are the database's clock (``now()``), never Label Studio's, so the
comparison is between like and like (v1's warning about mixing clocks is about
the label-timestamp expiry, which ``inputs_as_of`` carries).

The species sync stamps it every time it writes either link, as v1 cleared on
every write. NULL -- every migrated dive -- means "never changed since
migration", and v1's refusals arrive already reflecting v1's clears.

Additive; ``dives`` is already tenant-scoped (0005), so no policy or grant
changes.

Revision ID: species_01
Revises: 0020
"""

from alembic import op

revision = "species_01"
down_revision = "0020"


def upgrade() -> None:
    op.execute("ALTER TABLE dives ADD COLUMN calibration_links_changed_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE dives DROP COLUMN calibration_links_changed_at")
