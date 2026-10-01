"""A research read role: the lab's rows, read-only, bound by RLS.

**PLAN.md §9.20 is open; this builds its stated lean, pending the owner's
decision:** a named research role with read access scoped to the lab tenant,
*not* `BYPASSRLS`. v1's researchers ran `psql -U postgres` over SSH -- a
superuser, so every row and every write (§2.7). This role can read the
research views (0031) and what lies beneath them, and nothing else.

* **`fishsense_research`** is a NOLOGIN group role, created here if the
  cluster lacks it (roles are cluster-wide; a downgrade revokes but never drops
  it, since another database may use it). An operator gives a researcher a
  login in it::

      CREATE ROLE alice LOGIN PASSWORD '...' IN ROLE fishsense_research;
      ALTER ROLE alice SET search_path = v1, public;

  and v1's research SQL runs unchanged. No tenant variable to set.

* **Bound to the lab by RLS.** On every tenant table it can read, two policies
  for this role alone, both `FOR SELECT`:

  - `research_reads_lab` (permissive): the lab's rows, with no
    `app.tenant_id` set -- the canonical tenant policy would show nothing;
  - `research_only_lab` (restrictive): *only* the lab's rows. Permissive
    policies are OR-ed, and anyone may set `app.tenant_id`, so without this a
    research session could name another tenant and read it.

  The lab is found by its slug (`lab`, migrate-v1's tenant) at query time --
  its id exists only once migrate-v1 has run -- through
  `research_tenant_id()`, a SECURITY DEFINER function only this role may call.
  `tenants` forces RLS, so the lookup relies on the schema owner bypassing it,
  as migrate-v1 already requires (PLAN.md §6.4); if it does not, the function
  returns NULL and the role sees nothing: it fails closed.

* **Read-only.** SELECT and nothing else; the schema audit flags any write
  privilege, a missing lab binding on a table the role can read, and the role
  gaining BYPASSRLS or superuser (schema_audit.py).

Not decided here (§9.20): a versioned view contract, as-of reads for frozen
corpora, exports as a job; Superset's role (§9.18).

Revision ID: 0032
Revises: 0031
"""

from alembic import op

# Frozen at migration time (see 0031): the audit's names for the role
# and its policies are the ones created here.
from fishsense_services_api.schema_audit import (
    RESEARCH_LAB_BINDING,
    RESEARCH_LAB_READ,
    RESEARCH_ROLE,
    RESEARCH_TENANT_SLUG,
)

revision = "0032"
down_revision = "0031"

#: The tenant tables beneath the research views.
TENANT_TABLES = (
    "devices",
    "dives",
    "captures",
    "camera_calibrations",
    "laser_calibrations",
    "dive_laser_lines",
    "laser_labels",
    "head_tail_labels",
    "species_labels",
    "slate_labels",
    "laser_predictions",
    "laser_prediction_verdicts",
    "laser_depths",
    "fish",
    "dive_frame_clusters",
    "dive_frame_cluster_captures",
    "measurements",
)
#: Global reference data beneath them (no RLS: shared by every tenant).
GLOBAL_TABLES = (
    "species",
    "fish_models",
    "fish_model_references",
    "calibration_targets",
    "slate_templates",
)
#: v2 views beneath them.
PUBLIC_VIEWS = (
    "current_calibration_targets",
    "current_fish_model_references",
    "current_camera_calibrations",
    "current_dive_laser_lines",
    "current_laser_predictions_gated",
    "current_laser_depths",
)
#: The research views themselves (0031).
FISH_VIEWS = (
    "fish_model_measurement_accuracy",
    "fish_length_estimate",
    "fish_model_species_mislabel_suspects",
)

_LAB = "tenant_id = (SELECT public.research_tenant_id())"


def upgrade() -> None:
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RESEARCH_ROLE}')
            THEN
                CREATE ROLE {RESEARCH_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS
                    NOCREATEDB NOCREATEROLE;
            END IF;
        END
        $$
        """)
    op.execute(f"""
        CREATE FUNCTION public.research_tenant_id() RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$ SELECT id FROM public.tenants WHERE slug = '{RESEARCH_TENANT_SLUG}' $$
        """)
    op.execute("REVOKE ALL ON FUNCTION public.research_tenant_id() FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.research_tenant_id() TO {RESEARCH_ROLE}"
    )
    for table in TENANT_TABLES:
        op.execute(
            f"CREATE POLICY {RESEARCH_LAB_READ} ON public.{table} FOR SELECT "
            f"TO {RESEARCH_ROLE} USING ({_LAB})"
        )
        op.execute(
            f"CREATE POLICY {RESEARCH_LAB_BINDING} ON public.{table} AS RESTRICTIVE "
            f"FOR SELECT TO {RESEARCH_ROLE} USING ({_LAB})"
        )
    op.execute(f"GRANT USAGE ON SCHEMA public, v1 TO {RESEARCH_ROLE}")
    readable = [
        *(f"public.{t}" for t in (*TENANT_TABLES, *GLOBAL_TABLES, *PUBLIC_VIEWS)),
        *(f"public.{v}" for v in FISH_VIEWS),
    ]
    op.execute(f"GRANT SELECT ON {', '.join(readable)} TO {RESEARCH_ROLE}")
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA v1 TO {RESEARCH_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ON ALL TABLES IN SCHEMA v1 FROM {RESEARCH_ROLE}")
    readable = [
        *(f"public.{t}" for t in (*TENANT_TABLES, *GLOBAL_TABLES, *PUBLIC_VIEWS)),
        *(f"public.{v}" for v in FISH_VIEWS),
    ]
    op.execute(f"REVOKE SELECT ON {', '.join(readable)} FROM {RESEARCH_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public, v1 FROM {RESEARCH_ROLE}")
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY {RESEARCH_LAB_BINDING} ON public.{table}")
        op.execute(f"DROP POLICY {RESEARCH_LAB_READ} ON public.{table}")
    op.execute("DROP FUNCTION public.research_tenant_id()")
    # The role is the cluster's, not this database's: it stays.
