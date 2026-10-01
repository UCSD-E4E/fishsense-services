"""Let the API create the caller's own user row on first login.

The INSERT policy admits exactly one row: the caller's (``app.user_sub``).
Memberships stay read-only to the app role -- access is granted by admins.

Revision ID: 0003
Revises: 0002
"""

from alembic import context, op

revision = "0003"
down_revision = "0002"

ACTIVE_SUB = "NULLIF(current_setting('app.user_sub', true), '')"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    op.execute(f"""
        CREATE POLICY provision_own_user ON users FOR INSERT
            WITH CHECK (sub = {ACTIVE_SUB})
        """)
    op.execute(f"GRANT INSERT ON users TO {_app_role()}")


def downgrade() -> None:
    op.execute(f"REVOKE INSERT ON users FROM {_app_role()}")
    op.execute("DROP POLICY provision_own_user ON users")
