"""Laser depths and measurements: append-only, with named inputs.

v1 overwrote one measurement per (image, fish) and one depth per image, and
recomputed whatever no longer matched its recorded calibration. Here every
result is appended with its inputs -- laser calibration, laser label, head/tail
label -- and provenance (algorithm + version, core version, model version, run).

**current_measurements** (PLAN.md §9.13): the latest per (capture, fish,
source) whose inputs still hold -- a server result's laser calibration is still
the dive's *effective* calibration (0008), and none of its input labels are
superseded. A recalibration therefore makes old results stale by itself, the
stale rows stay as history, and a server recompute never displaces a device's
own measurement (different source). Device results carry no server calibration.

New server results must name algorithm, version, core version and calibration;
only migrated rows (``v1_id``) may lack them. Lengths and depths are positive.

Revision ID: 0013
Revises: 0012
"""

from alembic import context, op

revision = "0013"
down_revision = "0012"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _append_only(table: str, app_role: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    op.execute(f"GRANT SELECT, INSERT ON {table} TO {app_role}")


def upgrade() -> None:
    app_role = _app_role()

    op.execute("""
        CREATE TABLE laser_depths (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            v1_id bigint UNIQUE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            capture_id uuid NOT NULL,
            laser_label_id uuid,
            laser_calibration_id uuid,
            depth_m double precision NOT NULL
                CONSTRAINT laser_depths_depth_m_check CHECK (depth_m > 0),
            range_m double precision
                CONSTRAINT laser_depths_range_m_check CHECK (range_m >= 0),
            residual_m double precision
                CONSTRAINT laser_depths_residual_m_check CHECK (residual_m >= 0),
            core_version text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, laser_label_id)
                REFERENCES laser_labels (tenant_id, id),
            FOREIGN KEY (tenant_id, laser_calibration_id)
                REFERENCES laser_calibrations (tenant_id, id),
            CONSTRAINT laser_depths_provenance_check
                CHECK (core_version IS NOT NULL OR v1_id IS NOT NULL)
        )
        """)
    _append_only("laser_depths", app_role)
    op.execute("""
        CREATE VIEW current_laser_depths WITH (security_invoker = true) AS
            SELECT DISTINCT ON (capture_id) *
            FROM laser_depths
            ORDER BY capture_id, seq DESC
        """)

    op.execute("""
        CREATE TABLE measurements (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            v1_id bigint UNIQUE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            capture_id uuid,
            fish_id uuid,
            source text NOT NULL
                CONSTRAINT measurements_source_check
                CHECK (source IN ('server', 'device')),
            length_m double precision,
            laser_calibration_id uuid,
            laser_depth_id uuid,
            laser_label_id uuid,
            head_tail_label_id uuid,
            algorithm text,
            algorithm_version text,
            run_id uuid,
            core_version text,
            model_version text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, fish_id) REFERENCES fish (tenant_id, id),
            FOREIGN KEY (tenant_id, laser_calibration_id)
                REFERENCES laser_calibrations (tenant_id, id),
            FOREIGN KEY (tenant_id, laser_depth_id)
                REFERENCES laser_depths (tenant_id, id),
            FOREIGN KEY (tenant_id, laser_label_id)
                REFERENCES laser_labels (tenant_id, id),
            FOREIGN KEY (tenant_id, head_tail_label_id)
                REFERENCES head_tail_labels (tenant_id, id),
            CONSTRAINT measurements_subject_check CHECK (
                (capture_id IS NOT NULL AND fish_id IS NOT NULL) OR v1_id IS NOT NULL
            ),
            -- CASE, not OR: a CHECK passes on NULL, and "length_m > 0" is NULL
            -- for a missing length.
            CONSTRAINT measurements_length_check CHECK (
                CASE WHEN length_m IS NULL THEN v1_id IS NOT NULL
                     ELSE length_m > 0 END
            ),
            CONSTRAINT measurements_server_provenance_check CHECK (
                source <> 'server' OR v1_id IS NOT NULL
                OR (algorithm IS NOT NULL AND algorithm_version IS NOT NULL
                    AND core_version IS NOT NULL
                    AND laser_calibration_id IS NOT NULL)
            )
        )
        """)
    _append_only("measurements", app_role)
    op.execute("""
        CREATE VIEW current_measurements WITH (security_invoker = true) AS
            SELECT DISTINCT ON (m.capture_id, m.fish_id, m.source) m.*
            FROM measurements m
            JOIN captures c
              ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
            LEFT JOIN effective_laser_calibrations e
              ON e.tenant_id = c.tenant_id AND e.dive_id = c.dive_id
            LEFT JOIN laser_labels ll
              ON ll.tenant_id = m.tenant_id AND ll.id = m.laser_label_id
            LEFT JOIN head_tail_labels ht
              ON ht.tenant_id = m.tenant_id AND ht.id = m.head_tail_label_id
            WHERE (m.source = 'device'
                   OR m.laser_calibration_id = e.laser_calibration_id)
              AND NOT coalesce(ll.superseded, false)
              AND NOT coalesce(ht.superseded, false)
            ORDER BY m.capture_id, m.fish_id, m.source, m.seq DESC
        """)
    op.execute(f"""
        GRANT SELECT ON current_laser_depths, current_measurements TO {app_role}
        """)


def downgrade() -> None:
    op.execute("DROP VIEW current_measurements")
    op.execute("DROP TABLE measurements")
    op.execute("DROP VIEW current_laser_depths")
    op.execute("DROP TABLE laser_depths")
