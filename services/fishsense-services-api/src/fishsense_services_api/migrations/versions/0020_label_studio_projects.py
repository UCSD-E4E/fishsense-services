"""Which Label Studio project holds which dive's labels of which kind.

v1 found a dive's project only by searching Label Studio for its title
(`{name} #{dive_id} - Species Labeling`). v2 records the link when the
orchestrator creates a project, and migrate-v1 fills it from the projects v1's
labels point at. Titles still embed the dive's `number`, so they match v1's.

`dive_id` is nullable: a checkerboard-lattice project is not one dive's.

Revision ID: 0020
Revises: 0019
"""

from alembic import context, op

revision = "0020"
down_revision = "0019"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
KINDS = ("laser", "head_tail", "slate", "species", "checkerboard_lattice")


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    app_role = _app_role()
    kinds = ", ".join(f"'{k}'" for k in KINDS)
    op.execute(f"""
        CREATE TABLE label_studio_projects (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            dive_id uuid,
            kind text NOT NULL
                CONSTRAINT label_studio_projects_kind_check CHECK (kind IN ({kinds})),
            ls_project_id integer NOT NULL,
            title text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, kind, ls_project_id),
            FOREIGN KEY (tenant_id, dive_id) REFERENCES dives (tenant_id, id)
        )
        """)
    op.execute("ALTER TABLE label_studio_projects ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE label_studio_projects FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON label_studio_projects
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON label_studio_projects TO {app_role}")


def downgrade() -> None:
    op.execute("DROP TABLE label_studio_projects")
