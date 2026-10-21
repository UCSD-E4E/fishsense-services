"""What the signed-out page may say, from tenants that opted in (the owner,
2026-10-20; docs/design/README.md, Home pages).

``tenants.public_totals`` is false until a tenant says yes: the lab first, and
a partner only if they agree. Opting in is an operator's act
(``public-totals``, a dry run by default), never the API's.

The page is read by nobody in particular, so no tenant is in scope and RLS
would show the app role nothing. Two functions run as the owner (BYPASSRLS,
0037's pattern: ``SECURITY DEFINER`` with a pinned ``search_path``) and
return aggregates only, over opted-in tenants alone:

* ``public_totals()``: lengths, the wild fish and fish-model lengths among
  them, species, missions and deployments;
* ``public_wild_fish_by_folder()``: wild fish per mission folder, which the
  API turns into broad regions (``public_store``) and never passes on.

EXECUTE is the app role's alone.

Revision ID: 0055
Revises: 0054
"""

from alembic import context, op

revision = "0055"
down_revision = "0054"

PUBLIC = "JOIN public.tenants t ON t.id = o.tenant_id AND t.public_totals"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tenants ADD COLUMN public_totals boolean NOT NULL DEFAULT false"
    )
    op.execute(f"""
        CREATE FUNCTION public_totals()
        RETURNS TABLE (lengths bigint, wild_fish bigint, model_lengths bigint,
                       species bigint, missions bigint, deployments bigint)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            SELECT
              (SELECT count(*) FROM public.observations o {PUBLIC}),
              (SELECT count(*) FROM public.observations o {PUBLIC}
                WHERE o.fish_model IS NULL),
              (SELECT count(*) FROM public.observations o {PUBLIC}
                WHERE o.fish_model IS NOT NULL),
              (SELECT count(DISTINCT o.scientific_name)
                 FROM public.observations o {PUBLIC}),
              (SELECT count(*) FROM public.missions o {PUBLIC}),
              (SELECT count(*) FROM public.deployments o {PUBLIC})
        $$
        """)
    op.execute(f"""
        CREATE FUNCTION public_wild_fish_by_folder()
        RETURNS TABLE (source_path text, wild_fish bigint)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
            SELECT m.source_path, count(*)
            FROM public.observations o {PUBLIC}
            JOIN public.missions m
              ON m.tenant_id = o.tenant_id AND m.number = o.mission_number
            WHERE o.fish_model IS NULL
            GROUP BY m.source_path
        $$
        """)
    for fn in ("public_totals()", "public_wild_fish_by_folder()"):
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {_app_role()}")


def downgrade() -> None:
    op.execute("DROP FUNCTION public_wild_fish_by_folder()")
    op.execute("DROP FUNCTION public_totals()")
    op.execute("ALTER TABLE tenants DROP COLUMN public_totals")
