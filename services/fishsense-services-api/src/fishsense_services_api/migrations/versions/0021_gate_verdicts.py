"""The auto-accept gate's verdicts, appended beside the predictions they judge.

v1's gate UPDATEs its verdict in place on the prediction row (`auto_accept`,
`gate_verdict`, `line_offset_px`, `line_position_z`), and a re-prediction's
whole-row upsert clears it. v2's `laser_predictions` is append-only (0011: no
UPDATE), and it stays so: each verdict the gate reaches is a row here, keyed to
the prediction it judged.

* **A verdict belongs to one prediction row.** A re-prediction appends a new
  prediction, which has no verdict of its own and so reads as unjudged -- v1's
  "a re-prediction clears the verdict", by construction;
* **the effective verdict** of a prediction is its latest verdict here, else
  what the row itself carries -- only a migrated row carries one (v1's gate
  columns, copied by migrate-v1);
* `current_laser_predictions_gated` is `current_laser_predictions` with the
  effective verdict in place of the row's own columns: what every reader of
  the gate (populate, the auto-accept apply, the landing page's gated flag,
  the status view) should read.

The gate appends only verdicts that changed (v1 wrote only changed rows), so
an hourly re-judgement of an unchanged dive writes nothing.

Revision ID: 0021
Revises: 0020
"""

from alembic import context, op

revision = "0021"
down_revision = "0020"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
GATE_VERDICTS = (
    "auto_accepted",
    "off_line",
    "along_line_outlier",
    "audit_sample",
    "dive_ineligible",
    "no_prediction",
)


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    app_role = _app_role()
    verdicts = ", ".join(f"'{v}'" for v in GATE_VERDICTS)
    op.execute(f"""
        CREATE TABLE laser_prediction_verdicts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            prediction_id uuid NOT NULL,
            auto_accept boolean NOT NULL,
            gate_verdict text NOT NULL
                CONSTRAINT laser_prediction_verdicts_gate_verdict_check
                CHECK (gate_verdict IN ({verdicts})),
            line_offset_px double precision,
            line_position_z double precision,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, prediction_id)
                REFERENCES laser_predictions (tenant_id, id),
            CONSTRAINT laser_prediction_verdicts_accept_is_a_verdict_check
                CHECK (NOT auto_accept OR gate_verdict = 'auto_accepted')
        )
        """)
    op.execute("""
        CREATE INDEX laser_prediction_verdicts_prediction_idx
            ON laser_prediction_verdicts (tenant_id, prediction_id, seq)
        """)
    op.execute("ALTER TABLE laser_prediction_verdicts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE laser_prediction_verdicts FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON laser_prediction_verdicts
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    # Append-only: no UPDATE, no DELETE.
    op.execute(f"GRANT SELECT, INSERT ON laser_prediction_verdicts TO {app_role}")

    op.execute("""
        CREATE VIEW current_laser_predictions_gated
            WITH (security_invoker = true) AS
            SELECT p.id, p.tenant_id, p.v1_id, p.number, p.seq, p.capture_id,
                   p.width, p.height, p.confidence, p.predictor_version,
                   p.checkpoint, p.core_version, p.created_at,
                   p.x, p.y, p.color, p.color_margin, p.rejected_out_of_region,
                   CASE WHEN v.id IS NULL THEN p.auto_accept
                        ELSE v.auto_accept END AS auto_accept,
                   CASE WHEN v.id IS NULL THEN p.gate_verdict
                        ELSE v.gate_verdict END AS gate_verdict,
                   CASE WHEN v.id IS NULL THEN p.line_offset_px
                        ELSE v.line_offset_px END AS line_offset_px,
                   CASE WHEN v.id IS NULL THEN p.line_position_z
                        ELSE v.line_position_z END AS line_position_z
            FROM (
                -- current_laser_predictions, spelt out: that view's `*` was
                -- fixed when 0011 created it, before 0019 added `number`.
                SELECT DISTINCT ON (capture_id) * FROM laser_predictions
                ORDER BY capture_id, seq DESC
            ) p
            LEFT JOIN LATERAL (
                SELECT * FROM laser_prediction_verdicts lv
                WHERE lv.tenant_id = p.tenant_id AND lv.prediction_id = p.id
                ORDER BY lv.seq DESC
                LIMIT 1
            ) v ON true
        """)
    op.execute(f"GRANT SELECT ON current_laser_predictions_gated TO {app_role}")

    # 0009's and 0011's views were `SELECT *`, which Postgres expands when the
    # view is made -- before 0017 added `noise_estimator` and 0019 `number`.
    # Appending them is additive (existing columns keep their order).
    for view, table, key, added in (
        ("current_dive_laser_lines", "dive_laser_lines", "dive_id",
         "noise_estimator, number"),
        ("current_laser_predictions", "laser_predictions", "capture_id", "number"),
    ):  # fmt: skip
        columns = _columns_of(view)
        op.execute(f"""
            CREATE OR REPLACE VIEW {view} WITH (security_invoker = true) AS
                SELECT DISTINCT ON ({key}) {columns}, {added}
                FROM {table}
                ORDER BY {key}, seq DESC
            """)


def _columns_of(view: str) -> str:
    rows = op.get_bind().exec_driver_sql(
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = 'public' AND table_name = '{view}' "
        "ORDER BY ordinal_position"
    )
    return ", ".join(r[0] for r in rows)


def downgrade() -> None:
    for view, table, key in (
        ("current_dive_laser_lines", "dive_laser_lines", "dive_id"),
        ("current_laser_predictions", "laser_predictions", "capture_id"),
    ):
        # CREATE OR REPLACE cannot drop columns: rebuild the view as 0009/0011
        # left it, minus the appended columns.
        columns = [c for c in _columns_of(view).split(", ")
                   if c not in ("noise_estimator", "number")]  # fmt: skip
        op.execute(f"DROP VIEW {view}")
        op.execute(f"""
            CREATE VIEW {view} WITH (security_invoker = true) AS
                SELECT DISTINCT ON ({key}) {", ".join(columns)}
                FROM {table}
                ORDER BY {key}, seq DESC
            """)
        op.execute(f"GRANT SELECT ON {view} TO {_app_role()}")
    op.execute("DROP VIEW current_laser_predictions_gated")
    op.execute("DROP TABLE laser_prediction_verdicts")
