"""Each render's thumbnail, for /fish's cards (review of #151).

A render is the raw rectified at full size (~6-8 MB); as a card, a page of 50
would download ~300 MB. So each render gets a thumbnail, made from the stored
render -- never the NAS -- and recorded here once the orchestrator has checked
the stored object against the size and MD5 the processor reported
(`contracts.thumbnails`). A render that was not there to read is recorded too
(``render_unavailable``), so it is not tried every hour.

One row per render per target width (``target_width``; the key carries it);
``width``/``height`` are the thumbnail's own pixels. Append-only, tenant RLS;
the research role reads the lab's rows, as for renders.

Revision ID: 0053
Revises: 0052
"""

from alembic import context, op

from fishsense_services_api.schema_audit import (
    RESEARCH_LAB_BINDING,
    RESEARCH_LAB_READ,
    RESEARCH_ROLE,
)

revision = "0053"
down_revision = "0052"

TABLE = "capture_thumbnails"
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
            render_id uuid NOT NULL,
            capture_id uuid NOT NULL,
            status text NOT NULL
                CONSTRAINT {TABLE}_status_check
                CHECK (status IN ('made', 'render_unavailable')),
            target_width integer NOT NULL
                CONSTRAINT {TABLE}_target_width_check CHECK (target_width > 0),
            bucket text,
            key text,
            bytes bigint,
            md5 text,
            width integer,
            height integer,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, render_id, target_width),
            FOREIGN KEY (tenant_id, render_id) REFERENCES capture_renders (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            CONSTRAINT {TABLE}_object_check CHECK (
                (status = 'made') = (
                    bucket IS NOT NULL AND key IS NOT NULL
                    AND bytes IS NOT NULL AND bytes > 0
                    AND md5 IS NOT NULL AND md5 ~ '^[0-9a-f]{{32}}$'
                    AND width > 0 AND height > 0)),
            CONSTRAINT {TABLE}_abstention_check CHECK (
                status = 'made' OR (bucket IS NULL AND key IS NULL
                                    AND bytes IS NULL AND md5 IS NULL))
        )
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
