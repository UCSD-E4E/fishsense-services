"""BioCLIP species predictions: append-only, with provenance.

New in v2 (no v1 counterpart: v1 has no species model). The classifier is
ported from coral-gardeners-fish-detector@67c8627; see
`fishsense_services_contracts.species_prediction`. A prediction is a
**pre-annotation only**: shown to a labeler as a suggestion, never written as
a species label (`labels.source = 'pre_annotation'` stays reserved and
unwritten).

`species_predictions` follows 0011's prediction tables: appended, never
updated or deleted (SELECT and INSERT only), tenant-scoped under forced RLS,
ordered by `seq`, with `current_species_predictions` the latest per capture.
Each row records:

* the capture, and the head/tail prediction whose mask box it cropped (same
  tenant): a newer head/tail prediction makes it stale;
* `predictor_version` (`SPECIES_PREDICTOR_VERSION`, which names the model and
  the prompt set; the BioCLIP 2 fallback's is negative) and `model_id`, the
  verified weights (``bioclip/2.5-vith14@<sha256[:12]>``);
* `predicted_choice`, BioCLIP's top-1: the full species taxonomy value
  (``Fish, Hogfish (Lachnolaimus maximus)``), the top-1 probability, the
  margin to the second, and the top five (``[{choice, probability}]``).

A `predicted` row carries all of them; a `decode_failed` one none (it is
recorded so the cohort, which selects on a row's absence, moves on). No
`v1_id` and so no `number` (0019's rule is per `v1_id`): nothing migrates
into it, as with the other v2-only tables (0021, 0025).

Revision ID: 0034
Revises: 0033
"""

from alembic import context, op

revision = "0034"
down_revision = "0033"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    app_role = _app_role()
    op.execute("""
        CREATE TABLE species_predictions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            capture_id uuid NOT NULL,
            headtail_prediction_id uuid NOT NULL,
            status text NOT NULL
                CONSTRAINT species_predictions_status_check
                CHECK (status IN ('predicted', 'decode_failed')),
            predictor_version integer NOT NULL,
            model_id text NOT NULL,
            predicted_choice text,
            top1_probability double precision
                CONSTRAINT species_predictions_top1_probability_check
                CHECK (top1_probability BETWEEN 0 AND 1),
            margin double precision
                CONSTRAINT species_predictions_margin_check
                CHECK (margin BETWEEN 0 AND 1),
            top5 jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, headtail_prediction_id)
                REFERENCES head_tail_predictions (tenant_id, id),
            CONSTRAINT species_predictions_scores_check CHECK (
                (status = 'predicted') = (
                    predicted_choice IS NOT NULL
                    AND top1_probability IS NOT NULL
                    AND margin IS NOT NULL
                    AND jsonb_array_length(top5) > 0
                )
            )
        )
        """)
    op.execute("""
        CREATE INDEX species_predictions_tenant_id_capture_id_seq_idx
            ON species_predictions (tenant_id, capture_id, seq)
        """)
    op.execute("ALTER TABLE species_predictions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE species_predictions FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON species_predictions
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    # Append-only: no UPDATE, no DELETE.
    op.execute(f"GRANT SELECT, INSERT ON species_predictions TO {app_role}")
    op.execute("""
        CREATE VIEW current_species_predictions WITH (security_invoker = true) AS
            SELECT DISTINCT ON (capture_id) *
            FROM species_predictions
            ORDER BY capture_id, seq DESC
        """)
    op.execute(f"GRANT SELECT ON current_species_predictions TO {app_role}")


def downgrade() -> None:
    op.execute("DROP VIEW current_species_predictions")
    op.execute("DROP TABLE species_predictions")
