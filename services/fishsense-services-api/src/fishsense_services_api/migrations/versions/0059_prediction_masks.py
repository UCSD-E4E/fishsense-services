"""SAM masks, stored as files in the tenant's bucket (2026-10-21).

FishSense Mobile syncs its fish mask as a file (``mask_u8``: uint8, width x
height, nonzero = fish) named in ``capture_objects`` (0038). The masks our own
stages keep -- the automatic track's and the human-path head/tail predictor's
-- were dropped once their head/tail, box and area were taken. Now each kept
mask is stored the same way, gzip'd (a 4014x3016 mask is 12 MB raw; owner,
2026-10-21), and named here once the orchestrator has read it back and checked
it (`contracts.masks`).

One row per stored mask, naming exactly one prediction -- automatic
(``automatic_head_tail_prediction_id``) or human-path
(``head_tail_prediction_id``) -- and its object: bucket, key (unique),
SHA-256, size, content type, and the frame it is in (``width x height`` once
gunzipped). Append-only, tenant RLS; the research role reads the lab's rows.

Revision ID: 0059
Revises: 0058
"""

from alembic import context, op

from fishsense_services_api.schema_audit import (
    RESEARCH_LAB_BINDING,
    RESEARCH_LAB_READ,
    RESEARCH_ROLE,
)

revision = "0059"
down_revision = "0058"

TABLE = "prediction_masks"
ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_LAB = "tenant_id = (SELECT public.research_tenant_id())"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    app_role = _app_role()
    op.execute(f"""
        CREATE TABLE {TABLE} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            capture_id uuid NOT NULL,
            automatic_head_tail_prediction_id uuid UNIQUE,
            head_tail_prediction_id uuid UNIQUE,
            bucket text NOT NULL,
            object_key text NOT NULL,
            sha256 text NOT NULL
                CONSTRAINT {TABLE}_sha256_check CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
            size_bytes bigint NOT NULL CONSTRAINT {TABLE}_size_check
                CHECK (size_bytes > 0),
            content_type text NOT NULL,
            width integer NOT NULL CONSTRAINT {TABLE}_width_check CHECK (width > 0),
            height integer NOT NULL
                CONSTRAINT {TABLE}_height_check CHECK (height > 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            UNIQUE (bucket, object_key),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, automatic_head_tail_prediction_id)
                REFERENCES automatic_head_tail_predictions (tenant_id, id),
            FOREIGN KEY (tenant_id, head_tail_prediction_id)
                REFERENCES head_tail_predictions (tenant_id, id),
            CONSTRAINT {TABLE}_one_prediction_check CHECK (
                (automatic_head_tail_prediction_id IS NULL)
                <> (head_tail_prediction_id IS NULL))
        )
        """)
    op.execute(f"""
        CREATE INDEX {TABLE}_tenant_id_capture_id_idx ON {TABLE} (tenant_id, capture_id)
        """)
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {TABLE}
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    # Append-only: no UPDATE, no DELETE.
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO {app_role}")
    op.execute(
        f"CREATE POLICY {RESEARCH_LAB_READ} ON public.{TABLE} FOR SELECT "
        f"TO {RESEARCH_ROLE} USING ({_LAB})"
    )
    op.execute(
        f"CREATE POLICY {RESEARCH_LAB_BINDING} ON public.{TABLE} AS RESTRICTIVE "
        f"FOR SELECT TO {RESEARCH_ROLE} USING ({_LAB})"
    )
    op.execute(f"GRANT SELECT ON {TABLE} TO {RESEARCH_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP POLICY {RESEARCH_LAB_BINDING} ON public.{TABLE}")
    op.execute(f"DROP POLICY {RESEARCH_LAB_READ} ON public.{TABLE}")
    op.execute(f"DROP TABLE {TABLE}")
