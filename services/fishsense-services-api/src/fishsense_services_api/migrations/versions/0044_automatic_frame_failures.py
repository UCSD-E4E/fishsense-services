"""Automatic results: a frame's own failures, and two abstentions.

Review of #85 (2026-10-17). A frame the processor leaves out after its tries
was recorded as one of the *dive's* three strikes (0040), which conflated a
transient fault (a pod torn down mid-run, a node preempted) with a bad dive,
and cost a dive with one corrupt raw its calibration and every length.

* ``automatic_frame_failures``: one row per frame left out of a run. It is
  never a dive strike. At `AUTOMATIC_FRAME_MAX_FAILURES` the store appends a
  terminal ``frame_failed`` head/tail row for the frame (in the same
  transaction), so the frame drains and the dive goes on. Append-only for the
  app role, under forced RLS;
* two head/tail statuses: ``frame_failed`` (above) and ``mask_unscored`` -- a
  kept mask whose SAM score can't be named under the dot. That is a mismatch
  between the kernel and the score lookup, a bug to count, not the
  scientific answer ``laser_off_all_fish``.

Revision ID: 0044
Revises: 0043
"""

from alembic import context, op

revision = "0044"
down_revision = "0043"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
HEAD_TAILS = "automatic_head_tail_predictions"
STATUS = "automatic_head_tail_predictions_status_check"
_BEFORE = (
    "predicted", "no_laser_dot", "no_detections", "laser_off_all_fish",
    "headtail_failed", "decode_failed", "raw_unavailable", "slate_frame",
)  # fmt: skip
_AFTER = (*_BEFORE, "frame_failed", "mask_unscored")


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _status_check(statuses) -> None:
    listed = ", ".join(f"'{s}'" for s in statuses)
    op.execute(f"ALTER TABLE {HEAD_TAILS} DROP CONSTRAINT {STATUS}")
    op.execute(
        f"ALTER TABLE {HEAD_TAILS} ADD CONSTRAINT {STATUS} CHECK (status IN ({listed}))"
    )


def upgrade() -> None:
    _status_check(_AFTER)
    op.execute("""
        CREATE TABLE automatic_frame_failures (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            capture_id uuid NOT NULL,
            predictor_version integer NOT NULL,
            error text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id)
        )
        """)
    op.execute("""
        CREATE INDEX automatic_frame_failures_tenant_id_capture_id_idx
            ON automatic_frame_failures (tenant_id, capture_id)
        """)
    op.execute("ALTER TABLE automatic_frame_failures ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE automatic_frame_failures FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON automatic_frame_failures
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    op.execute(f"GRANT SELECT, INSERT ON automatic_frame_failures TO {_app_role()}")


def downgrade() -> None:
    op.execute("DROP TABLE automatic_frame_failures")
    _status_check(_BEFORE)
