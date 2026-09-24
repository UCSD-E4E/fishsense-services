"""The app boots from its environment: real Postgres, real JWKS over HTTP.

This is the wiring test. Everything else builds the app by hand; this one goes
through the same factory uvicorn uses, so a mistake in how settings become an
engine and a key source shows up here.
"""

import time
from collections.abc import AsyncIterator

import httpx
import jwt
import pytest
from jwks_server import JwksServer, jwk, rsa_key

from fishsense_services_api.main import create_app_from_env

AUDIENCE = "fishsense-web"
SIGNING_KEY = rsa_key()


@pytest.fixture
def jwks():
    with JwksServer() as server:
        server.keys = [jwk(SIGNING_KEY, "k1")]
        yield server


@pytest.fixture
def issuer(jwks) -> str:
    return f"{jwks.base_url}/application/o/fishsense/"


@pytest.fixture
async def client(monkeypatch, app_url, owner_engine, issuer) -> AsyncIterator:
    monkeypatch.delenv("FISHSENSE_OIDC_JWKS_URL", raising=False)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)
    monkeypatch.setenv("FISHSENSE_OIDC_ISSUER", issuer)
    monkeypatch.setenv("FISHSENSE_OIDC_AUDIENCES", f"{AUDIENCE},fishsense-mobile")

    app = create_app_from_env()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


def _bearer(issuer: str, key=SIGNING_KEY) -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": issuer, "aud": AUDIENCE, "sub": "s", "iat": now, "exp": now + 60}
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})
    return {"Authorization": f"Bearer {token}"}


async def test_the_booted_app_authenticates_against_the_jwks(client, issuer):
    """404, not 401 or 500: the token checked out and the DB answered."""
    response = await client.get("/tenants/lab/devices", headers=_bearer(issuer))

    assert response.status_code == 404


async def test_the_booted_app_rejects_a_token_it_did_not_publish(client, issuer):
    response = await client.get(
        "/tenants/lab/devices", headers=_bearer(issuer, key=rsa_key())
    )

    assert response.status_code == 401


async def test_health_needs_no_token(client):
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
