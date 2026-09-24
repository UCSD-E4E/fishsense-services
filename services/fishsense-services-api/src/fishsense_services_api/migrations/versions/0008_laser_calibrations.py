"""Laser calibrations: per dive, append-only; refusals are rows.

v1 kept one overwritten extrinsics row per dive and its refusals as columns on
``dive``. Here each attempt is appended with its outcome (PLAN.md §9.13):

- ``accepted`` carries the laser position and axis (3-vectors);
- ``refused`` carries its reason.

``producer`` says how it was calibrated -- v1's slate and checkerboard, and the
research methods (wuwnet, §2.7). Only migrated rows (``v1_id``) may leave it
unknown. ``inputs_as_of`` records the newest input considered (v1's
``calibration_refused_labels_at``), so a refusal can expire when newer labels
arrive.

Views (both ``security_invoker``):

- ``current_laser_calibrations``: the latest row per dive, by ``seq``.
- ``effective_laser_calibrations``: what a dive is measured with. It follows
  v1's borrowing -- a dive naming a calibration source (same tenant only,
  §9.17) uses that dive's current calibration -- and only an accepted one.

Revision ID: 0008
Revises: 0007
"""

from alembic import context, op

revision = "0008"
down_revision = "0007"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
PRODUCERS = (
    "slate",
    "checkerboard",
    "dots_range",
    "dots_two_ranges",
    "dots_apparent_size",
    "bench",
)


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _three_vector(column: str) -> str:
    # IS NOT NULL first: a CHECK passes when it evaluates to NULL, so without it
    # a missing vector would satisfy "is a 3-array" by being unknown.
    return (
        f"({column} IS NOT NULL AND jsonb_typeof({column}) = 'array'"
        f" AND jsonb_array_length({column}) = 3)"
    )


def upgrade() -> None:
    app_role = _app_role()
    producers = ", ".join(f"'{p}'" for p in PRODUCERS)

    op.execute(f"""
        CREATE TABLE laser_calibrations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            v1_id bigint UNIQUE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            dive_id uuid NOT NULL,
            camera_calibration_id uuid,
            producer text
                CONSTRAINT laser_calibrations_producer_check
                CHECK (producer IN ({producers})),
            outcome text NOT NULL
                CONSTRAINT laser_calibrations_outcome_check
                CHECK (outcome IN ('accepted', 'refused')),
            laser_position jsonb,
            laser_axis jsonb,
            refusal_reason text,
            inputs_as_of timestamptz,
            gate_verdicts jsonb
                CONSTRAINT laser_calibrations_gate_verdicts_check
                CHECK (jsonb_typeof(gate_verdicts) = 'object'),
            lever_arm_m double precision,
            observation_count integer
                CONSTRAINT laser_calibrations_observation_count_check
                CHECK (observation_count >= 0),
            residual_m double precision
                CONSTRAINT laser_calibrations_residual_m_check
                CHECK (residual_m >= 0),
            core_version text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, dive_id) REFERENCES dives (tenant_id, id),
            FOREIGN KEY (tenant_id, camera_calibration_id)
                REFERENCES camera_calibrations (tenant_id, id),
            CONSTRAINT laser_calibrations_producer_known_check
                CHECK (producer IS NOT NULL OR v1_id IS NOT NULL),
            CONSTRAINT laser_calibrations_accepted_geometry_check CHECK (
                outcome <> 'accepted'
                OR ({_three_vector('laser_position')}
                    AND {_three_vector('laser_axis')})
            ),
            CONSTRAINT laser_calibrations_refusal_reason_check
                CHECK (outcome <> 'refused' OR refusal_reason IS NOT NULL)
        )
        """)

    op.execute("ALTER TABLE laser_calibrations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE laser_calibrations FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON laser_calibrations
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    # Append-only: no UPDATE, no DELETE.
    op.execute(f"GRANT SELECT, INSERT ON laser_calibrations TO {app_role}")

    op.execute("""
        CREATE VIEW current_laser_calibrations
            WITH (security_invoker = true) AS
            SELECT DISTINCT ON (dive_id) *
            FROM laser_calibrations
            ORDER BY dive_id, seq DESC
        """)
    op.execute("""
        CREATE VIEW effective_laser_calibrations
            WITH (security_invoker = true) AS
            SELECT d.tenant_id,
                   d.id AS dive_id,
                   c.id AS laser_calibration_id,
                   c.dive_id AS source_dive_id,
                   d.calibration_source_dive_id IS NOT NULL AS borrowed
            FROM dives d
            JOIN current_laser_calibrations c
              ON c.tenant_id = d.tenant_id
             AND c.dive_id = coalesce(d.calibration_source_dive_id, d.id)
            WHERE c.outcome = 'accepted'
        """)
    op.execute(f"""
        GRANT SELECT ON current_laser_calibrations, effective_laser_calibrations
        TO {app_role}
        """)


def downgrade() -> None:
    op.execute("DROP VIEW effective_laser_calibrations")
    op.execute("DROP VIEW current_laser_calibrations")
    op.execute("DROP TABLE laser_calibrations")
