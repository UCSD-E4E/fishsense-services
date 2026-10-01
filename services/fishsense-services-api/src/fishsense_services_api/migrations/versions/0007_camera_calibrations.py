"""Camera calibrations: intrinsics per device, append-only.

The app role may INSERT and SELECT, never UPDATE or DELETE: a correction is a
new row, and "current" is the latest per device. Latest is by ``seq`` (an
identity), not ``created_at``: ``now()`` is the transaction's start time, so two
rows appended in one transaction would tie.

``camera_model`` names the projection (a flat-port camera is axial, not a
pinhole -- PLAN.md §8); an axial-refractive calibration must name its port
model. ``medium`` and ``coordinate_frame`` may be NULL, meaning *unknown*:
migrated v1 rows never recorded them, and unknown is not guessed.

Revision ID: 0007
Revises: 0006
"""

from alembic import context, op

revision = "0007"
down_revision = "0006"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    app_role = _app_role()

    op.execute("""
        CREATE TABLE camera_calibrations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            v1_id bigint UNIQUE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            device_id uuid NOT NULL,
            camera_model text NOT NULL DEFAULT 'pinhole'
                CONSTRAINT camera_calibrations_camera_model_check
                CHECK (camera_model IN ('pinhole', 'axial_refractive')),
            medium text
                CONSTRAINT camera_calibrations_medium_check
                CHECK (medium IN ('air', 'water')),
            coordinate_frame text
                CONSTRAINT camera_calibrations_coordinate_frame_check
                CHECK (coordinate_frame IN ('jpeg', 'raw_sensor')),
            camera_matrix jsonb NOT NULL,
            distortion_coefficients jsonb NOT NULL,
            calibration_target_id uuid REFERENCES calibration_targets (id),
            rms_px double precision
                CONSTRAINT camera_calibrations_rms_px_check CHECK (rms_px >= 0),
            port_model text,
            port_model_version text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, device_id) REFERENCES devices (tenant_id, id),
            CONSTRAINT camera_calibrations_matrix_shape_check CHECK (
                jsonb_typeof(camera_matrix) = 'array'
                AND jsonb_array_length(camera_matrix) = 3
                AND jsonb_array_length(camera_matrix -> 0) = 3
                AND jsonb_array_length(camera_matrix -> 1) = 3
                AND jsonb_array_length(camera_matrix -> 2) = 3
            ),
            CONSTRAINT camera_calibrations_distortion_shape_check CHECK (
                jsonb_typeof(distortion_coefficients) = 'array'
            ),
            CONSTRAINT camera_calibrations_axial_needs_port_check CHECK (
                camera_model <> 'axial_refractive' OR port_model IS NOT NULL
            )
        )
        """)

    op.execute("ALTER TABLE camera_calibrations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE camera_calibrations FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON camera_calibrations
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    # Append-only: no UPDATE, no DELETE.
    op.execute(f"GRANT SELECT, INSERT ON camera_calibrations TO {app_role}")

    op.execute("""
        CREATE VIEW current_camera_calibrations
            WITH (security_invoker = true) AS
            SELECT DISTINCT ON (device_id) *
            FROM camera_calibrations
            ORDER BY device_id, seq DESC
        """)
    op.execute(f"GRANT SELECT ON current_camera_calibrations TO {app_role}")


def downgrade() -> None:
    op.execute("DROP VIEW current_camera_calibrations")
    op.execute("DROP TABLE camera_calibrations")
