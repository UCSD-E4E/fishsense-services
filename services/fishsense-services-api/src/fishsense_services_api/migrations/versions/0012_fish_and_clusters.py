"""Fish-model identity, fish, and dive frame clusters.

**fish_models** (global): a physical fish model or calibration target is a row,
so everything reaches it by key. v1 matched ``fish.name`` against
``fishmodelreference.name`` as strings -- the path imwut's queries got wrong
(PLAN.md §2.7). Reference lengths (versioned, 0004) now reference the model by
name, which is unique here; existing names are registered first.

**fish** (tenant): a real animal of a species, or a fish model -- never both.
One fish per model per tenant. Never deleted: measurements point at them.

**dive_frame_clusters** (tenant): frames grouped by prediction (stage 1) or
Label Studio regrouping (stage 6.1), bound to a fish at measurement. Only
migrated clusters may lack a dive or a formation. Their capture memberships
belong to the cluster and go with it (``ON DELETE CASCADE`` -- the one
within-tenant cascade, because membership has no life of its own).

Revision ID: 0012
Revises: 0011
"""

from alembic import context, op

revision = "0012"
down_revision = "0011"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _tenant_scoped(table: str, app_role: str, privileges: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    op.execute(f"GRANT {privileges} ON {table} TO {app_role}")


def upgrade() -> None:
    app_role = _app_role()

    op.execute("""
        CREATE TABLE fish_models (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL UNIQUE,
            notes text,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """)
    op.execute(f"GRANT SELECT ON fish_models TO {app_role}")
    op.execute("""
        INSERT INTO fish_models (name)
        SELECT DISTINCT name FROM fish_model_references
        ON CONFLICT (name) DO NOTHING
        """)
    op.execute("""
        ALTER TABLE fish_model_references
            ADD CONSTRAINT fish_model_references_name_fkey
            FOREIGN KEY (name) REFERENCES fish_models (name) ON UPDATE CASCADE
        """)

    op.execute("""
        CREATE TABLE fish (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            v1_id bigint UNIQUE,
            species_id uuid REFERENCES species (id),
            fish_model_id uuid REFERENCES fish_models (id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, fish_model_id),
            CONSTRAINT fish_species_or_model_check
                CHECK (species_id IS NULL OR fish_model_id IS NULL)
        )
        """)
    # Never deleted: measurements point at fish.
    _tenant_scoped("fish", app_role, "SELECT, INSERT, UPDATE")

    op.execute("""
        CREATE TABLE dive_frame_clusters (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            v1_id bigint UNIQUE,
            dive_id uuid,
            formed_by text
                CONSTRAINT dive_frame_clusters_formed_by_check
                CHECK (formed_by IN ('prediction', 'label_studio')),
            fish_id uuid,
            updated_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, dive_id) REFERENCES dives (tenant_id, id),
            FOREIGN KEY (tenant_id, fish_id) REFERENCES fish (tenant_id, id),
            CONSTRAINT dive_frame_clusters_known_check CHECK (
                (dive_id IS NOT NULL AND formed_by IS NOT NULL) OR v1_id IS NOT NULL
            )
        )
        """)
    _tenant_scoped("dive_frame_clusters", app_role, "SELECT, INSERT, UPDATE, DELETE")

    op.execute("""
        CREATE TABLE dive_frame_cluster_captures (
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            cluster_id uuid NOT NULL,
            capture_id uuid NOT NULL,
            PRIMARY KEY (cluster_id, capture_id),
            FOREIGN KEY (tenant_id, cluster_id)
                REFERENCES dive_frame_clusters (tenant_id, id) ON DELETE CASCADE,
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id)
        )
        """)
    _tenant_scoped(
        "dive_frame_cluster_captures", app_role, "SELECT, INSERT, UPDATE, DELETE"
    )


def downgrade() -> None:
    op.execute("DROP TABLE dive_frame_cluster_captures")
    op.execute("DROP TABLE dive_frame_clusters")
    op.execute("DROP TABLE fish")
    op.execute(
        "ALTER TABLE fish_model_references DROP CONSTRAINT fish_model_references_name_fkey"
    )
    op.execute("DROP TABLE fish_models")
