"""Which render of the raw an automatic frame's dot and mask were found in.

The automatic frames stage rectifies each raw into a JPEG, finds the dot and
the mask against it, and writes it under the render's key: the render version
and the camera calibration it rectified with (`automatic_jpeg_r{N}_{camera}`).
Species crops it and the label-free calibration takes its features from it, so
both must read that same image -- never the head/tail JPEG, which for a
migrated frame is v1's render (2026-10-19). Recording both on the row says
which image the row's pixels belong to, and which frames to re-render when the
rendering or the camera changes. NULL: no render recorded (written before
renders were, or the raw would not decode); those frames have no automatic
JPEG. The version and the camera go together, and the camera is one of the
tenant's calibrations.

``current_automatic_head_tail_predictions`` selects ``*``, which Postgres
expanded when 0036 created it, so it is replaced to carry the new columns.
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
VERSION_CHECK = "automatic_head_tail_predictions_jpeg_render_version_check"
PAIRED_CHECK = "automatic_head_tail_predictions_jpeg_render_check"
CAMERA_FK = "automatic_head_tail_predictions_jpeg_camera_calibration_fkey"


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
            ADD COLUMN jpeg_render_version integer
                CONSTRAINT {VERSION_CHECK} CHECK (jpeg_render_version > 0),
            ADD COLUMN jpeg_camera_calibration_id uuid,
            ADD CONSTRAINT {PAIRED_CHECK} CHECK (
                (jpeg_render_version IS NULL) = (jpeg_camera_calibration_id IS NULL)),
            ADD CONSTRAINT {CAMERA_FK}
                FOREIGN KEY (tenant_id, jpeg_camera_calibration_id)
                REFERENCES camera_calibrations (tenant_id, id)
        """)
    # OR REPLACE keeps the view's grants; the new columns are appended last.
    _view("CREATE OR REPLACE")


def downgrade() -> None:
    app_role = op.get_bind().dialect.identifier_preparer.quote(
        context.config.attributes["app_role"]
    )
    op.execute(f"DROP VIEW {VIEW}")
    op.execute(f"""
        ALTER TABLE {TABLE}
            DROP COLUMN jpeg_camera_calibration_id,
            DROP COLUMN jpeg_render_version
        """)
    _view("CREATE")
    op.execute(f"GRANT SELECT ON {VIEW} TO {app_role}, {RESEARCH_ROLE}")
