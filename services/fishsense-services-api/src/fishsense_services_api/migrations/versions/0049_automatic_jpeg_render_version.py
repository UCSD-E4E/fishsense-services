"""Which render of the raw an automatic frame's dot and mask were found in.

The automatic frames stage rectifies each raw into a JPEG, finds the dot and
the mask against it, and writes it under the render's version
(`automatic_jpeg_r{N}`). Species crops it and the label-free calibration takes
its features from it, so both must read that same image -- never the head/tail
JPEG, which for a migrated frame is v1's render (2026-10-19). Recording the
version on the row says which image the row's pixels belong to, and which
frames to re-render when the rendering changes. NULL: written before renders
were recorded; those frames have no automatic JPEG.

``current_automatic_head_tail_predictions`` selects ``*``, which Postgres
expanded when 0036 created it, so it is replaced to carry the new column.
Nothing is built on that view (the export reads the table).

Revision ID: 0049
Revises: 0048
"""

from alembic import context, op

from fishsense_services_api.schema_audit import RESEARCH_ROLE

revision = "0049"
down_revision = "0048"

TABLE = "automatic_head_tail_predictions"
VIEW = "current_automatic_head_tail_predictions"
CHECK = "automatic_head_tail_predictions_jpeg_render_version_check"


def _view(create: str) -> None:
    op.execute(f"""
        {create} VIEW {VIEW} WITH (security_invoker = true) AS
            SELECT DISTINCT ON (tenant_id, capture_id) *
            FROM {TABLE}
            ORDER BY tenant_id, capture_id, seq DESC
        """)


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {TABLE} ADD COLUMN jpeg_render_version integer
            CONSTRAINT {CHECK} CHECK (jpeg_render_version > 0)
        """)
    # OR REPLACE keeps the view's grants; the new column is appended last.
    _view("CREATE OR REPLACE")


def downgrade() -> None:
    app_role = op.get_bind().dialect.identifier_preparer.quote(
        context.config.attributes["app_role"]
    )
    op.execute(f"DROP VIEW {VIEW}")
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN jpeg_render_version")
    _view("CREATE")
    op.execute(f"GRANT SELECT ON {VIEW} TO {app_role}, {RESEARCH_ROLE}")
