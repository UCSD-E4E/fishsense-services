"""Integer numbers: what people and tools call a row by.

v1 had integer ids everywhere: Label Studio project titles embed the dive's
(`#{dive_id}`), the web portal links by them, research queries join on them.
v2's ids are UUIDs and `v1_id` is set only on migrated rows, so every table
that has a `v1_id` also gets a `number`:

* a migrated row's number is its `v1_id` (so every existing Label Studio
  project is still found by title);
* a new row takes the table's next number, and `advance_numbers` (run at the
  end of migrate-v1) moves each sequence past v1's largest id, so a new row
  can never take a number a v1 row already has.

A BEFORE INSERT trigger fills it, so no writer has to know.

Revision ID: 0019
Revises: 0018
"""

from alembic import context, op
from sqlalchemy import text

revision = "0019"
down_revision = "0018"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _tables_with_v1_id() -> list[str]:
    rows = op.get_bind().execute(text("""
            SELECT c.table_name FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema = 'public' AND c.column_name = 'v1_id'
              AND t.table_type = 'BASE TABLE'
            ORDER BY c.table_name
            """))
    return list(rows.scalars())


def upgrade() -> None:
    app_role = _app_role()
    op.execute("""
        CREATE FUNCTION assign_number() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.number := coalesce(NEW.number, NEW.v1_id,
                                   nextval(TG_ARGV[0]::regclass));
            RETURN NEW;
        END
        $$
        """)
    for table in _tables_with_v1_id():
        seq = f"{table}_number_seq"
        op.execute(f"CREATE SEQUENCE {seq}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {seq} TO {app_role}")
        op.execute(f"ALTER TABLE {table} ADD COLUMN number bigint")
        # Rows already here (a rehearsal database): v1's ids first, then the
        # sequence moved past them, then numbers for the rest.
        op.execute(f"UPDATE {table} SET number = v1_id WHERE v1_id IS NOT NULL")
        op.execute(
            f"SELECT setval('{seq}', greatest((SELECT max(number) FROM {table}), 1))"
        )
        op.execute(f"UPDATE {table} SET number = nextval('{seq}') WHERE number IS NULL")
        op.execute(f"""
            ALTER TABLE {table}
                ALTER COLUMN number SET NOT NULL,
                ADD CONSTRAINT {table}_number_key UNIQUE (number)
            """)
        op.execute(f"""
            CREATE TRIGGER {table}_number BEFORE INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION assign_number('{seq}')
            """)


def downgrade() -> None:
    for table in _tables_with_v1_id():
        op.execute(f"DROP TRIGGER {table}_number ON {table}")
        op.execute(f"ALTER TABLE {table} DROP COLUMN number")
        op.execute(f"DROP SEQUENCE {table}_number_seq")
    op.execute("DROP FUNCTION assign_number()")
