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

Every view must be ``security_invoker``, in ``public`` and in every other
schema of ours (the research views live in ``v1``): by default a view runs with
its *owner's* rights, which bypasses RLS and would show every tenant's rows.

And on every table: the app role is not the owner. A new table that fits no
class is a violation, so isolation can't be forgotten -- only opted out of,
explicitly, by naming the table a global reference table.

**The research role** (migration research_02; PLAN.md §9.20's lean), when it
exists: never superuser or BYPASSRLS, never able to write a table or view, and
bound to the lab on every tenant table it can read -- a restrictive policy
(:data:`RESEARCH_LAB_BINDING`), because the canonical tenant policy opens a
table to anyone who sets ``app.tenant_id``. Its one permissive policy per table
(:data:`RESEARCH_LAB_READ`) is allowed only exactly as written.
"""

from collections import defaultdict
from collections.abc import Collection, Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

CALLER_SCOPED_TABLES = frozenset({"users", "memberships", "tenants"})
GLOBAL_REFERENCE_TABLES = frozenset(
    {
        "species",
        "calibration_targets",
        "fish_models",
        "fish_model_references",
        "slate_templates",
    }
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

#: The research read role and the tenant it is bound to (migration research_02).
RESEARCH_ROLE = "fishsense_research"
RESEARCH_TENANT_SLUG = "lab"
#: Its permissive policy (the lab's rows without a tenant setting) and its
#: restrictive one (only the lab's rows, whatever the setting).
RESEARCH_LAB_READ = "research_reads_lab"
RESEARCH_LAB_BINDING = "research_only_lab"
#: ``tenant_id = <the lab>`` as Postgres deparses it into ``pg_policies``
#: (Postgres 17). A scalar subquery, so the lookup runs once per query, not per
#: row.
RESEARCH_TENANT_EXPRESSION = (
    "(tenant_id = ( SELECT research_tenant_id() AS research_tenant_id))"
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
               AS app_can_write,
           -- has_table_privilege raises for an unknown role; CASE guards it.
           CASE WHEN to_regrole(:research_role) IS NULL THEN false
                ELSE has_table_privilege(:research_role, c.oid, 'SELECT')
           END AS research_can_read
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
      AND c.relname <> 'alembic_version'
    ORDER BY c.relname
    """)

_POLICIES = text("""
    SELECT tablename AS table, policyname AS name, permissive, cmd,
           roles::text[] AS roles, qual, with_check
    FROM pg_policies
    WHERE schemaname = 'public'
    ORDER BY tablename, policyname
    """)

#: Our schemas: every one but Postgres's own. Views outside ``public`` (the
#: research views in ``v1``) are named with their schema.
_OUR_SCHEMA = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema')"
    " AND n.nspname NOT LIKE 'pg\\_%'"
)
_QUALIFIED_NAME = (
    "CASE WHEN n.nspname = 'public' THEN c.relname"
    " ELSE n.nspname || '.' || c.relname END"
)

_VIEWS_RUNNING_AS_OWNER = text(f"""
    SELECT {_QUALIFIED_NAME} AS name
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE {_OUR_SCHEMA} AND c.relkind IN ('v', 'm')
      AND NOT coalesce('security_invoker=true' = ANY (c.reloptions), false)
    ORDER BY n.nspname, c.relname
    """)

_RESEARCH_ROLE = text("""
    SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :research_role
    """)

_RESEARCH_WRITES = text(f"""
    SELECT {_QUALIFIED_NAME} AS name
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE {_OUR_SCHEMA} AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND has_table_privilege(:research_role, c.oid,
                              'INSERT, UPDATE, DELETE, TRUNCATE')
    ORDER BY n.nspname, c.relname
    """)

_FOREIGN_KEYS = text("""
    SELECT con.conname AS name, src.relname AS table, dst.relname AS referenced,
           (SELECT array_agg(a.attname ORDER BY k.ord)
              FROM unnest(con.conkey) WITH ORDINALITY k (attnum, ord)
              JOIN pg_attribute a
                ON a.attrelid = con.conrelid AND a.attnum = k.attnum) AS columns,
           (SELECT array_agg(a.attname ORDER BY k.ord)
              FROM unnest(con.confkey) WITH ORDINALITY k (attnum, ord)
              JOIN pg_attribute a
                ON a.attrelid = con.confrelid AND a.attnum = k.attnum)
               AS referenced_columns
    FROM pg_constraint con
    JOIN pg_class src ON src.oid = con.conrelid
    JOIN pg_class dst ON dst.oid = con.confrelid
    JOIN pg_namespace n ON n.oid = src.relnamespace
    WHERE con.contype = 'f' AND n.nspname = 'public'
    ORDER BY src.relname, con.conname
    """)


async def tenancy_violations(
    conn: AsyncConnection,
    *,
    app_role: str,
    global_tables: Collection[str] = GLOBAL_REFERENCE_TABLES,
    caller_scoped: Collection[str] = CALLER_SCOPED_TABLES,
    research_role: str = RESEARCH_ROLE,
) -> list[str]:
    """Every way the schema breaks the tenancy rules; empty when it holds."""
    policies, restrictive = defaultdict(list), defaultdict(list)
    for policy in await conn.execute(_POLICIES):
        if policy.permissive == "PERMISSIVE":
            policies[policy.table].append(policy)
        else:
            restrictive[policy.table].append(policy)

    violations = []
    roles = {"app_role": app_role, "research_role": research_role}
    for t in await conn.execute(_TABLES, roles):
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
            violations += _tenant_policy_violations(
                t.name, policies[t.name], research_role
            )
            if t.research_can_read and not any(
                _is_research_policy(p, research_role, RESEARCH_LAB_BINDING)
                for p in restrictive[t.name]
            ):
                violations.append(
                    f"{t.name}: {research_role} can read it but is not bound to"
                    f" the lab -- it needs the restrictive {RESEARCH_LAB_BINDING}"
                    " policy, or setting app.tenant_id opens another tenant"
                )

    for view in await conn.execute(_VIEWS_RUNNING_AS_OWNER):
        violations.append(
            f"{view.name}: view must be WITH (security_invoker = true) -- by"
            " default a view runs as its owner, which bypasses RLS"
        )

    violations += await _research_role_violations(conn, research_role)

    not_tenant_scoped = set(global_tables) | set(caller_scoped)
    for fk in await conn.execute(_FOREIGN_KEYS):
        between_tenant_tables = (
            fk.table not in not_tenant_scoped and fk.referenced not in not_tenant_scoped
        )
        if between_tenant_tables and not _carries_tenant_id(fk):
            violations.append(
                f"{fk.table}: reference {fk.name} to {fk.referenced} lacks tenant_id"
                " -- use a composite key (tenant_id, ...) so it can't cross tenants"
            )
    return violations


def _carries_tenant_id(fk) -> bool:
    """Both sides hold tenant_id at the same position of the key."""
    pairs = zip(fk.columns, fk.referenced_columns, strict=True)
    return ("tenant_id", "tenant_id") in pairs


def _is_canonical_tenant_policy(policy) -> bool:
    return (
        policy.cmd == "ALL"
        and list(policy.roles) == ["public"]
        and policy.qual == CANONICAL_TENANT_EXPRESSION
        and policy.with_check == CANONICAL_TENANT_EXPRESSION
    )


def _is_research_policy(policy, research_role: str, name: str) -> bool:
    """The research role's lab policy, exactly: SELECT, that role alone, the lab."""
    return (
        policy.name == name
        and policy.cmd == "SELECT"
        and list(policy.roles) == [research_role]
        and policy.qual == RESEARCH_TENANT_EXPRESSION
        and policy.with_check is None
    )


async def _research_role_violations(conn: AsyncConnection, role: str) -> list[str]:
    found = (await conn.execute(_RESEARCH_ROLE, {"research_role": role})).first()
    if found is None:
        return []
    violations = []
    if found.rolsuper or found.rolbypassrls:
        violations.append(
            f"{role}: must be neither superuser nor BYPASSRLS -- RLS is what binds"
            " it to the lab"
        )
    for relation in await conn.execute(_RESEARCH_WRITES, {"research_role": role}):
        violations.append(f"{relation.name}: the research role {role} can write it")
    return violations


def _tenant_policy_violations(
    table: str, policies: list, research_role: str = RESEARCH_ROLE
) -> list[str]:
    canonical = [p for p in policies if _is_canonical_tenant_policy(p)]
    violations = [
        f"{table}: permissive policy {p.name!r} is not the canonical tenant policy"
        " (permissive policies are OR-ed, so it can widen access)"
        for p in policies
        if not _is_canonical_tenant_policy(p)
        and not _is_research_policy(p, research_role, RESEARCH_LAB_READ)
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
