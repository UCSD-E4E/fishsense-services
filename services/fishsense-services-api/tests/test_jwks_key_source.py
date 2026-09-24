"""Signing keys come from the issuer's published JWKS (Authentik's jwks_uri).

Served here by a real local HTTP server so the fetch, the cache and a key
rotation are exercised end to end. An unreachable JWKS is an outage, not a bad
token: it raises ``KeysUnavailable`` so the API can answer 503 instead of 401.
"""

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from fishsense_services_api.auth import (
    InvalidToken,
    JwksKeySource,
    KeysUnavailable,
    TokenValidator,
)

ISSUER = "https://auth.example.test/application/o/fishsense/"
AUDIENCE = "fishsense-web"


def _rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict:
    jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


def _token(key: rsa.RSAPrivateKey, kid: str) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "s", "iat": now, "exp": now + 60}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


class _Jwks:
    """A JWKS endpoint whose published keys the test can rotate."""

    def __init__(self) -> None:
        self.keys: list[dict] = []
        self.raw_body: bytes | None = None  # served instead of the key set
        self.fetches = 0
        jwks = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                jwks.fetches += 1
                body = jwks.raw_body or json.dumps({"keys": jwks.keys}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/jwks/"

    def __enter__(self) -> "_Jwks":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def jwks() -> Iterator[_Jwks]:
    with _Jwks() as server:
        yield server


def _validator(url: str, **source_options) -> TokenValidator:
    return TokenValidator(
        issuer=ISSUER,
        audiences=(AUDIENCE,),
        keys=JwksKeySource(url, **source_options),
    )


def test_a_token_signed_by_a_published_key_validates(jwks):
    key = _rsa_key()
    jwks.keys = [_jwk(key, "k1")]

    assert _validator(jwks.url).validate(_token(key, "k1")).sub == "s"


def test_a_token_naming_an_unpublished_key_is_rejected(jwks):
    jwks.keys = [_jwk(_rsa_key(), "k1")]

    with pytest.raises(InvalidToken):
        _validator(jwks.url).validate(_token(_rsa_key(), "k2"))


def test_a_rotated_key_is_picked_up_without_a_restart(jwks):
    """Once the refetch cooldown has passed (zero here), a new kid refetches."""
    old, new = _rsa_key(), _rsa_key()
    jwks.keys = [_jwk(old, "old")]
    validator = _validator(jwks.url, refetch_cooldown_seconds=0)
    assert validator.validate(_token(old, "old")).sub == "s"

    jwks.keys = [_jwk(new, "new")]

    assert validator.validate(_token(new, "new")).sub == "s"


def test_a_revoked_key_stops_validating_once_the_cache_expires(jwks):
    """Rotating a leaked key out of Authentik must lock its forgeries out.

    Revocation takes effect within ``cache_seconds``, never "at next restart".
    """
    old, new = _rsa_key(), _rsa_key()
    jwks.keys = [_jwk(old, "old")]
    validator = _validator(jwks.url, cache_seconds=0.2)
    assert validator.validate(_token(old, "old")).sub == "s"

    jwks.keys = [_jwk(new, "new")]
    time.sleep(0.3)

    with pytest.raises(InvalidToken):
        validator.validate(_token(old, "old"))


def test_unknown_key_ids_cannot_make_us_hammer_the_issuer(jwks):
    """Tokens naming random kids trigger at most one refetch per cooldown."""
    jwks.keys = [_jwk(_rsa_key(), "k1")]
    validator = _validator(jwks.url, refetch_cooldown_seconds=60)

    for attempt in range(5):
        with pytest.raises(InvalidToken):
            validator.validate(_token(_rsa_key(), f"random-{attempt}"))

    # The initial load starts the cooldown, so none of the five refetches.
    assert jwks.fetches == 1


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<html>down for maintenance</html>", id="html-page"),
        pytest.param(b"[]", id="json-not-an-object"),
        pytest.param(b'{"keys": []}', id="no-keys"),
    ],
)
def test_a_jwks_that_is_not_a_usable_key_set_is_an_outage(jwks, body):
    """A 200 that isn't a key set (e.g. a proxy's maintenance page) is 503."""
    jwks.raw_body = body

    with pytest.raises(KeysUnavailable):
        _validator(jwks.url).validate(_token(_rsa_key(), "k1"))


def test_an_unreachable_jwks_is_an_outage_not_a_bad_token():
    with _Jwks() as gone:
        url = gone.url
    key = _rsa_key()

    with pytest.raises(KeysUnavailable):
        _validator(url).validate(_token(key, "k1"))
