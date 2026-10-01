"""Label Studio labels (laser, head/tail, slate, species) and sync cursors.

The four label tables share one core, taken from v1's label tables:

- the capture it labels; the Label Studio project, task and labeler ids;
- ``completed`` / ``superseded`` / ``needs_reprocess`` -- three distinct states
  every reader must honour (PLAN.md §2.1);
- ``ls_updated_at`` (Label Studio's clock) and the raw ``ls_payload``;
- **``source``** -- human, gate auto-accept, model pre-annotation, import. v1
  cannot recover this; only migrated rows (``v1_id``) may leave it unknown.

A **sentinel** (v1's representation: no Label Studio project) may carry no task.
One label per capture per project; one label per Label Studio task.

Labels mirror Label Studio, so sync updates them in place (UPDATE granted), but
they are never deleted -- ``superseded`` records retirement (no DELETE).
Labelers are recorded by Label Studio user id; mapping them to v2 users is
PLAN.md §9.14.

Revision ID: 0010
Revises: 0009
"""

from alembic import context, op

revision = "0010"
down_revision = "0009"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
SOURCES = ("human", "auto_accept", "pre_annotation", "import")
KINDS = ("laser", "head_tail", "slate", "species")

EXTRA_COLUMNS = {
    "laser_labels": """
        x double precision,
        y double precision,
        label text,
    """,
    "head_tail_labels": """
        head_x double precision,
        head_y double precision,
        tail_x double precision,
        tail_y double precision,
    """,
    "slate_labels": """
        upside_down boolean,
        reference_points jsonb,
        slate_rectangle jsonb,
        skipped_points jsonb,
        image_url text,
    """,
    "species_labels": """
        image_url text,
        grouping text,
        top_three_photos_of_group boolean,
        content_of_image text,
        fish_measurable_category text,
        fish_angle_category text,
        fish_curved_category text,
        fish_angle_degrees double precision,
    """,
}


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


def _label_table(table: str) -> str:
    sources = ", ".join(f"'{s}'" for s in SOURCES)
    return f"""
        CREATE TABLE {table} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            v1_id bigint UNIQUE,
            capture_id uuid NOT NULL,
            source text
                CONSTRAINT {table}_source_check CHECK (source IN ({sources})),
            ls_project_id integer,
            ls_task_id integer,
            ls_labeler_id integer,
            ls_updated_at timestamptz,
            completed boolean NOT NULL DEFAULT false,
            superseded boolean NOT NULL DEFAULT false,
            needs_reprocess boolean NOT NULL DEFAULT false,
            ls_payload jsonb,
            {EXTRA_COLUMNS[table]}
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            UNIQUE (tenant_id, ls_task_id),
            UNIQUE (tenant_id, capture_id, ls_project_id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            CONSTRAINT {table}_source_known_check
                CHECK (source IS NOT NULL OR v1_id IS NOT NULL),
            CONSTRAINT {table}_sentinel_has_no_task_check
                CHECK (ls_project_id IS NOT NULL OR ls_task_id IS NULL)
        )
        """


def upgrade() -> None:
    app_role = _app_role()
    kinds = ", ".join(f"'{k}'" for k in KINDS)

    for table in EXTRA_COLUMNS:
        op.execute(_label_table(table))
        # Sync updates in place; nothing deletes a label (superseded retires it).
        _tenant_scoped(table, app_role, "SELECT, INSERT, UPDATE")

    op.execute(f"""
        CREATE TABLE label_studio_sync_cursors (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            v1_id bigint UNIQUE,
            kind text NOT NULL
                CONSTRAINT label_studio_sync_cursors_kind_check
                CHECK (kind IN ({kinds})),
            ls_project_id integer NOT NULL,
            last_synced_at timestamptz,
            UNIQUE (tenant_id, kind, ls_project_id)
        )
        """)
    _tenant_scoped("label_studio_sync_cursors", app_role, "SELECT, INSERT, UPDATE")


def downgrade() -> None:
    op.execute("DROP TABLE label_studio_sync_cursors")
    for table in reversed(list(EXTRA_COLUMNS)):
        op.execute(f"DROP TABLE {table}")
