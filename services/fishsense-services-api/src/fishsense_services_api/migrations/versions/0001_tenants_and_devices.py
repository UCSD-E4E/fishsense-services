"""Tenants, and devices as the first tenant-scoped table under RLS.

Revision ID: 0001
Revises:
"""

from alembic import context, op

revision = "0001"
down_revision = None

# One policy shape for every tenant-scoped table: the row's tenant must equal
# the transaction's active tenant. `current_setting(..., true)` is NULL when the
# setting was never defined in this session, and '' after a SET LOCAL has ended
# -- both must match nothing, hence NULLIF.
ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    app_role = _app_role()

    op.execute("""
        CREATE TABLE tenants (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            slug text NOT NULL UNIQUE,
            name text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """)
    op.execute("""
        CREATE TABLE devices (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            kind text NOT NULL,
            serial text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, serial)
        )
        """)

    op.execute("ALTER TABLE devices ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE devices FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON devices
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)

    op.execute(f"GRANT USAGE ON SCHEMA public TO {app_role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON devices TO {app_role}")


def downgrade() -> None:
    op.execute("DROP TABLE devices")
    op.execute("DROP TABLE tenants")
