"""A laser dot that fails fishsense-core's `is_measurable()` is a refusal
(contract v21, fishsense-core #96; owner-approved 2026-10-23).

The processor's laser geometry moved to fishsense-core
(`world_point.compute_laser_point`), and with it the gate: a dot is used only
if `LaserPoint.is_measurable()` -- every field finite, the depth in front of
the camera, and the camera ray at least 0.005 rad off the laser. The last is
new. Near the laser's vanishing point the triangulation returns a finite,
*positive*, untrustworthy depth (one pixel of dot error moves it more than 7 %
there), which the old `depth_m > 0` gate let through. Three CHECKs named the
old reasons and widen here:

- ``laser_depth_refusals`` (0025): `low_parallax` joins `non_finite_depth`
  and `non_positive_depth`. Its depth CHECK (`depth_m <= 0`: "never a usable
  depth, or it would have been one") holds for every other reason; a
  `low_parallax` refusal records the depth the solve gave, positive or not;
- ``measurement_refusals`` (0025): stage 14 now gates the dot before it
  measures (v1 measured at the dot's depth whatever its sign; the owner gave
  that parity up), so it takes the depth stage's three reasons too;
- ``automatic_measurements`` (0036): `low_parallax` joins its reasons.

Values added only, so every existing row still satisfies them. The downgrade
restores 0025's and 0036's definitions exactly, and fails -- rather than
deletes anything -- once a row holds a reason they do not name: these tables
are append-only history.

Revision ID: 0068
Revises: 0067
"""

from alembic import op

revision = "0068"
down_revision = "0067"

#: (table, constraint, 0025/0036's definition, 0068's).
_CHECKS = (
    (
        "laser_depth_refusals",
        "laser_depth_refusals_reason_check",
        "reason IN ('non_finite_depth', 'non_positive_depth')",
        "reason IN ('non_finite_depth', 'non_positive_depth', 'low_parallax')",
    ),
    (
        "laser_depth_refusals",
        "laser_depth_refusals_depth_m_check",
        "depth_m <= 0",
        "depth_m <= 0 OR reason = 'low_parallax'",
    ),
    (
        "measurement_refusals",
        "measurement_refusals_reason_check",
        "reason IN ('non_finite_length', 'zero_length', 'unparseable_species')",
        "reason IN ('non_finite_length', 'zero_length', 'unparseable_species',"
        " 'non_finite_depth', 'non_positive_depth', 'low_parallax')",
    ),
    (
        "automatic_measurements",
        "automatic_measurements_refusal_check",
        "refusal IN ('non_positive_depth', 'non_finite_length', 'zero_length')",
        "refusal IN ('non_positive_depth', 'non_finite_length', 'zero_length',"
        " 'low_parallax')",
    ),
)


def _replace(table: str, name: str, definition: str) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({definition})")


def upgrade() -> None:
    for table, name, _, widened in _CHECKS:
        _replace(table, name, widened)


def downgrade() -> None:
    for table, name, original, _ in reversed(_CHECKS):
        _replace(table, name, original)
