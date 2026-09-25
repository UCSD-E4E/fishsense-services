"""Devices get a human name (v1's ``camera.name``: FSL-01 … FSL-11).

The research repos and the lab refer to rigs by these names ("FSL-07"); the v2
schema had only the serial, so the v1 migration would have dropped them.
Unique per tenant, like v1's global uniqueness.

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE devices
            ADD COLUMN name text,
            ADD CONSTRAINT devices_tenant_id_name_key UNIQUE (tenant_id, name)
        """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE devices
            DROP CONSTRAINT devices_tenant_id_name_key,
            DROP COLUMN name
        """)
