"""Dive laser lines: the within-dive fit of the laser dots, append-only.

v1's ``divelaserline`` (one overwritten row per dive) gates laser auto-accept:
the line ``a·x + b·y + c = 0`` in Hesse normal form, with its fit statistics.
It is a within-dive fit only, never a prior for another dive. Each fit is
appended; ``current_dive_laser_lines`` is the latest per dive, by ``seq``.

Revision ID: 0009
Revises: 0008
"""

from alembic import context, op

revision = "0009"
down_revision = "0008"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    app_role = _app_role()

    op.execute("""
        CREATE TABLE dive_laser_lines (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            v1_id bigint UNIQUE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            dive_id uuid NOT NULL,
            a double precision NOT NULL,
            b double precision NOT NULL,
            c double precision NOT NULL,
            n_points integer NOT NULL
                CONSTRAINT dive_laser_lines_n_points_check CHECK (n_points >= 0),
            inlier_count integer NOT NULL
                CONSTRAINT dive_laser_lines_inlier_count_check
                CHECK (inlier_count >= 0),
            inlier_fraction double precision NOT NULL
                CONSTRAINT dive_laser_lines_inlier_fraction_check
                CHECK (inlier_fraction BETWEEN 0 AND 1),
            residual_std double precision NOT NULL
                CONSTRAINT dive_laser_lines_residual_std_check
                CHECK (residual_std >= 0),
            label_noise_mad double precision NOT NULL
                CONSTRAINT dive_laser_lines_label_noise_mad_check
                CHECK (label_noise_mad >= 0),
            line_confidence double precision NOT NULL
                CONSTRAINT dive_laser_lines_line_confidence_check
                CHECK (line_confidence BETWEEN 0 AND 1),
            fitted_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, dive_id) REFERENCES dives (tenant_id, id),
            CONSTRAINT dive_laser_lines_inliers_within_points_check
                CHECK (inlier_count <= n_points)
        )
        """)

    op.execute("ALTER TABLE dive_laser_lines ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE dive_laser_lines FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON dive_laser_lines
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    # Append-only: no UPDATE, no DELETE.
    op.execute(f"GRANT SELECT, INSERT ON dive_laser_lines TO {app_role}")

    op.execute("""
        CREATE VIEW current_dive_laser_lines
            WITH (security_invoker = true) AS
            SELECT DISTINCT ON (dive_id) *
            FROM dive_laser_lines
            ORDER BY dive_id, seq DESC
        """)
    op.execute(f"GRANT SELECT ON current_dive_laser_lines TO {app_role}")


def downgrade() -> None:
    op.execute("DROP VIEW current_dive_laser_lines")
    op.execute("DROP TABLE dive_laser_lines")
