"""Schema-wide tenancy audit (PLAN.md §4.1, §9.10).

Every table in ``public`` is classified, and each class has rules:

- **caller-scoped** (``users``, ``memberships``, ``tenants``): resolved before any
  tenant is active; RLS enabled and forced, with their own caller policies.
- **global reference** (e.g. species): shared by all tenants; the app role may
  read but never write.
- **tenant-scoped** -- everything else, by default: a non-null ``tenant_id``
  referencing ``tenants``, RLS enabled *and* forced, and a policy keyed on the
  active tenant for both reads (USING) and writes (WITH CHECK).

And on every table: the app role is not the owner. A new table that fits no
class is a violation, so isolation can't be forgotten -- only opted out of,
explicitly, by naming the table a global reference table.
"""

from collections.abc import Collection

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

CALLER_SCOPED_TABLES = frozenset({"users", "memberships", "tenants"})
GLOBAL_REFERENCE_TABLES: frozenset[str] = frozenset()

_TABLES = text("""
    SELECT c.relname AS name,
           c.relrowsecurity AS rls,
           c.relforcerowsecurity AS forced,
           pg_get_userbyid(c.relowner) AS owner,
           EXISTS (
               SELECT 1 FROM pg_attribute a
               WHERE a.attrelid = c.oid AND a.attname = 'tenant_id'
                 AND a.atttypid = 'uuid'::regtype AND a.attnotnull
                 AND NOT a.attisdropped
           ) AS has_tenant_id,
           EXISTS (
               SELECT 1 FROM pg_constraint con
               JOIN pg_attribute a
                 ON a.attrelid = con.conrelid AND a.attnum = ANY (con.conkey)
               WHERE con.contype = 'f' AND con.conrelid = c.oid
                 AND con.confrelid = 'public.tenants'::regclass
                 AND a.attname = 'tenant_id'
           ) AS references_tenants,
           EXISTS (
               SELECT 1 FROM pg_policies p
               WHERE p.schemaname = 'public' AND p.tablename = c.relname
                 AND p.cmd = 'ALL' AND p.permissive = 'PERMISSIVE'
                 AND p.qual LIKE '%app.tenant_id%'
                 AND p.with_check LIKE '%app.tenant_id%'
           ) AS has_tenant_policy,
           has_table_privilege(:app_role, c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')
               AS app_can_write
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
      AND c.relname <> 'alembic_version'
    ORDER BY c.relname
    """)


async def tenancy_violations(
    conn: AsyncConnection,
    *,
    app_role: str,
    global_tables: Collection[str] = GLOBAL_REFERENCE_TABLES,
    caller_scoped: Collection[str] = CALLER_SCOPED_TABLES,
) -> list[str]:
    """Every way the schema breaks the tenancy rules; empty when it holds."""
    violations = []
    for t in await conn.execute(_TABLES, {"app_role": app_role}):
        if t.owner == app_role:
            violations.append(f"{t.name}: owned by the app role")
        if t.name in global_tables:
            if t.app_can_write:
                violations.append(f"{t.name}: global table the app role can write")
        elif t.name in caller_scoped:
            if not (t.rls and t.forced):
                violations.append(f"{t.name}: RLS not enabled and forced")
        else:
            if not (t.has_tenant_id and t.references_tenants):
                violations.append(
                    f"{t.name}: needs a NOT NULL uuid tenant_id referencing tenants"
                )
            if not (t.rls and t.forced):
                violations.append(f"{t.name}: RLS not enabled and forced")
            if not t.has_tenant_policy:
                violations.append(f"{t.name}: no read+write policy on app.tenant_id")
    return violations
