"""The typed models (PLAN.md §3: SQLAlchemy 2.0 ``Mapped[]``) match the schema.

Migrations are hand-written -- RLS policies and grants can't be generated --
so nothing ties them to the models except this test. It asks Alembic's
autogenerate comparison what it would change to make the migrated database
match the models; the answer must be nothing.
"""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from fishsense_services_api.models import Base


def _differences(sync_conn) -> list:
    context = MigrationContext.configure(
        sync_conn, opts={"compare_type": True, "compare_server_default": True}
    )
    return compare_metadata(context, Base.metadata)


async def test_the_models_and_the_migrated_schema_agree(owner_engine):
    async with owner_engine.connect() as conn:
        differences = await conn.run_sync(_differences)

    assert differences == []
