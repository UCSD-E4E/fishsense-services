"""Phone captures imported from a dataset (the owner, 2026-10-21).

FishSense Mobile data that never went through the app's sync, such as CCFRP's
summer 2025 trips exported from the phones' databases, comes in by an
operator's import. Nobody synced those captures, so they have no
``uploaded_by``. ``captures.imported_from`` says where each came from instead
(the dataset file and row). A capture with a client id has exactly one of the
two: an uploader for a sync, a source for an import.

Revision ID: 0058
Revises: 0057
"""

from alembic import op

revision = "0058"
down_revision = "0057"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE captures
            ADD COLUMN imported_from text CHECK (imported_from <> ''),
            DROP CONSTRAINT captures_client_capture_uploader_check,
            ADD CONSTRAINT captures_client_capture_uploader_check CHECK (
                client_capture_uuid IS NULL
                OR (uploaded_by IS NULL) <> (imported_from IS NULL)
            )
        """)


def downgrade() -> None:
    # An import has no uploader to go back to: refuse rather than invent one.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM captures WHERE imported_from IS NOT NULL) THEN
                RAISE EXCEPTION 'imported captures exist; 0058 cannot be undone';
            END IF;
        END $$
        """)
    op.execute("""
        ALTER TABLE captures
            DROP CONSTRAINT captures_client_capture_uploader_check,
            DROP COLUMN imported_from,
            ADD CONSTRAINT captures_client_capture_uploader_check
                CHECK (client_capture_uuid IS NULL OR uploaded_by IS NOT NULL)
        """)
