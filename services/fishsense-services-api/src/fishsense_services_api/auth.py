"""In-app validation of Authentik (OIDC) access tokens.

The API trusts no upstream header: it verifies the token's signature against
the issuer's keys and checks issuer, audience and expiry itself. The caller's
identity is the stable ``sub`` claim -- never email, which admins can change.
"""

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import jwt

# Pinned so a token can never choose its own verification: no "none", and no
# symmetric algorithm that would treat our public key as a shared secret.
ALGORITHMS = ["RS256"]
REQUIRED_CLAIMS = ["iss", "aud", "exp", "iat", "sub"]


class InvalidToken(Exception):
    """The token is malformed, unsigned, mis-signed, expired or not for us."""


class KeysUnavailable(Exception):
    """The issuer's signing keys can't be fetched: an outage, not a bad token."""


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, identified by the IdP's stable subject."""

    sub: str


class KeySource(Protocol):
    def key_for(self, kid: str | None) -> Any:
        """Return the public key the issuer published under ``kid``."""


class StaticKeySource:
    """A fixed set of public keys by key id."""

    def __init__(self, keys: Mapping[str, Any]) -> None:
        self._keys = dict(keys)

    def key_for(self, kid: str | None) -> Any:
        try:
            return self._keys[kid]
        except KeyError:
            raise InvalidToken(f"unknown signing key {kid!r}") from None


class JwksKeySource:
    """The issuer's published keys (Authentik's ``jwks_uri``), cached.

    Only the key *set* is cached, for ``cache_seconds``; individual keys are
    never cached beyond it. So a key revoked in Authentik (e.g. after a leak)
    stops validating within ``cache_seconds``, not at the next restart.

    An unknown ``kid`` triggers one refetch before it is rejected, so a key
    rotation in Authentik is picked up without restarting the API. Forced
    refetches are at least ``refetch_cooldown_seconds`` apart, so tokens naming
    random kids can't turn the API into a flood against Authentik.
    """

    def __init__(
        self,
        url: str,
        *,
        cache_seconds: float = 300,
        refetch_cooldown_seconds: float = 30,
    ) -> None:
        # No `cache_keys=True`: it memoizes each key per kid with no expiry,
        # which would keep a revoked key trusted for the life of the process.
        self._client = jwt.PyJWKClient(url, lifespan=cache_seconds)
        self._cooldown = refetch_cooldown_seconds
        self._last_refetch: float | None = None
        self._lock = threading.Lock()  # FastAPI calls us from a threadpool

    def key_for(self, kid: str | None) -> Any:
        # Two failure domains, kept apart. Every attempt to obtain a usable key
        # set -- the cached load *and* a rotation refetch -- can only fail as an
        # outage (unreachable, a non-JSON 200 such as a proxy's maintenance
        # page, `{}`, no keys) -> 503. Only a kid that is missing from a usable
        # set means a bad token -> 401. PyJWT's get_signing_key blurs the two
        # during its refetch, so the refetch is done here.
        key = _match(self._signing_keys(refresh=False), kid)
        if key is None and self._may_refetch():
            key = _match(self._signing_keys(refresh=True), kid)
        if key is None:
            raise InvalidToken(f"unknown signing key {kid!r}")
        return key.key

    def _may_refetch(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if self._last_refetch is not None and now - self._last_refetch < (
                self._cooldown
            ):
                return False
            self._last_refetch = now
            return True

    def _signing_keys(self, *, refresh: bool) -> list[jwt.PyJWK]:
        try:
            keys = self._client.get_signing_keys(refresh=refresh)
        except (jwt.PyJWKClientError, jwt.PyJWKSetError, ValueError) as error:
            raise KeysUnavailable(str(error)) from error
        with self._lock:
            # The very first load is a fresh fetch too: it starts the cooldown,
            # so a burst of unknown kids right after startup refetches nothing.
            if self._last_refetch is None:
                self._last_refetch = time.monotonic()
        return keys


def _match(keys: list[jwt.PyJWK], kid: str | None) -> jwt.PyJWK | None:
    return next((key for key in keys if key.key_id == kid), None)


class TokenValidator:
    def __init__(self, *, issuer: str, audiences: tuple[str, ...], keys: KeySource):
        self._issuer = issuer
        self._audiences = list(audiences)
        self._keys = keys

    def validate(self, token: str) -> Principal:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            claims = jwt.decode(
                token,
                self._keys.key_for(kid),
                algorithms=ALGORITHMS,
                audience=self._audiences,
                issuer=self._issuer,
                options={"require": REQUIRED_CLAIMS},
            )
        except jwt.PyJWTError as error:
            raise InvalidToken(str(error)) from error
        return Principal(sub=claims["sub"])
