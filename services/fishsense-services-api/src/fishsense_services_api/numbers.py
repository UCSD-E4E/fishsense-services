"""Moving each table's `number` sequence past the rows it holds.

Migrated rows are numbered by their `v1_id` (migration 0019). Once they are in,
each sequence must start above the largest, or a new row could take a number a
v1 row already has -- and with it, that dive's Label Studio projects. migrate-v1
calls this at the end; it is safe to call again.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.engine import Connection

__all__ = ["ADVANCE_SQL", "advance_numbers", "advance_numbers_sync"]

#: One statement: every table with a `number`, its sequence moved past its max.
ADVANCE_SQL = """
    SELECT setval(format('%s_number_seq', c.table_name)::regclass,
                  greatest(coalesce(m.max_number, 0), 1))
    FROM information_schema.columns c
    CROSS JOIN LATERAL (
        SELECT (xpath('/row/max/text()',
                query_to_xml(format('SELECT max(number) FROM %I', c.table_name),
                             false, true, '')))[1]::text::bigint AS max_number
    ) m
    WHERE c.table_schema = 'public' AND c.column_name = 'number'
      AND to_regclass(format('%s_number_seq', c.table_name)) IS NOT NULL
"""


async def advance_numbers(conn: AsyncConnection) -> None:
    await conn.execute(text(ADVANCE_SQL))


def advance_numbers_sync(conn: Connection) -> None:
    conn.execute(text(ADVANCE_SQL))
