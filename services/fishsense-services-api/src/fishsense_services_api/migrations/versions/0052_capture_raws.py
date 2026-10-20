"""Each capture's raw, copied into its tenant's bucket; renders settle per bucket.

**Raws in the tenant's bucket** (the owner, 2026-10-20: Lite consistent with
Mobile, whose raws are already there). The render stage stages each raw from
the NAS anyway, so it copies it into the tenant's own bucket in the same pass
(``raws/{checksum}.ORF``, `contracts.renders.raw_key`). A copy is recorded
here only once the orchestrator has checked the stored object against the
bytes the processor reported, and their MD5 against the capture's own
checksum. Append-only; one copy per capture per bucket.

**Renders settle per bucket** (review of #151). 0051's ``rendered_once`` was
one render per (capture, version, camera), whatever the bucket, so a tenant
whose bucket moved could never be rendered into the new one: its photos were
gone for good. This adds ``rendered_once_per_bucket``, which the new code's
``ON CONFLICT`` names. The old index stays for now: the render backlog was
running in production when this deployed, and the orchestrator from before
it names the old index's columns in its ``ON CONFLICT`` until it restarts
(review of the stack). 0054 drops it once this release's orchestrator runs;
until then a render into a second bucket is refused by the old index.

Revision ID: 0052
Revises: 0051
"""

from alembic import context, op

from fishsense_services_api.schema_audit import (
    RESEARCH_LAB_BINDING,
    RESEARCH_LAB_READ,
    RESEARCH_ROLE,
)

revision = "0052"
down_revision = "0051"

TABLE = "capture_raws"
ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_LAB = "tenant_id = (SELECT public.research_tenant_id())"
PER_BUCKET = "capture_renders_rendered_once_per_bucket_idx"


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
            bucket text NOT NULL,
            key text NOT NULL,
            bytes bigint NOT NULL CONSTRAINT {TABLE}_bytes_check CHECK (bytes > 0),
            md5 text NOT NULL
                CONSTRAINT {TABLE}_md5_check CHECK (md5 ~ '^[0-9a-f]{{32}}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, capture_id, bucket),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id)
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

    op.execute(f"""
        CREATE UNIQUE INDEX {PER_BUCKET}
            ON capture_renders (tenant_id, capture_id, render_version,
                                camera_calibration_id, bucket)
            WHERE status = 'rendered'
        """)


def downgrade() -> None:
    op.execute(f"DROP INDEX {PER_BUCKET}")
    op.execute(f"DROP POLICY {RESEARCH_LAB_BINDING} ON public.{TABLE}")
    op.execute(f"DROP POLICY {RESEARCH_LAB_READ} ON public.{TABLE}")
    op.execute(f"DROP TABLE {TABLE}")
