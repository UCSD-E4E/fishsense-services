"""When the laser moved mid-dive: the partial run (owner, 2026-10-25).

On 21 of the 63 V-Slate missions the laser dots lay on two parallel lines,
each over its own stretch of the dive: the beam's aim changed (a bumped or
loose laser). One calibration cannot serve both aims. The owner's rule: the
part of the dive before the move is calibrated and measured, the rest is
refused ("generally, we won't be able to repair the second half"). The
detector is `beam_moves.detect_beam_move` (corpus study: 36 of 258 missions;
human dots, as a control, contradict none).

* ``mission_beam_moves``: append-only, one row per detection (or an
  operator's clearing, with no window): the window the move happened in --
  after the last dot on the first aim, before the first on the second -- the
  detector's version and its evidence. ``current_mission_beam_moves`` is the
  latest row per mission. Recorded by the CLI's ``detect-beam-moves`` (a dry
  run first).
* ``before_beam_move(tenant, mission, captured_at)``: whether a frame is on
  the mission's first aim -- no current move, or captured no later than the
  move's ``moved_after``. Frames inside the window belong to neither aim and
  are out. Every gate site calls it beside ``focal_length_calibrated``
  (0070): the stores' calibration and measurement inputs, and the two work
  views here (edited in place, options restated, as 0070 did).
* ``automatic_measurements.refusal`` may be ``beam_moved``: the CLI's
  ``withdraw-moved-lengths`` appends one after each current automatic length
  on a frame after a move.

Tenant RLS, the app role reads and inserts. The downgrade restores 0072's
state; it fails while a ``beam_moved`` refusal exists, rather than delete
history. Pinned by tests/test_migration_0073.py.

Revision ID: 0073
Revises: 0072
"""

from alembic import context, op
from sqlalchemy import text

revision = "0073"
down_revision = "0072"

TABLE = "mission_beam_moves"
ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
FOCAL = "focal_length_calibrated(c.focal_length_mm)"
GATE = "before_beam_move(c.tenant_id, c.mission_id, c.captured_at)"
VIEWS = ("measurement_work", "laser_depth_work")
#: The automatic refusals as 0071 left them (0036's three, 0070's `zoomed`,
#: 0071's `low_parallax` and `non_finite_depth`).
_REFUSALS = ("non_positive_depth", "non_finite_length", "zero_length", "zoomed",
             "low_parallax", "non_finite_depth")  # fmt: skip


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


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
    app_role = _app_role()
    op.execute(f"""
        CREATE TABLE {TABLE} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            mission_id uuid NOT NULL,
            moved_after timestamptz,
            moved_before timestamptz,
            detector_version text NOT NULL,
            gain integer,
            angle_deg double precision,
            apart_px double precision,
            dots_before integer,
            dots_after integer,
            created_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, mission_id) REFERENCES missions (tenant_id, id),
            CONSTRAINT {TABLE}_window_check CHECK (
                (moved_after IS NULL) = (moved_before IS NULL)
                AND (moved_after IS NULL OR moved_after < moved_before))
        )
        """)
    op.execute(
        f"CREATE INDEX {TABLE}_tenant_mission_idx ON {TABLE} (tenant_id, mission_id)"
    )
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {TABLE}
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO {app_role}")
    op.execute(f"""
        CREATE VIEW current_{TABLE} WITH (security_invoker = true) AS
            SELECT DISTINCT ON (tenant_id, mission_id) *
            FROM {TABLE}
            ORDER BY tenant_id, mission_id, seq DESC
        """)
    op.execute(f"GRANT SELECT ON current_{TABLE} TO {app_role}")
    op.execute(f"""
        CREATE FUNCTION before_beam_move(p_tenant uuid, p_mission uuid,
                                         p_at timestamptz)
        RETURNS boolean LANGUAGE sql STABLE PARALLEL SAFE AS $$
            SELECT NOT EXISTS (
                SELECT 1 FROM public.current_{TABLE} b
                WHERE b.tenant_id = p_tenant AND b.mission_id = p_mission
                  AND b.moved_after IS NOT NULL AND p_at > b.moved_after)
        $$
        """)
    for view in VIEWS:
        _edit_view(view, f"{FOCAL} AND ", f"{FOCAL} AND {GATE} AND ")
    _refusal_check(_REFUSALS + ("beam_moved",))


def downgrade() -> None:
    _refusal_check(_REFUSALS)
    for view in VIEWS:
        _edit_view(view, f"{GATE} AND ", "")
    op.execute("DROP FUNCTION before_beam_move(uuid, uuid, timestamptz)")
    op.execute(f"DROP VIEW current_{TABLE}")
    op.execute(f"DROP TABLE {TABLE}")
