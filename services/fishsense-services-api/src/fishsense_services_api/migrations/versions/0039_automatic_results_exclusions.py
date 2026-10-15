"""automatic_results_exclusions: dives the automatic chain must leave alone.

New in v2 (2026-10-15). The automatic-results backlog takes dives of any
priority, and some produce results that look plausible and are wrong: dive
466 holds three laser states (a label-free fit would blend three
geometries), dive 442 was shot overexposed (its automatic dots are not
reliable). An operator lists such a dive here, with the reason, and the
cohort skips it (`automatic_results_store.automatic_results_cohort`). A dive
run by hand (`AutomaticResultsForDiveWorkflow`) is not stopped: this is the
backlog's list.

Tenant-scoped under forced RLS. The app role reads it; rows are an
operator's decision, written as the owner. One row per dive; the reason is
required.

Revision ID: 0039
Revises: 0038
"""

from alembic import context, op

revision = "0039"
down_revision = "0038"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE automatic_results_exclusions (
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            dive_id uuid NOT NULL,
            reason text NOT NULL
                CONSTRAINT automatic_results_exclusions_reason_check
                CHECK (btrim(reason) <> ''),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, dive_id),
            FOREIGN KEY (tenant_id, dive_id) REFERENCES dives (tenant_id, id)
        )
        """)
    op.execute("ALTER TABLE automatic_results_exclusions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE automatic_results_exclusions FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON automatic_results_exclusions
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    op.execute(f"GRANT SELECT ON automatic_results_exclusions TO {_app_role()}")


def downgrade() -> None:
    op.execute("DROP TABLE automatic_results_exclusions")
