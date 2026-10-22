"""The app boots from its environment: real Postgres, real JWKS over HTTP.

This is the wiring test. Everything else builds the app by hand; this one goes
through the same factory uvicorn uses, so a mistake in how settings become an
engine and a key source shows up here.
"""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import jwt
import pytest
from jwks_server import JwksServer, jwk, rsa_key

from fishsense_services_api.main import create_app_from_env
from fishsense_services_api.place_names import PhotonGeocoder

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
def client_factory(monkeypatch, app_url, owner_engine, issuer):
    """Boots the app from the environment as it stands when called."""
    for name in (
        "FISHSENSE_OIDC_JWKS_URL",
        "FISHSENSE_OIDC_MOBILE_ISSUER",
        "FISHSENSE_OIDC_MOBILE_AUDIENCES",
        "FISHSENSE_OIDC_MOBILE_JWKS_URL",
        "FISHSENSE_TENANT_STORES_FILE",
        "FISHSENSE_OBJECT_STORE_ENDPOINT_URL",
        "FISHSENSE_OBJECT_STORE_PUBLIC_URL",
        "FISHSENSE_OBJECT_STORE_REGION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)
    monkeypatch.setenv("FISHSENSE_OIDC_ISSUER", issuer)
    monkeypatch.setenv("FISHSENSE_OIDC_AUDIENCES", AUDIENCE)

    @asynccontextmanager
    async def _client() -> AsyncIterator[httpx.AsyncClient]:
        app = create_app_from_env()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                yield c

    return _client


@pytest.fixture
async def client(client_factory) -> AsyncIterator:
    async with client_factory() as c:
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


async def test_the_booted_app_accepts_the_mobile_issuers_tokens(
    monkeypatch, client_factory, jwks
):
    mobile_issuer = f"{jwks.base_url}/application/o/fishsense-mobile/"
    monkeypatch.setenv("FISHSENSE_OIDC_MOBILE_ISSUER", mobile_issuer)
    monkeypatch.setenv("FISHSENSE_OIDC_MOBILE_AUDIENCES", "fishsense-mobile")

    async with client_factory() as client:
        now = int(time.time())
        claims = {"iss": mobile_issuer, "aud": "fishsense-mobile", "sub": "s",
                  "iat": now, "exp": now + 60}  # fmt: skip
        token = jwt.encode(
            claims, SIGNING_KEY, algorithm="RS256", headers={"kid": "k1"}
        )
        response = await client.get(
            "/tenants/lab/devices", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 404


async def test_the_booted_app_signs_uploads_into_the_tenants_bucket(
    monkeypatch, client_factory, issuer, seed_memberships, tmp_path
):
    """The tenant stores come from the rendered file, and uploads are signed
    for the public endpoint the phone will call."""
    await seed_memberships({"s": {"lab": "member"}})
    stores = tmp_path / "tenant-stores.json"
    stores.write_text(
        '{"lab": {"bucket": "fishsense-tenant-lab", "access_key_id": "GK1",'
        ' "secret_access_key": "s"}}'
    )
    monkeypatch.setenv("FISHSENSE_TENANT_STORES_FILE", str(stores))
    monkeypatch.setenv(
        "FISHSENSE_OBJECT_STORE_ENDPOINT_URL", "http://garage.internal:3900"
    )
    monkeypatch.setenv("FISHSENSE_OBJECT_STORE_PUBLIC_URL", "https://s3.example.test")

    async with client_factory() as client:
        response = await client.post(
            "/tenants/lab/mobile/captures/00000000-0000-4000-8000-000000000001/uploads",
            json={"files": [{"role": "rgb_jpeg", "sha256": "0" * 64, "size": 1}]},
            headers=_bearer(issuer),
        )

    assert response.status_code == 200, response.text
    assert response.json()["uploads"][0]["url"].startswith(
        "https://s3.example.test/fishsense-tenant-lab/mobile/captures/"
    )


def _captured_create_app(monkeypatch) -> dict:
    import fishsense_services_api.main as main

    captured: dict = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return main.FastAPI()

    monkeypatch.setattr(main, "create_app", fake)
    return captured


def test_the_app_names_places_on_the_configured_geocoder(monkeypatch, app_url, issuer):
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)
    monkeypatch.setenv("FISHSENSE_OIDC_ISSUER", issuer)
    monkeypatch.setenv("FISHSENSE_OIDC_AUDIENCES", AUDIENCE)
    monkeypatch.setenv("FISHSENSE_GEOCODER_URL", "http://photon.internal:2322")
    captured = _captured_create_app(monkeypatch)

    create_app_from_env()

    assert isinstance(captured["geocoder"], PhotonGeocoder)


def test_without_a_geocoder_url_no_places_are_named(monkeypatch, app_url, issuer):
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", app_url)
    monkeypatch.setenv("FISHSENSE_OIDC_ISSUER", issuer)
    monkeypatch.setenv("FISHSENSE_OIDC_AUDIENCES", AUDIENCE)
    monkeypatch.delenv("FISHSENSE_GEOCODER_URL", raising=False)
    captured = _captured_create_app(monkeypatch)

    create_app_from_env()

    assert captured["geocoder"] is None
