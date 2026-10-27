"""A Label Studio project may be retired (the owner, 2026-10-26).

``assign-slate-templates`` gave mission 508, a pool test that must never have
a dive slate, a slate template from an automatic vote, and stage 9 made it a
slate project (300745) of 47 placeholder slate labels. Mission 439 got one
too (300747, 9 placeholders), which the owner does not want labeled now.
Nothing could take a project back: the sync lists every project holding a
live label, so a project deleted in Label Studio would fail every sync run.

* ``label_studio_projects.retired_at`` and ``retired_reason``: when and why
  the project was retired, both or neither. A retired project is not synced
  (``label_sync_store``, ``slate_store``), not found as its mission's project
  and not recorded again (``label_project_store``). The rows stay: retired,
  never deleted (``slate_project_retirement``).

The downgrade drops the columns; it fails while a project is retired, rather
than forget why.

Revision ID: 0079
Revises: 0078
"""

from alembic import op
from sqlalchemy import text

revision = "0079"
down_revision = "0078"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE label_studio_projects
            ADD COLUMN retired_at timestamptz,
            ADD COLUMN retired_reason text,
            ADD CONSTRAINT label_studio_projects_retired_check
                CHECK ((retired_at IS NULL) = (retired_reason IS NULL))
        """)


def downgrade() -> None:
    retired = (
        op.get_bind()
        .execute(
            text(
                "SELECT count(*) FROM label_studio_projects "
                "WHERE retired_at IS NOT NULL"
            )
        )
        .scalar_one()
    )
    if retired:
        raise RuntimeError(
            f"{retired} retired Label Studio projects exist: 0078 cannot hold them"
        )
    op.execute("""
        ALTER TABLE label_studio_projects
            DROP COLUMN retired_at,
            DROP COLUMN retired_reason
        """)
