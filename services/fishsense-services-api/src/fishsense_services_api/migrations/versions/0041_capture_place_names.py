"""Place names the server derives from a capture's coordinates.

FishSense data is often collected with no internet, so the app frequently
can't name the place when a capture is taken, and a committed capture is
immutable (fishsense-mobile #72). The API names it instead, on our own Photon
geocoder (place_names.py), in the background after a commit or a read.

``capture_place_names`` keeps each answer apart from what the app sent: the
name (NULL when no named place is in range -- an answer, not an outage, which
is never recorded), the geocoder that gave it, and its raw answer, so a name
can be told apart from the app's and derived again. Append-only; the current
name is a capture's latest.

Tenant-scoped under forced RLS, SELECT and INSERT for the app role.

Revision ID: 0041
Revises: 0040
"""

from alembic import context, op

revision = "0041"
down_revision = "0040"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE capture_place_names (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            capture_id uuid NOT NULL,
            place_name text,
            source text NOT NULL,
            properties jsonb NOT NULL DEFAULT '{}'::jsonb,
            derived_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id)
        )
        """)
    op.execute("""
        CREATE INDEX capture_place_names_tenant_id_capture_id_derived_at_idx
            ON capture_place_names (tenant_id, capture_id, derived_at)
        """)
    op.execute("ALTER TABLE capture_place_names ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE capture_place_names FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON capture_place_names
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    op.execute(f"GRANT SELECT, INSERT ON capture_place_names TO {_app_role()}")


def downgrade() -> None:
    op.execute("DROP TABLE capture_place_names")
