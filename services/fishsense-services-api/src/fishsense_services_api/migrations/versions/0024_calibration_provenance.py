"""What a laser calibration was fitted against; an operator's refusal clear.

Two additions for the slate-calibration slice (fishsense-lite@77e8f8e5), both
additive and both append-only:

* ``laser_calibrations.slate_template_id`` / ``calibration_target_id``: the
  target a fit (or a refusal) used. v1's extrinsics row named no target, so a
  calibration could not be traced to its slate template or to the board --
  and the board *version* -- whose pitch set its scale (PLAN.md §4.3 asks for
  "the target plus its geometry version"). A checkerboard fit names the
  ``current_calibration_targets`` row it read, so a later pitch correction is
  visible as a different id. It also lets a refusal expire when the dive's
  target changes (v1's ``_clear_refusal`` on ``set_dive_slate`` /
  ``set_calibration_target``). A row names at most one target. Nullable:
  migrated rows and other producers name none.

* ``laser_calibration_refusal_clears``: an operator's "try this dive again"
  (v1's ``DELETE /dives/{id}/calibration-refused/``, which nulled three dive
  columns). v2's refusal is a row in the append-only ``laser_calibrations``,
  and the app role is never granted UPDATE there, so the clear is a row of its
  own naming the refusal it clears -- at most one per refusal, never edited or
  deleted (SELECT, INSERT only). Tenant-scoped under the canonical policy, and
  it can only name a refusal of its own tenant (composite key).

Revision ID: 0024
Revises: 0023
"""

from alembic import context, op

revision = "0024"
down_revision = "0023"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    app_role = _app_role()

    op.execute("""
        ALTER TABLE laser_calibrations
            ADD COLUMN slate_template_id uuid REFERENCES slate_templates (id),
            ADD COLUMN calibration_target_id uuid
                REFERENCES calibration_targets (id),
            ADD CONSTRAINT laser_calibrations_one_target_check
                CHECK (slate_template_id IS NULL OR calibration_target_id IS NULL)
        """)

    op.execute("""
        CREATE TABLE laser_calibration_refusal_clears (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            laser_calibration_id uuid NOT NULL,
            reason text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            CONSTRAINT laser_calibration_refusal_clears_refusal_key
                UNIQUE (tenant_id, laser_calibration_id),
            CONSTRAINT laser_calibration_refusal_clears_refusal_fkey
                FOREIGN KEY (tenant_id, laser_calibration_id)
                REFERENCES laser_calibrations (tenant_id, id)
        )
        """)
    op.execute("ALTER TABLE laser_calibration_refusal_clears ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE laser_calibration_refusal_clears FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON laser_calibration_refusal_clears
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    # Append-only: no UPDATE, no DELETE.
    op.execute(
        f"GRANT SELECT, INSERT ON laser_calibration_refusal_clears TO {app_role}"
    )


def downgrade() -> None:
    op.execute("DROP TABLE laser_calibration_refusal_clears")
    op.execute("""
        ALTER TABLE laser_calibrations
            DROP CONSTRAINT laser_calibrations_one_target_check,
            DROP COLUMN calibration_target_id,
            DROP COLUMN slate_template_id
        """)
