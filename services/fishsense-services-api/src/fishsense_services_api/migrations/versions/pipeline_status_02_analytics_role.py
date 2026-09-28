"""`fishsense_analytics`: Superset's read role, bound to the lab tenant by RLS.

v1's Superset connected as its own role with SELECT on every table plus
default privileges (fishsense-lite@77e8f8e5
deploy/pg_volumes/scripts/2025-09-02_create_database.sql). Under v2's RLS
such a role sees nothing -- no tenant is set -- or, with BYPASSRLS, every
tenant. PLAN.md §9.18 (analytics under RLS) and §9.20 (research access) are
open; this builds their stated lean, **pending the owner's decision**: a named
read role scoped to the lab tenant through RLS, not BYPASSRLS.

* ``fishsense_analytics`` is a NOLOGIN, NOBYPASSRLS group. It may SELECT
  ``dive_pipeline_status`` and the tables and views under it (a
  ``security_invoker`` view reads them as its caller), and nothing else; it
  writes nothing;
* a login is a member bound to the lab by its own setting, which the
  canonical tenant policy reads::

      CREATE ROLE superset LOGIN PASSWORD '...' IN ROLE fishsense_analytics;
      ALTER ROLE superset SET app.tenant_id = '<the lab tenant id>';

  A setting on the group itself would reach no member: Postgres applies a
  role's settings only at that role's own login;
* the setting alone is not a binding -- a session can ``SET`` another tenant
  (Superset's SQL Lab runs what it is given). So every tenant table the role
  may read carries a **restrictive** policy for it, ``tenant_id =
  analytics_tenant_id()``: restrictive policies AND with the canonical one,
  so the role reads the lab's rows or none. The schema audit allows exactly
  this (a restrictive policy can only narrow);
* ``analytics_tenant_id()`` is the lab tenant (slug ``lab``, as migrate-v1
  names it): SECURITY DEFINER so the role needs no grant on ``tenants``, and
  revoked from PUBLIC.

The role is cluster-wide, so it is created only if missing. Creating it needs
CREATEROLE, which the schema owner that runs migrations has in every
deployment so far (the container's superuser).

Revision ID: pipeline_status_02
Revises: pipeline_status_01
"""

from alembic import op

revision = "pipeline_status_02"
down_revision = "pipeline_status_01"

ROLE = "fishsense_analytics"
LAB_SLUG = "lab"

#: What `dive_pipeline_status` reads, transitively: the tenant tables and the
#: views between them (all `security_invoker`, so each is read as the caller).
TENANT_TABLES = (
    "camera_calibrations",
    "captures",
    "dive_frame_cluster_captures",
    "dive_frame_clusters",
    "dives",
    "fish",
    "head_tail_labels",
    "head_tail_predictions",
    "laser_calibration_refusal_clears",
    "laser_calibrations",
    "laser_depth_refusals",
    "laser_depths",
    "laser_labels",
    "laser_predictions",
    "measurement_refusals",
    "measurements",
    "slate_labels",
    "species_labels",
)
GLOBAL_TABLES = ("calibration_targets", "fish_models", "slate_templates")
VIEWS = (
    "current_calibration_targets",
    "current_camera_calibrations",
    "current_head_tail_predictions",
    "current_laser_calibrations",
    "current_laser_depths",
    "current_measurements",
    "dive_laser_geometry",
    "dive_pipeline_status",
    "effective_laser_calibrations",
    "laser_depth_work",
    "measurement_subjects",
    "measurement_work",
)
POLICY = "analytics_lab_only"


def upgrade() -> None:
    op.execute(f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS
                    NOCREATEDB NOCREATEROLE;
            END IF;
        END $$
        """)
    op.execute(f"""
        CREATE FUNCTION analytics_tenant_id() RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
        AS $$ SELECT id FROM public.tenants WHERE slug = '{LAB_SLUG}' $$
        """)
    op.execute("REVOKE ALL ON FUNCTION analytics_tenant_id() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION analytics_tenant_id() TO {ROLE}")

    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    relations = (*TENANT_TABLES, *GLOBAL_TABLES, *VIEWS)
    op.execute(f"GRANT SELECT ON {', '.join(relations)} TO {ROLE}")
    for table in TENANT_TABLES:
        # `(SELECT ...)`: evaluated once per query, not per row.
        op.execute(f"""
            CREATE POLICY {POLICY} ON {table} AS RESTRICTIVE FOR SELECT
                TO {ROLE} USING (tenant_id = (SELECT analytics_tenant_id()))
            """)


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY {POLICY} ON {table}")
    relations = (*TENANT_TABLES, *GLOBAL_TABLES, *VIEWS)
    op.execute(f"REVOKE SELECT ON {', '.join(relations)} FROM {ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE}")
    op.execute("DROP FUNCTION analytics_tenant_id()")
    # The role is cluster-wide and may hold grants in another database; it is
    # left for an operator to drop once nothing uses it.
