"""Slate presence predictions: append-only, with provenance; and which slate
labels the detector queued.

New in v2 (v1's slate predictor estimated *pose*; it was retired on
2026-08-03, and its `slate_predictions` stay as they migrated). The model is
2026-10-03_slate_detector@95a77d95's presence classifier; see
`fishsense_services_contracts.slate_presence`.

`slate_presence_predictions` follows 0034's prediction table: appended, never
updated or deleted (SELECT and INSERT only), tenant-scoped under forced RLS,
ordered by `seq`, with `current_slate_presence` the latest per capture (led
by the tenant, as 0028's `current_measurements`, so a lookup reaches the
index). Each row records the capture, `status` (`predicted`, or
`decode_failed` with no probability: recorded so the cohort, which selects on
a row's absence, moves on), `probability` (P(slate)), `model_version`
(`SLATE_DETECTOR_VERSION`) and `weights_sha256`, the verified weights it ran.
No `v1_id` and so no `number`: nothing migrates into it (0021, 0025, 0034).

`slate_labels.slate_presence_prediction_id` is the provenance of a label row
the detector queued: the prediction that put its frame in the dive's slate
project. NULL is every other row (a frame a person marked `Slate, Laser on
slate`, and everything v1 wrote). Additive, nullable, same-tenant.

Revision ID: slate_01
Revises: 0034
"""

from alembic import context, op

revision = "slate_01"
down_revision = "0034"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    app_role = _app_role()
    op.execute("""
        CREATE TABLE slate_presence_predictions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            capture_id uuid NOT NULL,
            status text NOT NULL
                CONSTRAINT slate_presence_predictions_status_check
                CHECK (status IN ('predicted', 'decode_failed')),
            probability double precision
                CONSTRAINT slate_presence_predictions_probability_check
                CHECK (probability BETWEEN 0 AND 1),
            model_version integer NOT NULL,
            weights_sha256 text NOT NULL
                CONSTRAINT slate_presence_predictions_weights_sha256_check
                CHECK (weights_sha256 ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            CONSTRAINT slate_presence_predictions_scored_check CHECK (
                (status = 'predicted') = (probability IS NOT NULL)
            )
        )
        """)
    op.execute("""
        CREATE INDEX slate_presence_predictions_tenant_id_capture_id_seq_idx
            ON slate_presence_predictions (tenant_id, capture_id, seq)
        """)
    op.execute("ALTER TABLE slate_presence_predictions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE slate_presence_predictions FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON slate_presence_predictions
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    # Append-only: no UPDATE, no DELETE.
    op.execute(f"GRANT SELECT, INSERT ON slate_presence_predictions TO {app_role}")
    op.execute("""
        CREATE VIEW current_slate_presence WITH (security_invoker = true) AS
            SELECT DISTINCT ON (tenant_id, capture_id) *
            FROM slate_presence_predictions
            ORDER BY tenant_id, capture_id, seq DESC
        """)
    op.execute(f"GRANT SELECT ON current_slate_presence TO {app_role}")

    op.execute("""
        ALTER TABLE slate_labels
            ADD COLUMN slate_presence_prediction_id uuid,
            ADD FOREIGN KEY (tenant_id, slate_presence_prediction_id)
                REFERENCES slate_presence_predictions (tenant_id, id)
        """)


def downgrade() -> None:
    op.execute("ALTER TABLE slate_labels DROP COLUMN slate_presence_prediction_id")
    op.execute("DROP VIEW current_slate_presence")
    op.execute("DROP TABLE slate_presence_predictions")
