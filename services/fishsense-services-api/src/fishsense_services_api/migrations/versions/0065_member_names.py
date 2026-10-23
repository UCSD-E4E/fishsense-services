"""Members by name, for their tenant's admins (the owner, 2026-10-22).

Users were known by their Authentik ``sub`` alone. A skills page of hashes
can't be used (the owner once had to confirm their own by hand), so the owner
chose to keep each person's name and email from their token:

* ``users.name`` and ``users.email``: what the caller's token last said.
* ``record_profile(name, email)``: the one way the app role writes them, to
  the caller's own row only (``app.user_sub``). A missing or blank claim
  leaves what was recorded, and an unchanged one writes nothing. SECURITY
  DEFINER with a pinned ``search_path`` (0027's pattern): the app role has no
  UPDATE on ``users``.
* ``tenant_members()``: the active tenant's members (``app.tenant_id``), with
  their names and emails. A member sees only their own row of ``users`` and
  ``memberships`` (0002), so listing others goes through this. The API shows
  it to the tenant's admins only.

Revision ID: 0065
Revises: 0064
"""

from alembic import context, op

revision = "0065"
down_revision = "0064"

_PINNED = "SECURITY DEFINER SET search_path = pg_catalog, public"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN name text, ADD COLUMN email text")
    op.execute(f"""
        CREATE FUNCTION record_profile(p_name text, p_email text) RETURNS void
        LANGUAGE sql {_PINNED} AS $$
            UPDATE public.users u
            SET name = coalesce(nullif(btrim(p_name), ''), u.name),
                email = coalesce(nullif(btrim(p_email), ''), u.email)
            WHERE u.sub = NULLIF(current_setting('app.user_sub', true), '')
              AND (u.name IS DISTINCT FROM coalesce(nullif(btrim(p_name), ''), u.name)
                   OR u.email IS DISTINCT FROM coalesce(nullif(btrim(p_email), ''), u.email))
        $$
        """)
    op.execute(f"""
        CREATE FUNCTION tenant_members()
        RETURNS TABLE (user_id uuid, sub text, role text, name text, email text,
                       joined_at timestamptz)
        LANGUAGE sql STABLE {_PINNED} AS $$
            SELECT u.id, u.sub, m.role, u.name, u.email, m.created_at
            FROM public.memberships m
            JOIN public.users u ON u.id = m.user_id
            WHERE m.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            ORDER BY lower(coalesce(u.name, u.email, u.sub))
        $$
        """)
    for signature in ("record_profile(text, text)", "tenant_members()"):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_app_role()}")


def downgrade() -> None:
    op.execute("DROP FUNCTION tenant_members()")
    op.execute("DROP FUNCTION record_profile(text, text)")
    op.execute("ALTER TABLE users DROP COLUMN email, DROP COLUMN name")
