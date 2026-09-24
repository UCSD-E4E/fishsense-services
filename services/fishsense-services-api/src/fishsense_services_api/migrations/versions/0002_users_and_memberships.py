"""Users keyed on the IdP subject, their memberships, and caller-scoped reads.

Membership is resolved before any tenant is active, so these tables are scoped
to the *caller* (``app.user_sub``) instead: a caller reads their own user row,
their own memberships, and only the tenants they belong to -- the lookup can't
be used to enumerate other users or partner tenants. The app role may only
read them; granting access is an administrative act, not an API side effect.

Revision ID: 0002
Revises: 0001
"""

from alembic import context, op

revision = "0002"
down_revision = "0001"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
ACTIVE_SUB = "NULLIF(current_setting('app.user_sub', true), '')"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    app_role = _app_role()

    op.execute("""
        CREATE TABLE users (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            sub text NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """)
    op.execute("""
        CREATE TABLE memberships (
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            role text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, user_id)
        )
        """)

    for table in ("users", "memberships", "tenants"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(f"""
        CREATE POLICY own_user ON users FOR SELECT
            USING (sub = {ACTIVE_SUB})
        """)
    op.execute(f"""
        CREATE POLICY own_memberships ON memberships FOR SELECT
            USING (user_id IN (SELECT id FROM users WHERE sub = {ACTIVE_SUB}))
        """)
    # The memberships subquery is itself filtered to the caller by its policy.
    op.execute(f"""
        CREATE POLICY visible_tenants ON tenants FOR SELECT
            USING (
                id = {ACTIVE_TENANT}
                OR id IN (SELECT tenant_id FROM memberships)
            )
        """)

    op.execute(f"GRANT SELECT ON users, memberships, tenants TO {app_role}")


def downgrade() -> None:
    op.execute("DROP POLICY visible_tenants ON tenants")
    op.execute("ALTER TABLE tenants NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE memberships")
    op.execute("DROP TABLE users")
