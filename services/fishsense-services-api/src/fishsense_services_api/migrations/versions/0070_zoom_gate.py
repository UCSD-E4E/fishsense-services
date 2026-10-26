"""The zoom gate (owner, 2026-10-25): a frame shot zoomed in is neither
calibrated from nor measured.

The TG-6 (and TG-7) zoom 4.5-18 mm; the camera calibrations are at the 4.5 mm
wide end. A zoomed frame's laser dot sits on another image line and its depth
and length assume intrinsics it was not shot at. The focal lengths recorded
on 2026-10-25 (0069, 134,178 frames): 4,156 zoomed frames (2,047 canonical)
in 33 missions; none of the 3,314 human lengths on one, 152 automatic
lengths on one.

* ``focal_length_calibrated(focal)``: the one rule -- unknown (a phone, or not
  read) or the wide end (4.5 mm, to the EXIF rational's 0.01 mm) passes.
  Every gate site calls it: the stores' calibration and measurement inputs
  (laser_calibration_store, automatic_results_store) and the two work views
  here.
* ``measurement_work`` and ``laser_depth_work`` gain it after
  ``c.is_canonical``. Their text lives only in the database (0046 renamed
  their columns in place), so the definition Postgres holds is edited, not
  retyped. ``CREATE OR REPLACE VIEW`` keeps their owner and grants but not
  their options, so each view's options (``security_invoker``: RLS applies
  to the caller) are restated from what Postgres holds.
* ``automatic_measurements.refusal`` may be ``zoomed``: the CLI's
  ``withdraw-zoomed-lengths`` appends one after each current automatic length
  on a zoomed frame (append-only: the length stays in the history).

The downgrade restores all three; it fails while a ``zoomed`` refusal exists,
rather than delete history. Pinned by tests/test_migration_0070.py.

Revision ID: 0070
Revises: 0069
"""

from alembic import op
from sqlalchemy import text

revision = "0070"
down_revision = "0069"

#: The camera calibrations' focal length (the TG-6/TG-7 wide end), mm.
WIDE_END_MM = 4.5
GATE = "focal_length_calibrated(c.focal_length_mm)"
VIEWS = ("measurement_work", "laser_depth_work")
_ANCHOR = "WHERE c.is_canonical AND "

_REFUSALS = ("non_positive_depth", "non_finite_length", "zero_length")


def _refusal_check(reasons) -> None:
    listed = ", ".join(f"'{r}'" for r in reasons)
    op.execute(f"""
        ALTER TABLE automatic_measurements
            DROP CONSTRAINT automatic_measurements_refusal_check,
            ADD CONSTRAINT automatic_measurements_refusal_check
                CHECK (refusal IN ({listed}))
        """)


def _edit_view(view: str, old: str, new: str) -> None:
    held = (
        op.get_bind()
        .execute(
            text("""
                SELECT pg_get_viewdef(c.oid, true) AS definition, c.reloptions
                FROM pg_class c WHERE c.oid = CAST(:v AS regclass)
                """),
            {"v": view},
        )
        .one()
    )
    if held.definition.count(old) != 1:
        raise RuntimeError(f"{view}: expected one {old!r} in its definition")
    options = f" WITH ({', '.join(held.reloptions)})" if held.reloptions else ""
    body = held.definition.replace(old, new).rstrip(";")
    op.execute(f"CREATE OR REPLACE VIEW {view}{options} AS {body}")


def upgrade() -> None:
    op.execute(f"""
        CREATE FUNCTION focal_length_calibrated(focal double precision)
        RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
            SELECT focal IS NULL OR abs(focal - {WIDE_END_MM}) <= 0.005
        $$
        """)
    for view in VIEWS:
        _edit_view(view, _ANCHOR, f"{_ANCHOR}{GATE} AND ")
    _refusal_check(_REFUSALS + ("zoomed",))


def downgrade() -> None:
    _refusal_check(_REFUSALS)
    for view in VIEWS:
        _edit_view(view, f"{GATE} AND ", "")
    op.execute("DROP FUNCTION focal_length_calibrated(double precision)")
