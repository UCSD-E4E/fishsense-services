"""Refusals: "tried, made no progress" for laser depths and measurements.

v1's depth stage counted an image none of whose valid labels triangulated in
front of the camera (`skipped_invalid_geometry`) and wrote nothing, so its
cohort offered the dive every hour forever: dive 32 blocked 49 dives for 23
hours (fishsense-lite@77e8f8e5 CLAUDE.md, PLAN.md §4.5). Stage 14 did the same
with a non-finite length, and v2's `measurements` (CHECK length_m > 0) cannot
hold v1's zero-length ones. PLAN.md §9.16 asks for an explicit state instead.

A refusal is **a record of the inputs tried and the answer**, appended, never
edited or deleted (SELECT, INSERT only, like the results it stands in for):

- ``laser_depth_refusals``: a capture's laser label -- with the dot as it was
  (x, y) -- under a laser calibration gave no depth in front of the camera;
- ``measurement_refusals``: a capture's laser dot and head/tail keypoints
  under a calibration gave no usable length (zero, or not finite), or its
  species label named a real fish no name could be read from.

A refusal holds only while every input it names is still the one in use: the
cohorts compare the ids *and* the coordinates, so a new calibration, a moved
dot or a new label is tried again. The cohorts skip what is refused
(depth_measure_02).

Revision ID: depth_measure_01
Revises: 0020
"""

from alembic import context, op

revision = "depth_measure_01"
down_revision = "0020"

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
        CREATE TABLE laser_depth_refusals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            capture_id uuid NOT NULL,
            laser_label_id uuid NOT NULL,
            laser_x double precision NOT NULL,
            laser_y double precision NOT NULL,
            laser_calibration_id uuid NOT NULL,
            reason text NOT NULL
                CONSTRAINT laser_depth_refusals_reason_check
                CHECK (reason IN ('non_finite_depth', 'non_positive_depth')),
            -- What the triangulation said; NULL when it was not finite. Never
            -- a usable depth, or it would have been one.
            depth_m double precision
                CONSTRAINT laser_depth_refusals_depth_m_check CHECK (depth_m <= 0),
            core_version text NOT NULL,
            run_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, laser_label_id)
                REFERENCES laser_labels (tenant_id, id),
            FOREIGN KEY (tenant_id, laser_calibration_id)
                REFERENCES laser_calibrations (tenant_id, id)
        )
        """)
    op.execute("""
        CREATE INDEX laser_depth_refusals_tenant_id_capture_id_idx
            ON laser_depth_refusals (tenant_id, capture_id)
        """)
    _append_only("laser_depth_refusals", app_role)

    op.execute("""
        CREATE TABLE measurement_refusals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            capture_id uuid NOT NULL,
            laser_calibration_id uuid NOT NULL,
            reason text NOT NULL
                CONSTRAINT measurement_refusals_reason_check
                CHECK (reason IN ('non_finite_length', 'zero_length',
                                  'unparseable_species')),
            species_label_id uuid NOT NULL,
            content_of_image text,
            laser_label_id uuid NOT NULL,
            laser_x double precision NOT NULL,
            laser_y double precision NOT NULL,
            head_tail_label_id uuid NOT NULL,
            head_x double precision NOT NULL,
            head_y double precision NOT NULL,
            tail_x double precision NOT NULL,
            tail_y double precision NOT NULL,
            -- What was computed: NULL when not finite.
            length_m double precision,
            depth_m double precision,
            algorithm text NOT NULL,
            algorithm_version text NOT NULL,
            core_version text NOT NULL,
            run_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, laser_calibration_id)
                REFERENCES laser_calibrations (tenant_id, id),
            FOREIGN KEY (tenant_id, species_label_id)
                REFERENCES species_labels (tenant_id, id),
            FOREIGN KEY (tenant_id, laser_label_id)
                REFERENCES laser_labels (tenant_id, id),
            FOREIGN KEY (tenant_id, head_tail_label_id)
                REFERENCES head_tail_labels (tenant_id, id)
        )
        """)
    op.execute("""
        CREATE INDEX measurement_refusals_tenant_id_capture_id_idx
            ON measurement_refusals (tenant_id, capture_id)
        """)
    _append_only("measurement_refusals", app_role)


def downgrade() -> None:
    op.execute("DROP TABLE measurement_refusals")
    op.execute("DROP TABLE laser_depth_refusals")
