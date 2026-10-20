"""Drop 0051's bucket-blind render index: renders settle per bucket.

0052 added ``capture_renders_rendered_once_per_bucket_idx`` beside 0051's
``capture_renders_rendered_once_idx`` rather than replacing it, because the
render backlog ran in production across that deploy and the orchestrator from
before it named the old index's columns in its ``ON CONFLICT`` (review of the
stack). That release's orchestrator names the per-bucket index; once it runs,
the old one only refuses a render into a tenant's second bucket -- the case
0052 exists for -- so it goes.

The downgrade recreates it, which fails if a capture has since been rendered
into two buckets at one version and camera.

Revision ID: 0054
Revises: 0053
"""

from alembic import op

revision = "0054"
down_revision = "0053"

OLD = "capture_renders_rendered_once_idx"


def upgrade() -> None:
    op.execute(f"DROP INDEX {OLD}")


def downgrade() -> None:
    op.execute(f"""
        CREATE UNIQUE INDEX {OLD}
            ON capture_renders (tenant_id, capture_id, render_version,
                                camera_calibration_id)
            WHERE status = 'rendered'
        """)
