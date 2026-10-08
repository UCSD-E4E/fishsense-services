"""A partner joins their org's tenant automatically, from the ``org`` claim.

PLAN.md §9.10 left this open. krg-infra ``collaborator_invites.tf`` gives each
partner org one reusable Authentik invite, and every account made through it
carries ``org = <the org>`` (pinned server-side; a link holder can't change it),
emitted as the ``org`` claim. **Decided:** a tenant that claims that org takes
such a caller in as a ``member`` on first sight -- no operator per person.

- ``tenants.org_claim``: the org whose accounts join this tenant. At most one
  tenant per org (UNIQUE); NULL -- the lab, and every tenant by default --
  claims none. Set by an operator (``fishsense-services-api add-tenant``); the
  app role can still only read ``tenants``.
- ``join_claimed_tenant()``: the one way the app role adds a membership. No
  arguments: it reads the caller (``app.user_sub``) and their org
  (``app.user_org``, from the verified token) from the transaction's scope, so
  it can admit only the caller, only into the tenant that claims their org,
  only as ``member``, and never touches a membership that already exists (an
  operator's promotion survives the next login).

``memberships`` keeps no INSERT grant or policy for the app role: the function
is ``SECURITY DEFINER`` with a pinned ``search_path`` (0027's pattern), runs as
the owner (BYPASSRLS), and EXECUTE is the app role's alone.

Leaving the org (an operator clearing the attribute) does not remove the
membership; offboarding is deactivating the Authentik account, or an operator
deleting the row and clearing the claim.

Revision ID: 0037
Revises: 0036
"""

from alembic import context, op

revision = "0037"
down_revision = "0036"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN org_claim text UNIQUE")
    op.execute("""
        CREATE FUNCTION join_claimed_tenant() RETURNS void
        LANGUAGE sql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            INSERT INTO public.memberships (tenant_id, user_id, role)
            SELECT t.id, u.id, 'member'
            FROM public.tenants t, public.users u
            WHERE t.org_claim = NULLIF(current_setting('app.user_org', true), '')
              AND u.sub = NULLIF(current_setting('app.user_sub', true), '')
            ON CONFLICT (tenant_id, user_id) DO NOTHING
        $$
        """)
    op.execute("REVOKE ALL ON FUNCTION join_claimed_tenant() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION join_claimed_tenant() TO {_app_role()}")


def downgrade() -> None:
    op.execute("DROP FUNCTION join_claimed_tenant()")
    op.execute("ALTER TABLE tenants DROP COLUMN org_claim")
