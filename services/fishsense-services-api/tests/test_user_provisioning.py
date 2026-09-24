"""A caller's user row is created on their first valid token (PLAN.md §9.10).

Authentik stays the only identity provider; v2 keeps a local record *of* each
identity, keyed on the stable ``sub``. The app role may create only the
caller's own row -- never one for somebody else.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from fishsense_services_api.db import principal_transaction
from fishsense_services_api.users import provision_user


async def _subs(owner_engine) -> list[str]:
    async with owner_engine.connect() as conn:
        return sorted((await conn.execute(text("SELECT sub FROM users"))).scalars())


async def test_first_login_creates_the_user(owner_engine, app_engine):
    async with principal_transaction(app_engine, "sub-alice") as conn:
        await provision_user(conn, "sub-alice")

    assert await _subs(owner_engine) == ["sub-alice"]


async def test_later_logins_reuse_the_same_user(owner_engine, app_engine):
    ids = []
    for _ in range(2):
        async with principal_transaction(app_engine, "sub-alice") as conn:
            ids.append(await provision_user(conn, "sub-alice"))

    assert ids[0] == ids[1]
    assert await _subs(owner_engine) == ["sub-alice"]


async def test_a_caller_cannot_create_someone_elses_user(owner_engine, app_engine):
    with pytest.raises(DBAPIError, match="row-level security"):
        async with principal_transaction(app_engine, "sub-alice") as conn:
            await provision_user(conn, "sub-mallory")

    assert await _subs(owner_engine) == []


async def test_the_insert_policy_itself_refuses_someone_elses_user(
    owner_engine, app_engine
):
    """Pins the INSERT policy alone.

    ``provision_user`` uses RETURNING, which the SELECT policy also guards, so
    the test above would still pass if the INSERT policy were loosened. A bare
    INSERT is checked by the INSERT policy and nothing else.
    """
    with pytest.raises(DBAPIError, match="row-level security"):
        async with principal_transaction(app_engine, "sub-alice") as conn:
            await conn.execute(text("INSERT INTO users (sub) VALUES ('sub-mallory')"))

    assert await _subs(owner_engine) == []
