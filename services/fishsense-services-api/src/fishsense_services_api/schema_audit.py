"""Schema-wide tenancy audit (PLAN.md §4.1, §9.10).

Every table in ``public`` is classified, and each class has rules:

- **caller-scoped** (``users``, ``memberships``, ``tenants``): resolved before any
  tenant is active; RLS enabled and forced, and *only* their known policies.
- **global reference** (e.g. species): shared by all tenants; the app role may
  read but never write.
- **tenant-scoped** -- everything else, by default: a non-null ``tenant_id``
  referencing ``tenants``, RLS enabled *and* forced, and exactly one permissive
  policy -- the canonical tenant policy, for every command and every role.

Why "exactly one": Postgres OR-s permissive policies together, so a single extra
``USING (true)`` policy opens a table to every tenant, and a policy that merely
*mentions* ``app.tenant_id`` can still be loosened (``… OR true``). So the audit
compares the policy's expressions to the canonical one exactly, and flags every
other permissive policy. Restrictive policies are always fine: they can only
narrow what is visible.

And on every table: the app role is not the owner. A new table that fits no
class is a violation, so isolation can't be forgotten -- only opted out of,
explicitly, by naming the table a global reference table.
"""

from collections import defaultdict
from collections.abc import Collection, Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

CALLER_SCOPED_TABLES = frozenset({"users", "memberships", "tenants"})
GLOBAL_REFERENCE_TABLES = frozenset(
    {"species", "calibration_targets", "fish_model_references", "slate_templates"}
)

#: The only permissive policies caller-scoped tables may carry: (name, command).
CALLER_POLICIES: Mapping[str, frozenset[tuple[str, str]]] = {
    "users": frozenset({("own_user", "SELECT"), ("provision_own_user", "INSERT")}),
    "memberships": frozenset({("own_memberships", "SELECT")}),
    "tenants": frozenset({("visible_tenants", "SELECT")}),
}

#: ``tenant_id = <active tenant>`` exactly as Postgres deparses it into
#: ``pg_policies`` (pinned to the Postgres major we run: 17).
CANONICAL_TENANT_EXPRESSION = (
    "(tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true),"
    " ''::text))::uuid)"
)

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
           has_table_privilege(:app_role, c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')
               AS app_can_write
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
      AND c.relname <> 'alembic_version'
    ORDER BY c.relname
    """)

_PERMISSIVE_POLICIES = text("""
    SELECT tablename AS table, policyname AS name, cmd,
           roles::text[] AS roles, qual, with_check
    FROM pg_policies
    WHERE schemaname = 'public' AND permissive = 'PERMISSIVE'
    ORDER BY tablename, policyname
    """)


async def tenancy_violations(
    conn: AsyncConnection,
    *,
    app_role: str,
    global_tables: Collection[str] = GLOBAL_REFERENCE_TABLES,
    caller_scoped: Collection[str] = CALLER_SCOPED_TABLES,
) -> list[str]:
    """Every way the schema breaks the tenancy rules; empty when it holds."""
    policies = defaultdict(list)
    for policy in await conn.execute(_PERMISSIVE_POLICIES):
        policies[policy.table].append(policy)

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
            violations += _caller_policy_violations(t.name, policies[t.name])
        else:
            if not (t.has_tenant_id and t.references_tenants):
                violations.append(
                    f"{t.name}: needs a NOT NULL uuid tenant_id referencing tenants"
                )
            if not (t.rls and t.forced):
                violations.append(f"{t.name}: RLS not enabled and forced")
            violations += _tenant_policy_violations(t.name, policies[t.name])
    return violations


def _is_canonical_tenant_policy(policy) -> bool:
    return (
        policy.cmd == "ALL"
        and list(policy.roles) == ["public"]
        and policy.qual == CANONICAL_TENANT_EXPRESSION
        and policy.with_check == CANONICAL_TENANT_EXPRESSION
    )


def _tenant_policy_violations(table: str, policies: list) -> list[str]:
    canonical = [p for p in policies if _is_canonical_tenant_policy(p)]
    violations = [
        f"{table}: permissive policy {p.name!r} is not the canonical tenant policy"
        " (permissive policies are OR-ed, so it can widen access)"
        for p in policies
        if not _is_canonical_tenant_policy(p)
    ]
    if not canonical:
        violations.append(f"{table}: no canonical read+write policy on app.tenant_id")
    return violations


def _caller_policy_violations(table: str, policies: list) -> list[str]:
    allowed = CALLER_POLICIES.get(table, frozenset())
    return [
        f"{table}: unexpected permissive policy {p.name!r} ({p.cmd})"
        for p in policies
        if (p.name, p.cmd) not in allowed
    ]
