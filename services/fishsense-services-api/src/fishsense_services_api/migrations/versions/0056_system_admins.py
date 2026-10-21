"""System admins (docs/design/README.md, Tenants; the owner, 2026-10-20).

``users.is_system_admin`` is false until an operator sets it (``system-admin``,
a dry run by default). A system admin may open any tenant: in one they are not
a member of they arrive **read-only**, and writing takes a deliberate unlock
that lasts a while. Both are recorded in ``system_admin_access`` (append-only:
a ``view`` at most once an hour per tenant, and each ``unlock`` with its end).

The app role has no grant on the table and reads no other tenant: four
``SECURITY DEFINER`` functions (0037's pattern) act for the caller named by
``app.user_sub`` -- the verified token's subject -- and do nothing for anyone
who is not a system admin:

* ``caller_is_system_admin()``;
* ``system_admin_tenant(slug)``: the tenant and any live unlock's end, for a
  tenant the caller is not a member of, recording the visit;
* ``system_admin_unlock(slug, minutes)``: records an unlock, returns its end;
* ``system_admin_tenants()``: every tenant, for the switcher.

Revision ID: 0056
Revises: 0055
"""

from alembic import context, op

revision = "0056"
down_revision = "0055"

CALLER = "NULLIF(current_setting('app.user_sub', true), '')"
ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
#: The caller, if they are a system admin.
ADMIN = f"""
    SELECT u.id FROM public.users u
    WHERE u.sub = {CALLER} AND u.is_system_admin
"""
#: The tenant named ``wanted``, if the caller is not one of its members.
NOT_THEIRS = """
    SELECT t.id FROM public.tenants t
    WHERE t.slug = wanted
      AND NOT EXISTS (SELECT 1 FROM public.memberships m
                      WHERE m.tenant_id = t.id AND m.user_id = me.id)
"""
FUNCTIONS = (
    "caller_is_system_admin()",
    "system_admin_tenant(text)",
    "system_admin_unlock(text, integer)",
    "system_admin_tenants()",
)


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN is_system_admin boolean NOT NULL DEFAULT false"
    )
    op.execute("""
        CREATE TABLE system_admin_access (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES users (id),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            action text NOT NULL CHECK (action IN ('view', 'unlock')),
            at timestamptz NOT NULL DEFAULT now(),
            until timestamptz,
            CHECK ((action = 'unlock') = (until IS NOT NULL))
        )
        """)
    op.execute(
        "CREATE INDEX system_admin_access_user_tenant_idx "
        "ON system_admin_access (user_id, tenant_id, at)"
    )
    op.execute("ALTER TABLE system_admin_access ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE system_admin_access FORCE ROW LEVEL SECURITY")
    # The canonical tenant policy, as on every tenant table (schema_audit). The
    # app role has no grant on this table, so it opens nothing: only the
    # functions below, running as the owner, read or write it.
    op.execute(f"""
        CREATE POLICY tenant_isolation ON system_admin_access
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)

    op.execute(f"""
        CREATE FUNCTION caller_is_system_admin() RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            SELECT EXISTS ({ADMIN})
        $$
        """)
    op.execute(f"""
        CREATE FUNCTION system_admin_tenant(wanted text)
        RETURNS TABLE (tenant_id uuid, unlocked_until timestamptz)
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE
            me record;
            tenant uuid;
        BEGIN
            SELECT * INTO me FROM ({ADMIN}) a;
            IF me.id IS NULL THEN RETURN; END IF;
            SELECT * INTO tenant FROM ({NOT_THEIRS}) t;
            IF tenant IS NULL THEN RETURN; END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.system_admin_access a
                WHERE a.user_id = me.id AND a.tenant_id = tenant
                  AND a.action = 'view' AND a.at > now() - interval '1 hour'
            ) THEN
                INSERT INTO public.system_admin_access (user_id, tenant_id, action)
                VALUES (me.id, tenant, 'view');
            END IF;
            RETURN QUERY
                SELECT tenant, (SELECT max(a.until) FROM public.system_admin_access a
                                WHERE a.user_id = me.id AND a.tenant_id = tenant
                                  AND a.action = 'unlock' AND a.until > now());
        END
        $$
        """)
    op.execute(f"""
        CREATE FUNCTION system_admin_unlock(wanted text, minutes integer)
        RETURNS timestamptz
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE
            me record;
            tenant uuid;
            ends timestamptz;
        BEGIN
            SELECT * INTO me FROM ({ADMIN}) a;
            IF me.id IS NULL OR minutes < 1 OR minutes > 240 THEN RETURN NULL; END IF;
            SELECT * INTO tenant FROM ({NOT_THEIRS}) t;
            IF tenant IS NULL THEN RETURN NULL; END IF;
            ends := now() + make_interval(mins => minutes);
            INSERT INTO public.system_admin_access (user_id, tenant_id, action, until)
            VALUES (me.id, tenant, 'unlock', ends);
            RETURN ends;
        END
        $$
        """)
    op.execute(f"""
        CREATE FUNCTION system_admin_tenants()
        RETURNS TABLE (slug text, name text)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            SELECT t.slug, t.name FROM public.tenants t
            WHERE EXISTS ({ADMIN})
            ORDER BY t.slug
        $$
        """)
    for fn in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {_app_role()}")


def downgrade() -> None:
    for fn in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION {fn}")
    op.execute("DROP TABLE system_admin_access")
    op.execute("ALTER TABLE users DROP COLUMN is_system_admin")
