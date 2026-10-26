"""The automatic slate-pose calibration (the owner, 2026-10-26).

Calibration v4 poses each slate frame from its printed template (SAM board
outline, the template page aligned by ECC, production's plane from the
template's reference points) and fits the beam through the dots on those
planes. It ran only as a read-only replay; the owner decided to use it for
automatic lengths, behind a switch that stays off until a read-only
validation on every mission has been reviewed. Mission 436, its one
"failure", turned out to be production's error: 11 mirrored human labels
(0077).

* ``automatic_laser_calibrations.method`` may be ``slate_pose`` (0036 allowed
  only ``size_constancy``), and such a row names the template it posed with
  (``slate_template_id``; only on a slate-pose row).
* ``current_automatic_laser_calibrations`` is the latest row per mission and
  method: a mission keeps its size-constancy fit beside its slate-pose one.
  Same columns.
* ``automatic_measurement_calibrations``: a mission's lengths use, of the
  accepted calibrations, its own slate pose, then its own size constancy,
  then its calibration link's slate pose, then the link's size constancy
  (the owner: the slate first). Same columns.

The downgrade restores 0077's views and constraint; it fails while a
slate-pose calibration exists, rather than delete history.

Revision ID: 0078
Revises: 0077
"""

from alembic import op
from sqlalchemy import text

revision = "0078"
down_revision = "0077"

TABLE = "automatic_laser_calibrations"
_METHOD_CHECK = f"{TABLE}_method_check"
_TEMPLATE_CHECK = f"{TABLE}_slate_template_check"

_CURRENT_BEFORE = "SELECT DISTINCT ON (tenant_id, mission_id) id,"
_CURRENT_AFTER = "SELECT DISTINCT ON (tenant_id, mission_id, method) id,"
_ORDER_BEFORE = "ORDER BY tenant_id, mission_id, seq DESC"
_ORDER_AFTER = "ORDER BY tenant_id, mission_id, method, seq DESC"
_PICK_BEFORE = "ORDER BY (a.mission_id = d.id) DESC"
_PICK_AFTER = (
    "ORDER BY (a.mission_id = d.id) DESC, (a.method = 'slate_pose'::text) DESC"
)


def _edit_view(view: str, *edits: tuple[str, str]) -> None:
    """Rewrite `view` in place from the definition and options Postgres holds
    (CREATE OR REPLACE drops options it isn't given: security_invoker)."""
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
    body = held.definition
    for old, new in edits:
        if body.count(old) != 1:
            raise RuntimeError(f"{view}: expected one {old!r} in its definition")
        body = body.replace(old, new)
    options = f" WITH ({', '.join(held.reloptions)})" if held.reloptions else ""
    op.execute(f"CREATE OR REPLACE VIEW {view}{options} AS {body.rstrip(';')}")


def _method_check(methods) -> None:
    listed = ", ".join(f"'{m}'" for m in methods)
    op.execute(f"""
        ALTER TABLE {TABLE}
            DROP CONSTRAINT {_METHOD_CHECK},
            ADD CONSTRAINT {_METHOD_CHECK} CHECK (method IN ({listed}))
        """)


def upgrade() -> None:
    _method_check(("size_constancy", "slate_pose"))
    op.execute(f"""
        ALTER TABLE {TABLE}
            ADD COLUMN slate_template_id uuid REFERENCES slate_templates (id),
            ADD CONSTRAINT {_TEMPLATE_CHECK}
                CHECK ((method = 'slate_pose') = (slate_template_id IS NOT NULL))
        """)
    _edit_view(
        "current_automatic_laser_calibrations",
        (_CURRENT_BEFORE, _CURRENT_AFTER),
        (_ORDER_BEFORE, _ORDER_AFTER),
    )
    _edit_view("automatic_measurement_calibrations", (_PICK_BEFORE, _PICK_AFTER))


def downgrade() -> None:
    posed = (
        op.get_bind()
        .execute(text(f"SELECT count(*) FROM {TABLE} WHERE method = 'slate_pose'"))
        .scalar_one()
    )
    if posed:
        raise RuntimeError(
            f"{posed} slate-pose calibrations exist: 0077 cannot hold them"
        )
    _edit_view("automatic_measurement_calibrations", (_PICK_AFTER, _PICK_BEFORE))
    _edit_view(
        "current_automatic_laser_calibrations",
        (_CURRENT_AFTER, _CURRENT_BEFORE),
        (_ORDER_AFTER, _ORDER_BEFORE),
    )
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN slate_template_id")
    _method_check(("size_constancy",))
