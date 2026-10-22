"""Every regeneration of a prediction's SAM mask (2026-10-22).

The mask backfill re-runs the kernel that made each prediction made before
masks were stored (0059) and keeps the mask when it is the prediction's: the
same crop, the area within 0.5% (owner, 2026-10-22). The keypointer's head
and tail on that mask are not judged -- on the trial they moved 1-284 px from
the same mask -- but kept here with every regeneration, stored or not, so
their repeatability can be measured over the whole backfill.

One row per regeneration, naming exactly one prediction (automatic or
human-path), its outcome, what it got (area, crop, head, tail; none without a
mask), and the mask it stored, if any. Append-only, tenant RLS; the research
role reads the lab's rows.

Revision ID: 0062
Revises: 0061
"""

from alembic import context, op

from fishsense_services_api.schema_audit import (
    RESEARCH_LAB_BINDING,
    RESEARCH_LAB_READ,
    RESEARCH_ROLE,
)

revision = "0062"
down_revision = "0061"

TABLE = "mask_regenerations"
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
            automatic_head_tail_prediction_id uuid,
            head_tail_prediction_id uuid,
            outcome text NOT NULL CONSTRAINT {TABLE}_outcome_check CHECK (outcome IN
                ('reproduced', 'mismatch', 'no_mask', 'image_unavailable',
                 'decode_failed')),
            mask_area_px integer,
            crop_x integer,
            crop_y integer,
            head_x double precision,
            head_y double precision,
            tail_x double precision,
            tail_y double precision,
            prediction_mask_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, automatic_head_tail_prediction_id)
                REFERENCES automatic_head_tail_predictions (tenant_id, id),
            FOREIGN KEY (tenant_id, head_tail_prediction_id)
                REFERENCES head_tail_predictions (tenant_id, id),
            FOREIGN KEY (tenant_id, prediction_mask_id)
                REFERENCES prediction_masks (tenant_id, id),
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
