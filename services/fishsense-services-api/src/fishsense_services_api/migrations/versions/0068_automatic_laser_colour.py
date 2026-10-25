"""The automatic dot keeps its colour (contract v21, 2026-10-24).

The laser kernel reads the dot's colour off its pixels (``laser_predictions.
color``, 0003; fishsense-core's `laser_color` since 0067), and the automatic
frames stage called the same kernel and dropped the colour, so no automatic
row had one. ``automatic_head_tail_predictions`` gains it:

* ``laser_color``: ``red`` or ``green``, or NULL when the dot gave no reading
  -- and on every row written before this, which stays NULL until its frame
  is predicted again (keeping the colour changes no result, so it does not
  bump the predictor version);
* ``laser_color_margin``: how clearly, as the human path's ``color_margin``.

``current_automatic_head_tail_predictions`` selects ``*``, which Postgres
expanded when it was made, so it is replaced to carry them (0049's pattern;
OR REPLACE keeps its grants and appends the new columns).

Revision ID: 0068
Revises: 0067
"""

from alembic import context, op

from fishsense_services_api.schema_audit import RESEARCH_ROLE

revision = "0068"
down_revision = "0067"

TABLE = "automatic_head_tail_predictions"
VIEW = "current_automatic_head_tail_predictions"
COLOR_CHECK = "automatic_head_tail_predictions_laser_color_check"


def _view(create: str) -> None:
    op.execute(f"""
        {create} VIEW {VIEW} WITH (security_invoker = true) AS
            SELECT DISTINCT ON (tenant_id, capture_id) *
            FROM {TABLE}
            ORDER BY tenant_id, capture_id, seq DESC
        """)


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {TABLE}
            ADD COLUMN laser_color text
                CONSTRAINT {COLOR_CHECK} CHECK (laser_color IN ('red', 'green')),
            ADD COLUMN laser_color_margin double precision
        """)
    _view("CREATE OR REPLACE")


def downgrade() -> None:
    app_role = op.get_bind().dialect.identifier_preparer.quote(
        context.config.attributes["app_role"]
    )
    op.execute(f"DROP VIEW {VIEW}")
    op.execute(
        f"ALTER TABLE {TABLE} DROP COLUMN laser_color_margin, DROP COLUMN laser_color"
    )
    _view("CREATE")
    op.execute(f"GRANT SELECT ON {VIEW} TO {app_role}, {RESEARCH_ROLE}")
