"""Backfilled laser colours for automatic dots (the owner, 2026-10-26).

Automatic dots predicted before contract v22 (0072) kept no colour: the
frames stage dropped what the laser kernel read. Head/tail v5 never runs the
dot step again, so nothing would fill them in. The backfill (contract v25,
`laser_colour_backfill`) reads each dot's colour off the render its
prediction was made on, at the dot, with fishsense-core's
`classify_laser_color` -- the kernel's rule on the 8-bit render instead of
the linear raw, so each reading says where it came from:

* ``automatic_laser_colour_readings``: append-only, tenant RLS, research read
  of the lab's rows (0074's pattern). One row per reading of an automatic
  head/tail prediction's dot: ``color`` (``red``/``green``; NULL when too
  close to call, or no reading was possible), ``color_margin`` (signed R-G in
  8-bit levels, core's), ``source`` (``render``) and ``reading_version``.
  The prediction row is never touched: a new prediction would make the
  current lengths stale, and results tables are append-only.
* ``current_automatic_laser_colour_readings``: the latest per prediction.

A dot's own colour (``automatic_head_tail_predictions.laser_color``, 0072)
always wins over a reading; the store writes a reading only for a dot that
is current and has none.

Revision ID: 0076
Revises: 0075
"""

from alembic import context, op

from fishsense_services_api.schema_audit import (
    RESEARCH_LAB_BINDING,
    RESEARCH_LAB_READ,
    RESEARCH_ROLE,
)

revision = "0076"
down_revision = "0075"

TABLE = "automatic_laser_colour_readings"
VIEW = f"current_{TABLE}"
ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_LAB = "tenant_id = (SELECT public.research_tenant_id())"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    app_role = _app_role()
    op.execute(f"""
        CREATE TABLE {TABLE} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            capture_id uuid NOT NULL,
            automatic_head_tail_prediction_id uuid NOT NULL,
            color text CONSTRAINT {TABLE}_color_check
                CHECK (color IN ('red', 'green')),
            color_margin double precision,
            source text NOT NULL CONSTRAINT {TABLE}_source_check
                CHECK (source IN ('render')),
            reading_version integer NOT NULL
                CONSTRAINT {TABLE}_reading_version_check CHECK (reading_version > 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, automatic_head_tail_prediction_id)
                REFERENCES automatic_head_tail_predictions (tenant_id, id),
            CONSTRAINT {TABLE}_colour_has_margin_check
                CHECK (color IS NULL OR color_margin IS NOT NULL)
        )
        """)
    op.execute(
        f"CREATE INDEX {TABLE}_tenant_prediction_idx "
        f"ON {TABLE} (tenant_id, automatic_head_tail_prediction_id, seq)"
    )
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {TABLE}
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO {app_role}")
    op.execute(
        f"CREATE POLICY {RESEARCH_LAB_READ} ON public.{TABLE} FOR SELECT "
        f"TO {RESEARCH_ROLE} USING ({_LAB})"
    )
    op.execute(
        f"CREATE POLICY {RESEARCH_LAB_BINDING} ON public.{TABLE} AS RESTRICTIVE "
        f"FOR SELECT TO {RESEARCH_ROLE} USING ({_LAB})"
    )
    op.execute(f"""
        CREATE VIEW {VIEW} WITH (security_invoker = true) AS
            SELECT DISTINCT ON (tenant_id, automatic_head_tail_prediction_id) *
            FROM {TABLE}
            ORDER BY tenant_id, automatic_head_tail_prediction_id, seq DESC
        """)
    op.execute(f"GRANT SELECT ON {VIEW} TO {app_role}")
    op.execute(f"GRANT SELECT ON {TABLE}, {VIEW} TO {RESEARCH_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP VIEW {VIEW}")
    op.execute(f"DROP TABLE {TABLE}")
