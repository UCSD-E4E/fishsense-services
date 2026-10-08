"""A partner joins their org's tenant on first sight, from the ``org`` claim.

krg-infra ``collaborator_invites.tf`` hands each partner org one reusable
Authentik invite; every account made through it carries ``org = <the org>``,
pinned server-side, and the token carries it as the ``org`` claim. A tenant
that names that org in ``tenants.org_claim`` takes such a caller in as a
``member`` -- automatically, with no operator in the loop.

What stays administrative: which tenant (if any) claims an org, and every role
above ``member``. The app role still can't write ``memberships`` itself; the
one door is ``join_claimed_tenant()``, which takes no arguments -- it reads the
caller and their org from the transaction's scope.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from fishsense_services_api.db import principal_transaction
from fishsense_services_api.memberships import (
    Membership,
    join_claimed_tenant,
    resolve_membership,
)
from fishsense_services_api.users import provision_user

PARTNER = "sub-partner"
ORG = "conservation-angler"


async def _claim(owner_engine, slug: str, org: str) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenants SET org_claim = :org WHERE slug = :slug"),
            {"org": org, "slug": slug},
        )


async def _first_sight(app_engine, sub: str, org: str | None) -> Membership | None:
    """What the API does with a caller: provision, join, resolve."""
    async with principal_transaction(app_engine, sub, org=org) as conn:
        await provision_user(conn, sub)
        await join_claimed_tenant(conn)
        return await resolve_membership(conn, sub, ORG)


@pytest.fixture
async def tenants(seed_memberships, owner_engine) -> dict:
    ids = await seed_memberships({"sub-lab-admin": {"lab": "admin", ORG: "admin"}})
    await _claim(owner_engine, ORG, ORG)
    return ids


async def test_a_caller_from_a_claimed_org_joins_its_tenant_as_a_member(
    tenants, app_engine
):
    assert await _first_sight(app_engine, PARTNER, ORG) == Membership(
        tenant_id=tenants[ORG], role="member"
    )


async def test_the_org_joins_only_its_own_tenant(tenants, app_engine):
    await _first_sight(app_engine, PARTNER, ORG)

    async with principal_transaction(app_engine, PARTNER) as conn:
        assert await resolve_membership(conn, PARTNER, "lab") is None
        assert (
            await conn.execute(text("SELECT slug FROM tenants"))
        ).scalars().all() == [ORG]


async def test_joining_again_changes_nothing(tenants, app_engine, owner_engine):
    for _ in range(3):
        await _first_sight(app_engine, PARTNER, ORG)

    async with owner_engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT m.role FROM memberships m JOIN users u ON u.id = m.user_id "
                "WHERE u.sub = :sub"
            ),
            {"sub": PARTNER},
        )
        assert rows.scalars().all() == ["member"]


async def test_joining_never_demotes_a_role_an_operator_granted(tenants, app_engine):
    """An operator may promote a partner to admin; their next login keeps it."""
    assert await _first_sight(app_engine, "sub-lab-admin", ORG) == Membership(
        tenant_id=tenants[ORG], role="admin"
    )


async def test_an_org_no_tenant_claims_joins_nothing(tenants, app_engine):
    assert await _first_sight(app_engine, PARTNER, "some-other-org") is None


async def test_no_org_joins_nothing(tenants, app_engine):
    assert await _first_sight(app_engine, PARTNER, None) is None


async def test_a_caller_with_no_user_row_joins_nothing(tenants, app_engine):
    async with principal_transaction(app_engine, PARTNER, org=ORG) as conn:
        await join_claimed_tenant(conn)
        assert await resolve_membership(conn, PARTNER, ORG) is None


async def test_two_tenants_cannot_claim_the_same_org(tenants, owner_engine):
    with pytest.raises(IntegrityError):
        await _claim(owner_engine, "lab", ORG)


async def test_the_app_role_cannot_claim_an_org_for_a_tenant(tenants, app_engine):
    """Which tenant an org lands in is an operator's decision, not a request's."""
    with pytest.raises(DBAPIError, match="permission denied"):
        async with principal_transaction(app_engine, "sub-lab-admin") as conn:
            await conn.execute(
                text("UPDATE tenants SET org_claim = 'mallory-org' WHERE slug = 'lab'")
            )


async def test_only_the_app_role_may_join(owner_engine):
    async with owner_engine.connect() as conn:
        may = await conn.execute(
            text(
                "SELECT r.rolname, has_function_privilege("
                "r.rolname, 'public.join_claimed_tenant()', 'EXECUTE') "
                "FROM pg_roles r WHERE r.rolname IN "
                "('fishsense_app', 'fishsense_research', 'fishsense_analytics')"
            )
        )
        assert dict(may.all()) == {
            "fishsense_app": True,
            "fishsense_research": False,
            "fishsense_analytics": False,
        }
