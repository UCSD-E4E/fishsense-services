"""Reviews and skills over HTTP (0063; the owner, 2026-10-21).

Every member may review, and reviews as themselves: the API takes the
reviewer from the signed-in caller, never from the body. Their skill decides
whether the review counts (``review_store``). A tenant's admins set skills.
A read-only system admin can't review until they unlock the tenant; a
non-member sees nothing.
"""

from __future__ import annotations

from depth_measure_seed import calibrated_dive, capture, forget_identities  # noqa: F401
from sqlalchemy import text
from test_automatic_review_store import _render
from test_observations_api import _bearer, client  # noqa: F401

ADMIN, MEMBER, OUTSIDER = "sub-admin", "sub-member", "sub-outsider"


async def _setup(seed_memberships, owner_engine):
    found = await seed_memberships({ADMIN: {"lab": "admin"}, MEMBER: {"lab": "member"},
                                    OUTSIDER: {"partner": "member"}})  # fmt: skip
    lab = found["lab"]
    mission_id, _ = await calibrated_dive(owner_engine, lab)
    c = await capture(owner_engine, lab, mission_id)
    await _render(owner_engine, lab, c)
    async with owner_engine.connect() as conn:
        render = (
            await conn.execute(
                text("SELECT id FROM capture_renders WHERE capture_id = :c"), {"c": c}
            )
        ).scalar_one()
        member = (
            await conn.execute(
                text("SELECT id FROM users WHERE sub = :s"), {"s": MEMBER}
            )
        ).scalar_one()
    return c, render, member


def _fix(render, **extra):
    return {"kind": "head_tail", "action": "fix", "head": {"x": 650, "y": 444},
            "tail": {"x": 890, "y": 580}, "render_id": str(render), **extra}  # fmt: skip


async def test_a_members_first_review_waits(client, seed_memberships, owner_engine):
    c, render, _ = await _setup(seed_memberships, owner_engine)

    response = await client.post(f"/tenants/lab/captures/{c}/reviews",
                                 json=_fix(render), headers=_bearer(MEMBER))  # fmt: skip

    assert response.status_code == 201, response.text
    assert (response.json()["level"], response.json()["counts"]) == ("learning", False)


async def test_an_admin_sets_a_skill_and_the_members_review_then_counts(
    client, seed_memberships, owner_engine
):
    c, render, member = await _setup(seed_memberships, owner_engine)

    set_ = await client.put(f"/tenants/lab/members/{member}/skills/head_tail",
                            json={"level": "expert"}, headers=_bearer(ADMIN))  # fmt: skip
    review = await client.post(f"/tenants/lab/captures/{c}/reviews",
                               json=_fix(render), headers=_bearer(MEMBER))  # fmt: skip

    assert set_.status_code == 204, set_.text
    assert (review.json()["level"], review.json()["counts"]) == ("expert", True)
    assert review.json()["label_id"]


async def test_only_an_admin_sets_skills(client, seed_memberships, owner_engine):
    _, _, member = await _setup(seed_memberships, owner_engine)

    response = await client.put(f"/tenants/lab/members/{member}/skills/laser",
                                json={"level": "expert"}, headers=_bearer(MEMBER))  # fmt: skip

    assert response.status_code == 403


async def test_a_member_reads_their_own_skills(client, seed_memberships, owner_engine):
    _, _, member = await _setup(seed_memberships, owner_engine)
    await client.put(f"/tenants/lab/members/{member}/skills/laser",
                     json={"level": "trusted"}, headers=_bearer(ADMIN))  # fmt: skip

    mine = (await client.get("/tenants/lab/me/skills", headers=_bearer(MEMBER))).json()

    assert mine == {"laser": "trusted", "head_tail": "learning", "species": "learning"}


async def test_a_frames_reviews_are_listed_with_whether_they_count(
    client, seed_memberships, owner_engine
):
    c, render, _ = await _setup(seed_memberships, owner_engine)
    await client.post(f"/tenants/lab/captures/{c}/reviews", json=_fix(render),
                      headers=_bearer(MEMBER))  # fmt: skip

    listed = (await client.get(f"/tenants/lab/captures/{c}/reviews",
                               headers=_bearer(ADMIN))).json()  # fmt: skip

    assert [(r["kind"], r["action"], r["counts"]) for r in listed] == [
        ("head_tail", "fix", False)
    ]


async def test_a_review_that_cant_be_recorded_says_why(
    client, seed_memberships, owner_engine
):
    c, _, _ = await _setup(seed_memberships, owner_engine)

    response = await client.post(f"/tenants/lab/captures/{c}/reviews",
                                 json={"kind": "head_tail", "action": "reject"},
                                 headers=_bearer(MEMBER))  # fmt: skip

    assert response.status_code == 422
    assert "reject" in response.json()["detail"]


async def test_someone_outside_the_tenant_cant_review(
    client, seed_memberships, owner_engine
):
    c, render, _ = await _setup(seed_memberships, owner_engine)

    response = await client.post(f"/tenants/lab/captures/{c}/reviews",
                                 json=_fix(render), headers=_bearer(OUTSIDER))  # fmt: skip

    assert response.status_code == 404


async def test_a_review_of_no_such_frame_is_not_found(
    client, seed_memberships, owner_engine
):
    await _setup(seed_memberships, owner_engine)

    response = await client.post(
        f"/tenants/lab/captures/{__import__('uuid').uuid4()}/reviews",
        json={
            "kind": "species",
            "action": "fix",
            "scientific_name": "Scarus coeruleus",
        },
        headers=_bearer(MEMBER),
    )

    assert response.status_code == 404, response.text


async def test_a_skill_for_someone_outside_the_tenant_is_not_found(
    client, seed_memberships, owner_engine
):
    await _setup(seed_memberships, owner_engine)
    async with owner_engine.connect() as conn:
        outsider = (
            await conn.execute(
                text("SELECT id FROM users WHERE sub = :s"), {"s": OUTSIDER}
            )
        ).scalar_one()

    response = await client.put(f"/tenants/lab/members/{outsider}/skills/laser",
                                json={"level": "expert"}, headers=_bearer(ADMIN))  # fmt: skip

    assert response.status_code == 404, response.text


async def test_a_listed_review_says_whether_it_is_the_callers(
    client, seed_memberships, owner_engine
):
    """The web offers no confirm of one's own review (it would be refused)."""
    c, render, _ = await _setup(seed_memberships, owner_engine)
    await client.post(f"/tenants/lab/captures/{c}/reviews",
                      json=_fix(render), headers=_bearer(MEMBER))  # fmt: skip

    mine = await client.get(
        f"/tenants/lab/captures/{c}/reviews", headers=_bearer(MEMBER)
    )
    theirs = await client.get(
        f"/tenants/lab/captures/{c}/reviews", headers=_bearer(ADMIN)
    )

    assert [r["mine"] for r in mine.json()] == [True]
    assert [r["mine"] for r in theirs.json()] == [False]
