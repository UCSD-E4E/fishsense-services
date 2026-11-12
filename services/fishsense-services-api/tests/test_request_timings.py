"""How long each API request and each web page takes (the owner, 2026-11-12).

The owner: "things are so slow, can we add time to first byte measurements for
every page? ... it would be nice to log this information for trying to improve
the performance of the website". Docker's logs reach back only to a
container's last restart, which every deploy is, so the timings are kept in
Postgres (migration 0091), where p50/p95 per route can be asked for.

* ``api_timings``: one row per API request -- its route template, status, the
  time to its response's headers, and the time spent in the database.
* ``page_timings``: what the web's visitors' browsers measured -- time to first
  byte and the Core Web Vitals on a page load, and how long a click took to
  show the next page.

Both are function-only, as ``api_tokens`` is (0066): forced RLS with no
policy, no grant to the app role, and ``SECURITY DEFINER`` functions that
insert and prune rows older than 90 days.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from depth_measure_seed import exec_
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from fishsense_services_api.app import create_app
from fishsense_services_api.auth import StaticKeySource, TokenValidator
from fishsense_services_api.timings import TimingRecorder

ISSUER, AUDIENCE, KID = "https://idp.test", "fishsense-api", "test-key"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
MEMBER = "member-sub"


def _signed_in(sub: str = MEMBER) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 60}
    token = jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": KID})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
async def no_timings(owner_engine) -> AsyncIterator[None]:
    yield
    await exec_(owner_engine, "TRUNCATE api_timings, page_timings")


@pytest.fixture
async def app(app_engine):
    validator = TokenValidator(
        issuer=ISSUER,
        audiences=(AUDIENCE,),
        keys=StaticKeySource({KID: KEY.public_key()}),
    )
    return create_app(engine=app_engine, validator=validator)


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as c:
        yield c


async def _rows(owner_engine, table: str) -> list[dict]:
    async with owner_engine.connect() as conn:
        result = await conn.execute(text(f"SELECT * FROM {table} ORDER BY id"))
        return [dict(r._mapping) for r in result]


# ── the tables: only their functions reach them ─────────────────────────────


@pytest.mark.parametrize("table", ["api_timings", "page_timings"])
async def test_the_app_role_can_neither_read_nor_write_a_timings_table(
    app_engine, table
):
    for sql in (f"SELECT * FROM {table}", f"DELETE FROM {table}"):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with app_engine.begin() as conn:
                await conn.execute(text(sql))


async def test_record_api_timings_stores_each_request(app_engine, owner_engine):
    rows = [
        {
            "method": "GET",
            "route": "/tenants/{slug}/devices",
            "status": 200,
            "total_ms": 12.5,
            "db_ms": 4.25,
            "db_queries": 3,
        },
        {
            "method": "POST",
            "route": "/me/tokens",
            "status": 201,
            "total_ms": 30.0,
            "db_ms": 10.0,
            "db_queries": 5,
        },
    ]
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT record_api_timings(CAST(:rows AS jsonb))"),
            {"rows": json.dumps(rows)},
        )

    stored = await _rows(owner_engine, "api_timings")
    assert [
        {k: r[k] for k in ("method", "route", "status", "db_queries")} for r in stored
    ] == [
        {
            "method": "GET",
            "route": "/tenants/{slug}/devices",
            "status": 200,
            "db_queries": 3,
        },
        {"method": "POST", "route": "/me/tokens", "status": 201, "db_queries": 5},
    ]
    assert stored[0]["total_ms"] == pytest.approx(12.5)
    assert stored[0]["db_ms"] == pytest.approx(4.25)
    assert stored[0]["recorded_at"] is not None


async def test_record_page_timings_stores_each_measurement(app_engine, owner_engine):
    rows = [
        {"route": "/[tenant]/fish", "metric": "TTFB", "value": 812.0, "kind": "load",
         "release": "0.84.0"},
        {"route": "/[tenant]/data/missions/[n]", "metric": "NAV", "value": 1400.0,
         "kind": "navigation", "release": "0.84.0"},
    ]  # fmt: skip
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT record_page_timings(CAST(:rows AS jsonb))"),
            {"rows": json.dumps(rows)},
        )

    stored = await _rows(owner_engine, "page_timings")
    assert [(r["route"], r["metric"], r["kind"], r["value"]) for r in stored] == [
        ("/[tenant]/fish", "TTFB", "load", 812.0),
        ("/[tenant]/data/missions/[n]", "NAV", "navigation", 1400.0),
    ]
    assert stored[0]["release"] == "0.84.0"


async def test_a_page_timing_with_an_unknown_metric_is_refused(app_engine):
    row = {"route": "/", "metric": "BOGUS", "value": 1.0, "kind": "load"}
    with pytest.raises(DBAPIError, match="page_timings_metric_check"):
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT record_page_timings(CAST(:rows AS jsonb))"),
                {"rows": json.dumps([row])},
            )


async def test_recording_prunes_timings_older_than_90_days(app_engine, owner_engine):
    await exec_(
        owner_engine,
        """
        INSERT INTO api_timings (recorded_at, method, route, status, total_ms,
                                 db_ms, db_queries)
        VALUES (now() - interval '91 days', 'GET', '/old', 200, 1, 0, 0),
               (now() - interval '89 days', 'GET', '/kept', 200, 1, 0, 0)
        """,
    )
    await exec_(
        owner_engine,
        """
        INSERT INTO page_timings (recorded_at, route, metric, value, kind)
        VALUES (now() - interval '91 days', '/old', 'TTFB', 1, 'load'),
               (now() - interval '89 days', '/kept', 'TTFB', 1, 'load')
        """,
    )
    api = {"method": "GET", "route": "/new", "status": 200, "total_ms": 1,
           "db_ms": 0, "db_queries": 0}  # fmt: skip
    page = {"route": "/new", "metric": "TTFB", "value": 1, "kind": "load"}
    async with app_engine.begin() as conn:
        await conn.execute(
            text("SELECT record_api_timings(CAST(:rows AS jsonb))"),
            {"rows": json.dumps([api])},
        )
        await conn.execute(
            text("SELECT record_page_timings(CAST(:rows AS jsonb))"),
            {"rows": json.dumps([page])},
        )

    assert [r["route"] for r in await _rows(owner_engine, "api_timings")] == [
        "/kept",
        "/new",
    ]
    assert [r["route"] for r in await _rows(owner_engine, "page_timings")] == [
        "/kept",
        "/new",
    ]


# ── the API times every request ─────────────────────────────────────────────


async def test_every_request_is_timed_under_its_route_template(
    client, app, seed_memberships, owner_engine
):
    await seed_memberships({MEMBER: {"lab": "member"}})

    response = await client.get("/tenants/lab/devices", headers=_signed_in())
    assert response.status_code == 200
    await app.state.timings.flush()

    [row] = await _rows(owner_engine, "api_timings")
    assert (row["method"], row["route"], row["status"]) == (
        "GET",
        "/tenants/{slug}/devices",
        200,
    )
    # Membership, then the tenant's transaction: several statements, all timed.
    assert row["db_queries"] >= 3
    assert 0 < row["db_ms"] <= row["total_ms"]


async def test_a_refused_request_is_timed_with_its_status(client, app, owner_engine):
    response = await client.get("/tenants/lab/devices")
    assert response.status_code == 401
    await app.state.timings.flush()

    [row] = await _rows(owner_engine, "api_timings")
    assert (row["route"], row["status"], row["db_queries"]) == (
        "/tenants/{slug}/devices",
        401,
        0,
    )


async def test_the_response_says_where_its_time_went(client, seed_memberships):
    await seed_memberships({MEMBER: {"lab": "member"}})

    response = await client.get("/tenants/lab/devices", headers=_signed_in())

    timing = response.headers["server-timing"]
    parts = {p.split(";")[0].strip(): p for p in timing.split(",")}
    assert set(parts) == {"app", "db"}
    assert "dur=" in parts["app"] and "dur=" in parts["db"]
    assert "queries" in parts["db"]


async def test_health_checks_are_not_recorded(client, app, owner_engine):
    assert (await client.get("/healthz")).status_code == 200
    await app.state.timings.flush()

    assert await _rows(owner_engine, "api_timings") == []


async def test_a_failure_to_store_timings_never_fails_a_request(owner_url, caplog):
    """A broken timings store loses its rows and says so; requests go on."""
    broken = create_async_engine(owner_url.replace("/test", "/no_such_database"))
    recorder = TimingRecorder(broken)
    recorder.record(
        method="GET", route="/x", status=200, total_ms=1.0, db_ms=0.0, db_queries=0
    )

    with caplog.at_level(logging.WARNING):
        await recorder.flush()

    assert "timings" in caplog.text
    await broken.dispose()


async def test_the_recorder_flushes_itself_once_enough_rows_wait(
    app_engine, owner_engine
):
    recorder = TimingRecorder(app_engine, flush_rows=2, flush_seconds=3600)
    for _ in range(2):
        recorder.record(
            method="GET", route="/x", status=200, total_ms=1.0, db_ms=0.0, db_queries=0
        )
    await recorder.wait()

    assert len(await _rows(owner_engine, "api_timings")) == 2


# ── the web posts what its visitors' browsers measured ──────────────────────


async def test_page_timings_from_the_web_are_stored(client, owner_engine):
    body = {
        "release": "0.84.0",
        "timings": [
            {
                "route": "/[tenant]/fish",
                "metric": "TTFB",
                "value": 640.5,
                "kind": "load",
            },
            {"route": "/[tenant]/fish", "metric": "LCP", "value": 1900, "kind": "load"},
        ],
    }

    response = await client.post(
        "/telemetry/page-timings", headers=_signed_in(), json=body
    )

    assert response.status_code == 204, response.text
    stored = await _rows(owner_engine, "page_timings")
    assert [(r["metric"], r["value"], r["release"]) for r in stored] == [
        ("TTFB", 640.5, "0.84.0"),
        ("LCP", 1900.0, "0.84.0"),
    ]


async def test_page_timings_need_a_caller(client):
    body = {"timings": [{"route": "/", "metric": "TTFB", "value": 1, "kind": "load"}]}

    response = await client.post("/telemetry/page-timings", json=body)

    assert response.status_code == 401


@pytest.mark.parametrize(
    "timing",
    [
        {"route": "/", "metric": "BOGUS", "value": 1, "kind": "load"},
        {"route": "/", "metric": "TTFB", "value": -1, "kind": "load"},
        {"route": "/", "metric": "TTFB", "value": 1e9, "kind": "load"},
        {"route": "no-slash", "metric": "TTFB", "value": 1, "kind": "load"},
        {"route": "/" + "x" * 300, "metric": "TTFB", "value": 1, "kind": "load"},
        {"route": "/", "metric": "TTFB", "value": 1, "kind": "sideways"},
    ],
)
async def test_a_page_timing_out_of_bounds_is_refused(client, timing):
    response = await client.post(
        "/telemetry/page-timings", headers=_signed_in(), json={"timings": [timing]}
    )

    assert response.status_code == 422


async def test_a_beacon_carries_at_most_50_timings(client):
    timing = {"route": "/", "metric": "TTFB", "value": 1, "kind": "load"}

    response = await client.post(
        "/telemetry/page-timings",
        headers=_signed_in(),
        json={"timings": [timing] * 51},
    )

    assert response.status_code == 422
