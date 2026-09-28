"""The web portal's routes, over HTTP, under the same four steps as devices.

Ported from fishsense-lite@77e8f8e5: the routes the portal called on v1's
fishsense-api (`GET /api/v1/labels/{kind}/label-studio-project-ids`,
`GET /api/v1/dives/`, `PUT`/`DELETE /api/v1/dives/{id}/calibration-source/...`)
and the web's own gate on them (apps/fishsense-lite-web/lib/authz.ts,
app/portal/calibration/actions.test.ts).

v2 changes, each pinned here:

* every route is under `/tenants/{slug}/` and needs a member's bearer token
  (v1: a shared Basic-auth service account, and the API enforced nothing);
* **editing a calibration source needs the tenant's `admin` role**, enforced
  by the API itself -- v1's only gate was the web's Authentik group check,
  and "server actions are public endpoints";
* the caller can ask for their own membership, which is what the web's portal
  gate now asks instead of reading Authentik groups;
* dives are addressed by `number`, never a uuid.
"""

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

from fishsense_services_api.app import create_app
from fishsense_services_api.auth import StaticKeySource, TokenValidator

ISSUER = "https://auth.example.test/application/o/fishsense/"
AUDIENCE = "fishsense-web"
KID = "k1"
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ADMIN = "sub-admin"
MEMBER = "sub-member"
OUTSIDER = "sub-outsider"
T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _bearer(sub: str) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 60}
    token = jwt.encode(claims, SIGNING_KEY, algorithm="RS256", headers={"kid": KID})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client(app_engine) -> AsyncIterator[httpx.AsyncClient]:
    validator = TokenValidator(
        issuer=ISSUER,
        audiences=(AUDIENCE,),
        keys=StaticKeySource({KID: SIGNING_KEY.public_key()}),
    )
    app = create_app(engine=app_engine, validator=validator)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as c:
        yield c


@pytest.fixture
async def tenants(seed_memberships) -> dict:
    return await seed_memberships(
        {
            ADMIN: {"lab": "admin"},
            MEMBER: {"lab": "member"},
            OUTSIDER: {"partner": "admin"},
        }
    )


async def _dive(owner_engine, tenant, number, *, source=None):
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO dives (tenant_id, v1_id, name, source_path, dived_at, "
                    "calibration_source_dive_id) VALUES (:t, :n, :name, :p, :at, :s) "
                    "RETURNING id"
                ),
                {"t": tenant, "n": number, "name": f"dive {number}",
                 "p": f"/dives/{number}", "at": T0, "s": source},
            )  # fmt: skip
        ).scalar_one()


async def _source_of(owner_engine, dive_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT s.number FROM dives d LEFT JOIN dives s "
                    "ON s.id = d.calibration_source_dive_id WHERE d.id = :d"
                ),
                {"d": dive_id},
            )
        ).scalar_one()


async def _laser_label(owner_engine, tenant, project_id, *, completed=False):
    async with owner_engine.begin() as conn:
        dive = (
            await conn.execute(
                text(
                    "INSERT INTO dives (tenant_id, source_path, dived_at) "
                    "VALUES (:t, gen_random_uuid()::text, :at) RETURNING id"
                ),
                {"t": tenant, "at": T0},
            )
        ).scalar_one()
        capture = (
            await conn.execute(
                text(
                    "INSERT INTO captures (tenant_id, dive_id, source_path, "
                    "captured_at, checksum) VALUES (:t, :d, gen_random_uuid()::text, "
                    ":at, md5(random()::text)) RETURNING id"
                ),
                {"t": tenant, "d": dive, "at": T0},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO laser_labels (tenant_id, capture_id, source, "
                "ls_project_id, completed) VALUES (:t, :c, 'human', :p, :done)"
            ),
            {"t": tenant, "c": capture, "p": project_id, "done": completed},
        )
        return capture


# -- who the caller is in the tenant ---------------------------------------------


async def test_a_caller_reads_their_own_role(client, tenants):
    admin = await client.get("/tenants/lab/membership", headers=_bearer(ADMIN))
    member = await client.get("/tenants/lab/membership", headers=_bearer(MEMBER))

    assert admin.json() == {"role": "admin", "is_admin": True}
    assert member.json() == {"role": "member", "is_admin": False}


async def test_a_non_member_has_no_membership_to_read(client, tenants):
    response = await client.get("/tenants/lab/membership", headers=_bearer(OUTSIDER))

    assert response.status_code == 404


@pytest.mark.parametrize("role", ["Admin", "ADMIN", "admin ", "owner", "admins"])
async def test_only_the_exact_admin_role_is_admin(client, seed_memberships, role):
    """memberships.role is free text; only the exact string `admin` grants the
    edits PLAN.md §4.1 reserves to admins. v1 matched its Authentik group the
    same way -- exactly -- because a loose match makes the gate advisory."""
    await seed_memberships({"sub-lookalike": {"lab": role}})

    response = await client.get(
        "/tenants/lab/membership", headers=_bearer("sub-lookalike")
    )

    assert response.json()["is_admin"] is False


async def test_no_token_is_401(client):
    for method, path in [
        ("GET", "/tenants/lab/membership"),
        ("GET", "/tenants/lab/label-studio-projects?kind=laser"),
        ("GET", "/tenants/lab/dives"),
        ("PUT", "/tenants/lab/dives/2/calibration-source/1"),
        ("DELETE", "/tenants/lab/dives/2/calibration-source"),
        ("PUT", "/tenants/lab/dives/2/labels/laser/needs-reprocess"),
        ("DELETE", "/tenants/lab/dives/2/labels/laser/needs-reprocess"),
    ]:
        response = await client.request(method, path)
        assert response.status_code == 401, (method, path)


# -- live Label Studio projects ----------------------------------------------------


async def test_a_member_lists_live_projects(client, tenants, owner_engine):
    await _laser_label(owner_engine, tenants["lab"], 43, completed=False)
    await _laser_label(owner_engine, tenants["lab"], 42, completed=True)

    everything = await client.get(
        "/tenants/lab/label-studio-projects",
        params={"kind": "laser"},
        headers=_bearer(MEMBER),
    )
    incomplete = await client.get(
        "/tenants/lab/label-studio-projects",
        params={"kind": "laser", "incomplete": "true"},
        headers=_bearer(MEMBER),
    )

    assert everything.json() == [42, 43]
    assert incomplete.json() == [43]


async def test_gated_passes_through(client, tenants, owner_engine):
    """No prediction has been judged, so nothing is gated -- the flag reached
    the query (the predicate itself is pinned in test_portal_store)."""
    await _laser_label(owner_engine, tenants["lab"], 43)

    gated = await client.get(
        "/tenants/lab/label-studio-projects",
        params={"kind": "laser", "incomplete": "true", "gated": "true"},
        headers=_bearer(MEMBER),
    )
    ungated = await client.get(
        "/tenants/lab/label-studio-projects",
        params={"kind": "laser", "gated": "false"},
        headers=_bearer(MEMBER),
    )

    assert (gated.json(), ungated.json()) == ([], [43])


async def test_gated_on_a_kind_without_a_gate_is_a_422(client, tenants):
    response = await client.get(
        "/tenants/lab/label-studio-projects",
        params={"kind": "species", "gated": "true"},
        headers=_bearer(MEMBER),
    )

    assert response.status_code == 422


@pytest.mark.parametrize("kind", ["headtail", "dive-slate", "checkerboard_lattice", ""])
async def test_an_unknown_kind_is_a_422(client, tenants, kind):
    """v2 spells kinds as label_studio_projects.kind does (`head_tail`,
    `slate`); v1's URL segments are not accepted."""
    response = await client.get(
        "/tenants/lab/label-studio-projects",
        params={"kind": kind},
        headers=_bearer(MEMBER),
    )

    assert response.status_code == 422


async def test_a_non_member_cannot_list_a_tenants_projects(
    client, tenants, owner_engine
):
    await _laser_label(owner_engine, tenants["lab"], 43)

    response = await client.get(
        "/tenants/lab/label-studio-projects",
        params={"kind": "laser"},
        headers=_bearer(OUTSIDER),
    )

    assert response.status_code == 404


# -- dives ---------------------------------------------------------------------


async def test_a_member_lists_dives_by_number(client, tenants, owner_engine):
    source = await _dive(owner_engine, tenants["lab"], 1)
    await _dive(owner_engine, tenants["lab"], 2, source=source)
    await _dive(owner_engine, tenants["partner"], 3)

    response = await client.get("/tenants/lab/dives", headers=_bearer(MEMBER))

    assert response.json() == [
        {"number": 1, "name": "dive 1", "dived_at": "2025-01-01T00:00:00Z",
         "priority": "low", "slate_template_number": None,
         "calibration_target_number": None, "calibration_source_number": None},
        {"number": 2, "name": "dive 2", "dived_at": "2025-01-01T00:00:00Z",
         "priority": "low", "slate_template_number": None,
         "calibration_target_number": None, "calibration_source_number": 1},
    ]  # fmt: skip


# -- the calibration-source link ------------------------------------------------------


async def test_an_admin_links_a_dive_to_a_calibration_source(
    client, tenants, owner_engine
):
    await _dive(owner_engine, tenants["lab"], 1)
    two = await _dive(owner_engine, tenants["lab"], 2)

    response = await client.put(
        "/tenants/lab/dives/2/calibration-source/1", headers=_bearer(ADMIN)
    )

    assert response.status_code == 200
    assert response.json()["number"] == 2
    assert response.json()["calibration_source_number"] == 1
    assert await _source_of(owner_engine, two) == 1


async def test_a_member_who_is_not_an_admin_cannot_link(client, tenants, owner_engine):
    """The write changes measured fish lengths (a borrowed calibration runs
    -8..+2% against ~1% for the dive's own), so it is reserved to admins --
    and the API enforces it, not only the web."""
    await _dive(owner_engine, tenants["lab"], 1)
    two = await _dive(owner_engine, tenants["lab"], 2)

    response = await client.put(
        "/tenants/lab/dives/2/calibration-source/1", headers=_bearer(MEMBER)
    )

    assert response.status_code == 403
    assert await _source_of(owner_engine, two) is None


async def test_a_member_who_is_not_an_admin_cannot_clear(client, tenants, owner_engine):
    source = await _dive(owner_engine, tenants["lab"], 1)
    two = await _dive(owner_engine, tenants["lab"], 2, source=source)

    response = await client.delete(
        "/tenants/lab/dives/2/calibration-source", headers=_bearer(MEMBER)
    )

    assert response.status_code == 403
    assert await _source_of(owner_engine, two) == 1


async def test_the_role_is_checked_before_the_dive_is_looked_up(client, tenants):
    """A non-admin learns nothing about which dives exist: 403, not 404."""
    response = await client.put(
        "/tenants/lab/dives/999/calibration-source/998", headers=_bearer(MEMBER)
    )

    assert response.status_code == 403


async def test_an_admin_of_another_tenant_cannot_link(client, tenants, owner_engine):
    await _dive(owner_engine, tenants["lab"], 1)
    two = await _dive(owner_engine, tenants["lab"], 2)

    response = await client.put(
        "/tenants/lab/dives/2/calibration-source/1", headers=_bearer(OUTSIDER)
    )

    assert response.status_code == 404
    assert await _source_of(owner_engine, two) is None


async def test_a_self_link_is_a_400(client, tenants, owner_engine):
    await _dive(owner_engine, tenants["lab"], 1)

    response = await client.put(
        "/tenants/lab/dives/1/calibration-source/1", headers=_bearer(ADMIN)
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "A dive cannot be its own calibration source"


@pytest.mark.parametrize(
    "path, detail",
    [
        ("/tenants/lab/dives/999/calibration-source/1", "dive 999 not found"),
        (
            "/tenants/lab/dives/1/calibration-source/999",
            "calibration source dive 999 not found",
        ),
    ],
)
async def test_a_missing_dive_or_source_is_a_404(
    client, tenants, owner_engine, path, detail
):
    await _dive(owner_engine, tenants["lab"], 1)

    response = await client.put(path, headers=_bearer(ADMIN))

    assert response.status_code == 404
    assert response.json()["detail"] == detail


async def test_another_tenants_dive_is_not_a_source(client, tenants, owner_engine):
    await _dive(owner_engine, tenants["lab"], 1)
    await _dive(owner_engine, tenants["partner"], 2)

    response = await client.put(
        "/tenants/lab/dives/1/calibration-source/2", headers=_bearer(ADMIN)
    )

    assert response.status_code == 404


async def test_an_admin_clears_a_link_idempotently(client, tenants, owner_engine):
    source = await _dive(owner_engine, tenants["lab"], 1)
    two = await _dive(owner_engine, tenants["lab"], 2, source=source)

    first = await client.delete(
        "/tenants/lab/dives/2/calibration-source", headers=_bearer(ADMIN)
    )
    again = await client.delete(
        "/tenants/lab/dives/2/calibration-source", headers=_bearer(ADMIN)
    )

    assert (first.status_code, again.status_code) == (204, 204)
    assert await _source_of(owner_engine, two) is None


async def test_clearing_a_missing_dive_is_a_404(client, tenants):
    response = await client.delete(
        "/tenants/lab/dives/999/calibration-source", headers=_bearer(ADMIN)
    )

    assert response.status_code == 404


@pytest.mark.parametrize("bad", ["abc", "1.5", "-1", "0x1"])
async def test_a_dive_number_is_a_non_negative_integer(client, tenants, bad):
    """v1's web validated ids as non-negative integers (`safeId`) before they
    went into a URL; the API refuses anything else itself."""
    response = await client.put(
        f"/tenants/lab/dives/{bad}/calibration-source/1", headers=_bearer(ADMIN)
    )

    assert response.status_code == 422


# -- asking for a redraw: needs_reprocess -----------------------------------------
#
# fishsense-lite@77e8f8e5 fishsense-api controllers/label_reprocess_controller.py:
# `PUT`/`DELETE /api/v1/dives/{id}/labels/{laser,headtail,species,dive-slate}/
# needs-reprocess`. Raising puts the dive back in its kind's preprocessing
# cohort (the JPEGs are redrawn at the same keys; Label Studio's tasks are
# untouched). v1 served it to anyone holding the service account; here it
# needs the tenant's admin role. The per-kind rules (canonical frames, live
# rows, incomplete by default) are the stores', pinned in each store's tests.

LABEL_TABLES = {
    "laser": "laser_labels",
    "head_tail": "head_tail_labels",
    "species": "species_labels",
    "slate": "slate_labels",
}


async def _labelled_dive(owner_engine, tenant, number, table):
    """Dive `number` with two canonical frames labelled in `table`: one
    answered, one not."""
    dive = await _dive(owner_engine, tenant, number)
    async with owner_engine.begin() as conn:
        for done in (False, True):
            capture = (
                await conn.execute(
                    text(
                        "INSERT INTO captures (tenant_id, dive_id, source_path, "
                        "captured_at, checksum, is_canonical) VALUES (:t, :d, "
                        "gen_random_uuid()::text, :at, md5(random()::text), true) "
                        "RETURNING id"
                    ),
                    {"t": tenant, "d": dive, "at": T0},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    f"INSERT INTO {table} (tenant_id, capture_id, source, completed) "
                    "VALUES (:t, :c, 'human', :done)"
                ),
                {"t": tenant, "c": capture, "done": done},
            )
    return dive


async def _flagged(owner_engine, table, dive):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    f"SELECT count(*) FROM {table} l JOIN captures c "
                    "ON c.id = l.capture_id WHERE c.dive_id = :d AND l.needs_reprocess"
                ),
                {"d": dive},
            )
        ).scalar_one()


@pytest.mark.parametrize("kind", sorted(LABEL_TABLES))
async def test_an_admin_asks_for_a_dives_unanswered_frames_to_be_redrawn(
    client, tenants, owner_engine, kind
):
    table = LABEL_TABLES[kind]
    dive = await _labelled_dive(owner_engine, tenants["lab"], 7, table)

    response = await client.put(
        f"/tenants/lab/dives/7/labels/{kind}/needs-reprocess", headers=_bearer(ADMIN)
    )

    assert response.status_code == 200
    assert response.json() == {"flagged": 1}
    assert await _flagged(owner_engine, table, dive) == 1


@pytest.mark.parametrize("kind", sorted(LABEL_TABLES))
async def test_only_incomplete_false_redraws_answered_frames_too(
    client, tenants, owner_engine, kind
):
    table = LABEL_TABLES[kind]
    dive = await _labelled_dive(owner_engine, tenants["lab"], 7, table)

    response = await client.put(
        f"/tenants/lab/dives/7/labels/{kind}/needs-reprocess",
        params={"only_incomplete": "false"},
        headers=_bearer(ADMIN),
    )

    assert response.json() == {"flagged": 2}
    assert await _flagged(owner_engine, table, dive) == 2


@pytest.mark.parametrize("kind", sorted(LABEL_TABLES))
async def test_an_admin_withdraws_a_redraw(client, tenants, owner_engine, kind):
    table = LABEL_TABLES[kind]
    dive = await _labelled_dive(owner_engine, tenants["lab"], 7, table)
    await client.put(
        f"/tenants/lab/dives/7/labels/{kind}/needs-reprocess",
        params={"only_incomplete": "false"},
        headers=_bearer(ADMIN),
    )

    first = await client.delete(
        f"/tenants/lab/dives/7/labels/{kind}/needs-reprocess", headers=_bearer(ADMIN)
    )
    again = await client.delete(
        f"/tenants/lab/dives/7/labels/{kind}/needs-reprocess", headers=_bearer(ADMIN)
    )

    assert first.json() == {"cleared": 2}
    # Idempotent, never a 404 (v1's). v1 counted the rows the clear touched,
    # not the flags it lowered, so the count of a repeat is not pinned.
    assert again.status_code == 200
    assert await _flagged(owner_engine, table, dive) == 0


async def test_a_member_who_is_not_an_admin_cannot_ask_for_a_redraw(
    client, tenants, owner_engine
):
    dive = await _labelled_dive(owner_engine, tenants["lab"], 7, "laser_labels")

    raised = await client.put(
        "/tenants/lab/dives/7/labels/laser/needs-reprocess", headers=_bearer(MEMBER)
    )
    cleared = await client.delete(
        "/tenants/lab/dives/7/labels/laser/needs-reprocess", headers=_bearer(MEMBER)
    )

    assert (raised.status_code, cleared.status_code) == (403, 403)
    assert await _flagged(owner_engine, "laser_labels", dive) == 0


async def test_another_tenants_admin_cannot_ask_for_a_redraw(
    client, tenants, owner_engine
):
    dive = await _labelled_dive(owner_engine, tenants["lab"], 7, "laser_labels")

    response = await client.put(
        "/tenants/lab/dives/7/labels/laser/needs-reprocess", headers=_bearer(OUTSIDER)
    )

    assert response.status_code == 404
    assert await _flagged(owner_engine, "laser_labels", dive) == 0


async def test_a_redraw_of_a_missing_dive_is_a_404(client, tenants):
    for method in ("PUT", "DELETE"):
        response = await client.request(
            method,
            "/tenants/lab/dives/999/labels/laser/needs-reprocess",
            headers=_bearer(ADMIN),
        )
        assert response.status_code == 404, method
        assert response.json()["detail"] == "dive 999 not found"


@pytest.mark.parametrize("kind", ["headtail", "dive-slate", "checkerboard_lattice"])
async def test_a_redraw_of_an_unknown_kind_is_a_422(client, tenants, kind):
    response = await client.put(
        f"/tenants/lab/dives/7/labels/{kind}/needs-reprocess", headers=_bearer(ADMIN)
    )

    assert response.status_code == 422


# -- the calibration levers: a refusal and the calibration target -------------------
#
# fishsense-lite@77e8f8e5 fishsense-api controllers/dive_controller.py:
# `DELETE /api/v1/dives/{id}/calibration-refused/`,
# `PUT /api/v1/dives/{id}/calibration-target/{calibration_target_id}` and
# `DELETE /api/v1/dives/{id}/calibration-target/`. They are the levers a
# refusal's recorded remedy names (the processor's `REFUSAL_REMEDIES`). v2: an
# admin's, under the tenant, by dive number; the target by its `number` (v1's
# id for a migrated one); and a clear is an appended row, since a refusal is
# never edited.


async def _refused(owner_engine, tenant, dive):
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO laser_calibrations (tenant_id, dive_id, producer, "
                    "outcome, refusal_reason) VALUES (:t, :d, 'checkerboard', "
                    "'refused', 'implausible baseline') RETURNING id"
                ),
                {"t": tenant, "d": dive},
            )
        ).scalar_one()


async def _clears(owner_engine, refusal):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT reason FROM laser_calibration_refusal_clears "
                    "WHERE laser_calibration_id = :r"
                ),
                {"r": refusal},
            )
        ).all()


async def _board(owner_engine):
    """A calibration target (global reference data, so its name is unique)
    and its number."""
    async with owner_engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO calibration_targets (name, interior_rows, "
                    "interior_cols, pitch_x_m, pitch_y_m) VALUES "
                    "(gen_random_uuid()::text, 10, 14, 0.042, 0.042) "
                    "RETURNING id, number"
                )
            )
        ).one()


async def _links(owner_engine, dive):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT calibration_target_id, calibration_links_changed_at "
                    "FROM dives WHERE id = :d"
                ),
                {"d": dive},
            )
        ).one()


async def test_an_admin_clears_a_calibration_refusal(client, tenants, owner_engine):
    """v1's operator override: "try it again". The clear is appended, with
    the admin's reason, and a repeat is a no-op (v1: idempotent, 204)."""
    dive = await _dive(owner_engine, tenants["lab"], 7)
    refusal = await _refused(owner_engine, tenants["lab"], dive)

    first = await client.delete(
        "/tenants/lab/dives/7/calibration-refusal",
        params={"reason": "board re-measured"},
        headers=_bearer(ADMIN),
    )
    again = await client.delete(
        "/tenants/lab/dives/7/calibration-refusal", headers=_bearer(ADMIN)
    )

    assert (first.status_code, again.status_code) == (204, 204)
    assert [r.reason for r in await _clears(owner_engine, refusal)] == [
        "board re-measured"
    ]


async def test_an_admin_links_a_dive_to_a_calibration_target(
    client, tenants, owner_engine
):
    """The remedy's other half. Written as the species sync writes it, so the
    write expires a standing refusal (v1's `_clear_refusal`)."""
    dive = await _dive(owner_engine, tenants["lab"], 7)
    board = await _board(owner_engine)

    response = await client.put(
        f"/tenants/lab/dives/7/calibration-target/{board.number}",
        headers=_bearer(ADMIN),
    )

    assert response.status_code == 200
    assert response.json()["number"] == 7
    assert response.json()["calibration_target_number"] == board.number
    links = await _links(owner_engine, dive)
    assert links.calibration_target_id == board.id
    assert links.calibration_links_changed_at is not None


async def test_an_admin_clears_a_calibration_target_idempotently(
    client, tenants, owner_engine
):
    """What a refusal's remedy asks of a checkerboard dive that needs other
    frames: it leaves the board's cohort. v1's clear did not touch the
    refusal, and neither does this."""
    dive = await _dive(owner_engine, tenants["lab"], 7)
    board = await _board(owner_engine)
    await client.put(
        f"/tenants/lab/dives/7/calibration-target/{board.number}",
        headers=_bearer(ADMIN),
    )
    stamped = (await _links(owner_engine, dive)).calibration_links_changed_at

    first = await client.delete(
        "/tenants/lab/dives/7/calibration-target", headers=_bearer(ADMIN)
    )
    again = await client.delete(
        "/tenants/lab/dives/7/calibration-target", headers=_bearer(ADMIN)
    )

    assert (first.status_code, again.status_code) == (204, 204)
    links = await _links(owner_engine, dive)
    assert links.calibration_target_id is None
    assert links.calibration_links_changed_at == stamped


async def test_a_missing_calibration_target_is_a_404(client, tenants, owner_engine):
    dive = await _dive(owner_engine, tenants["lab"], 7)

    response = await client.put(
        "/tenants/lab/dives/7/calibration-target/999999", headers=_bearer(ADMIN)
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "calibration target 999999 not found"
    assert (await _links(owner_engine, dive)).calibration_target_id is None


_LEVERS = [
    ("DELETE", "/tenants/lab/dives/{n}/calibration-refusal"),
    ("PUT", "/tenants/lab/dives/{n}/calibration-target/{board}"),
    ("DELETE", "/tenants/lab/dives/{n}/calibration-target"),
]


@pytest.mark.parametrize("method, path", _LEVERS)
async def test_the_calibration_levers_are_an_admins(
    client, tenants, owner_engine, method, path
):
    """They change what is fitted, hence every measured length, so the API
    reserves them to the tenant's admins; another tenant's admin sees no
    dive, and a missing dive is a 404."""
    dive = await _dive(owner_engine, tenants["lab"], 7)
    refusal = await _refused(owner_engine, tenants["lab"], dive)
    board = await _board(owner_engine)

    def url(n):
        return path.format(n=n, board=board.number)

    member = await client.request(method, url(7), headers=_bearer(MEMBER))
    outsider = await client.request(method, url(7), headers=_bearer(OUTSIDER))
    missing = await client.request(method, url(999), headers=_bearer(ADMIN))

    assert (member.status_code, outsider.status_code) == (403, 404)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "dive 999 not found"
    assert await _clears(owner_engine, refusal) == []
    assert (await _links(owner_engine, dive)).calibration_target_id is None
