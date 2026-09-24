"""Views run with the caller's rights (security_invoker), never the owner's.

By default a Postgres view runs as its owner, which bypasses RLS: a view over a
tenant table would show every tenant's rows. The two reference-data views from
0004 read only global tables, so nothing leaked, but every view follows one
rule, which the schema audit enforces.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"

VIEWS = ("current_calibration_targets", "current_fish_model_references")


def upgrade() -> None:
    for view in VIEWS:
        op.execute(f"ALTER VIEW {view} SET (security_invoker = true)")


def downgrade() -> None:
    for view in VIEWS:
        op.execute(f"ALTER VIEW {view} RESET (security_invoker)")
