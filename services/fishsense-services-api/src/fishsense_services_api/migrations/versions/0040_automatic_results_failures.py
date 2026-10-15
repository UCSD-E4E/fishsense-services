"""automatic_results_failures: a dive's failed automatic runs.

New in v2 (2026-10-15). The automatic-results backlog takes its oldest dive;
one that fails deterministically -- a raw gone from the NAS, an unusable
calibration, a frame no worker can finish -- was selected again every hour
and starved every dive behind it. Each failed run of a dive's step is a row
here; at `AUTOMATIC_RESULTS_MAX_FAILURES` the cohort leaves the dive for an
operator, who reads why here and deletes its rows (as the owner) to retry it.
A GPU that is not available is not a failure: those steps wait for a run
that has one.

Append-only for the app role (SELECT, INSERT), under forced RLS.

Revision ID: 0040
Revises: 0039
"""

from alembic import context, op

revision = "0040"
down_revision = "0039"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
STEPS = ("frames", "calibration", "lengths", "species")


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    steps = ", ".join(f"'{s}'" for s in STEPS)
    op.execute(f"""
        CREATE TABLE automatic_results_failures (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            dive_id uuid NOT NULL,
            step text NOT NULL
                CONSTRAINT automatic_results_failures_step_check
                CHECK (step IN ({steps})),
            error text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, dive_id) REFERENCES dives (tenant_id, id)
        )
        """)
    op.execute("""
        CREATE INDEX automatic_results_failures_tenant_id_dive_id_idx
            ON automatic_results_failures (tenant_id, dive_id)
        """)
    op.execute("ALTER TABLE automatic_results_failures ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE automatic_results_failures FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON automatic_results_failures
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    op.execute(f"GRANT SELECT, INSERT ON automatic_results_failures TO {_app_role()}")


def downgrade() -> None:
    op.execute("DROP TABLE automatic_results_failures")
