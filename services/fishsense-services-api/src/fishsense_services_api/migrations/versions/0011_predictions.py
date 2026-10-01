"""Model predictions (laser dot, slate, head/tail): append-only, with provenance.

v1 overwrote one prediction per image (``laserprediction`` etc.). Here each is
appended with ``predictor_version`` / ``checkpoint`` / ``core_version`` -- only
migrated rows may lack a predictor version -- and ``current_*`` views give the
latest per capture, by ``seq``.

Shape rules, from v1's semantics:

- laser: ``x`` and ``y`` together, or neither (no dot found); the auto-accept
  gate's verdict rides on the prediction, as in v1.
- head/tail: a ``predicted`` status carries all four points; it names the
  laser label it was cropped around (same tenant).
- slate: a new prediction has reference points *or* a rejection reason.

Revision ID: 0011
Revises: 0010
"""

from alembic import context, op

revision = "0011"
down_revision = "0010"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _core(table: str) -> str:
    return f"""
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
        v1_id bigint UNIQUE,
        seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
        capture_id uuid NOT NULL,
        width integer,
        height integer,
        confidence double precision NOT NULL DEFAULT 0,
        predictor_version integer,
        checkpoint text,
        core_version text,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, id),
        FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
        CONSTRAINT {table}_predictor_known_check
            CHECK (predictor_version IS NOT NULL OR v1_id IS NOT NULL),
    """


TABLES = {
    "laser_predictions": """
        x double precision,
        y double precision,
        color text
            CONSTRAINT laser_predictions_color_check
            CHECK (color IN ('red', 'green')),
        color_margin double precision,
        rejected_out_of_region boolean NOT NULL DEFAULT false,
        auto_accept boolean NOT NULL DEFAULT false,
        gate_verdict text
            CONSTRAINT laser_predictions_gate_verdict_check
            CHECK (gate_verdict IN ('off_line', 'along_line_outlier',
                                    'audit_sample', 'dive_ineligible',
                                    'no_prediction')),
        line_offset_px double precision,
        line_position_z double precision,
        CONSTRAINT laser_predictions_dot_check CHECK ((x IS NULL) = (y IS NULL))
    """,
    "slate_predictions": """
        reference_points jsonb,
        rejected_reason text
            CONSTRAINT slate_predictions_rejected_reason_check
            CHECK (rejected_reason IN ('unsupported_slate_family', 'no_board',
                                       'low_confidence', 'points_off_canvas')),
        CONSTRAINT slate_predictions_points_or_rejection_check CHECK (
            v1_id IS NOT NULL
            OR (reference_points IS NULL) <> (rejected_reason IS NULL)
        )
    """,
    "head_tail_predictions": """
        head_x double precision,
        head_y double precision,
        tail_x double precision,
        tail_y double precision,
        mask_area_px integer,
        silhouette_ratio double precision,
        crop_x integer,
        crop_y integer,
        laser_label_id uuid,
        status text NOT NULL DEFAULT 'predicted'
            CONSTRAINT head_tail_predictions_status_check
            CHECK (status IN ('predicted', 'no_detections',
                              'laser_off_all_fish', 'headtail_failed')),
        rejected_low_confidence boolean NOT NULL DEFAULT false,
        FOREIGN KEY (tenant_id, laser_label_id)
            REFERENCES laser_labels (tenant_id, id),
        CONSTRAINT head_tail_predictions_points_check CHECK (
            status <> 'predicted'
            OR (head_x IS NOT NULL AND head_y IS NOT NULL
                AND tail_x IS NOT NULL AND tail_y IS NOT NULL)
        )
    """,
}


def upgrade() -> None:
    app_role = _app_role()
    for table, columns in TABLES.items():
        op.execute(f"CREATE TABLE {table} ({_core(table)} {columns})")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = {ACTIVE_TENANT})
                WITH CHECK (tenant_id = {ACTIVE_TENANT})
            """)
        # Append-only: no UPDATE, no DELETE.
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {app_role}")
        op.execute(f"""
            CREATE VIEW current_{table} WITH (security_invoker = true) AS
                SELECT DISTINCT ON (capture_id) *
                FROM {table}
                ORDER BY capture_id, seq DESC
            """)
        op.execute(f"GRANT SELECT ON current_{table} TO {app_role}")


def downgrade() -> None:
    for table in reversed(list(TABLES)):
        op.execute(f"DROP VIEW current_{table}")
        op.execute(f"DROP TABLE {table}")
