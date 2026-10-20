"""Every capture's render: its raw rectified to a JPEG, in its tenant's bucket.

New in v2 (2026-10-19). The render is the image every step after the raw
reads -- SAM, species, the label-free calibration, the portal's /fish -- so
it is made once per capture and recorded here: which render version, under
which camera calibration (both change the pixels, and both are in the key),
in which bucket at which key, and the bytes the orchestrator checked against
the stored object. An abstention (the raw was not in scratch, or would not
decode) is recorded too, so a run does not retry it forever.

Append-only, as every results table: a rendered capture is recorded once per
(version, camera) (a partial unique index), and ``current_capture_renders``
is each capture's newest row. Tenant-isolated by RLS; the research role reads
the lab's rows, as 0036's automatic tables.

Revision ID: 0050
Revises: 0049
"""

from alembic import context, op

from fishsense_services_api.schema_audit import (
    RESEARCH_LAB_BINDING,
    RESEARCH_LAB_READ,
    RESEARCH_ROLE,
)

revision = "0050"
down_revision = "0049"

TABLE = "capture_renders"
VIEW = "current_capture_renders"
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
            status text NOT NULL
                CONSTRAINT {TABLE}_status_check
                CHECK (status IN ('rendered', 'raw_unavailable', 'decode_failed')),
            render_version integer NOT NULL
                CONSTRAINT {TABLE}_render_version_check CHECK (render_version > 0),
            camera_calibration_id uuid NOT NULL,
            bucket text,
            key text,
            bytes bigint,
            md5 text,
            width integer,
            height integer,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, camera_calibration_id)
                REFERENCES camera_calibrations (tenant_id, id),
            CONSTRAINT {TABLE}_object_check CHECK (
                (status = 'rendered') = (
                    bucket IS NOT NULL AND key IS NOT NULL
                    AND bytes IS NOT NULL AND bytes > 0
                    AND md5 IS NOT NULL AND md5 ~ '^[0-9a-f]{{32}}$')),
            CONSTRAINT {TABLE}_abstention_check CHECK (
                status = 'rendered' OR (bucket IS NULL AND key IS NULL
                                        AND bytes IS NULL AND md5 IS NULL))
        )
        """)
    op.execute(f"""
        CREATE INDEX {TABLE}_tenant_id_capture_id_seq_idx
            ON {TABLE} (tenant_id, capture_id, seq)
        """)
    op.execute(f"""
        CREATE UNIQUE INDEX {TABLE}_rendered_once_idx
            ON {TABLE} (tenant_id, capture_id, render_version, camera_calibration_id)
            WHERE status = 'rendered'
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
    op.execute(f"""
        CREATE VIEW {VIEW} WITH (security_invoker = true) AS
            SELECT DISTINCT ON (tenant_id, capture_id) *
            FROM {TABLE}
            ORDER BY tenant_id, capture_id, seq DESC
        """)
    op.execute(f"GRANT SELECT ON {TABLE}, {VIEW} TO {app_role}, {RESEARCH_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP VIEW {VIEW}")
    op.execute(f"DROP POLICY {RESEARCH_LAB_BINDING} ON public.{TABLE}")
    op.execute(f"DROP POLICY {RESEARCH_LAB_READ} ON public.{TABLE}")
    op.execute(f"DROP TABLE {TABLE}")
