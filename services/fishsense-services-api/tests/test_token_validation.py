"""The API validates Authentik access tokens itself (PLAN.md §3, §9.10).

Nothing upstream is trusted: the signature, issuer, audience and expiry are
checked in-app, and identity is the stable ``sub`` -- never email.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from fishsense_services_api.auth import (
    InvalidToken,
    Principal,
    StaticKeySource,
    TokenValidator,
)

ISSUER = "https://auth.example.test/application/o/fishsense/"
WEB_CLIENT = "fishsense-web"
MOBILE_CLIENT = "fishsense-mobile"
KID = "authentik-signing-key"


def _rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING_KEY = _rsa_key()


@pytest.fixture
def validator() -> TokenValidator:
    return TokenValidator(
        issuer=ISSUER,
        audiences=(WEB_CLIENT, MOBILE_CLIENT),
        keys=StaticKeySource({KID: SIGNING_KEY.public_key()}),
    )


def _token(
    *,
    key=SIGNING_KEY,
    algorithm: str = "RS256",
    kid: str = KID,
    **overrides,
) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": WEB_CLIENT,
        "sub": "hashed-user-id-123",
        "email": "someone@example.test",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


def test_a_valid_token_yields_its_stable_subject(validator):
    assert validator.validate(_token()) == Principal(sub="hashed-user-id-123")


def test_both_client_audiences_are_accepted(validator):
    assert validator.validate(_token(aud=MOBILE_CLIENT)).sub == "hashed-user-id-123"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"aud": "some-other-app"}, id="wrong-audience"),
        pytest.param({"iss": "https://evil.example.test/"}, id="wrong-issuer"),
        pytest.param({"exp": int(time.time()) - 1}, id="expired"),
        pytest.param({"sub": None}, id="no-subject"),
        pytest.param({"iat": None}, id="no-issued-at"),
    ],
)
def test_claims_that_do_not_check_out_are_rejected(validator, overrides):
    with pytest.raises(InvalidToken):
        validator.validate(_token(**overrides))


def test_a_token_signed_by_another_key_is_rejected(validator):
    with pytest.raises(InvalidToken):
        validator.validate(_token(key=_rsa_key()))


def test_a_token_naming_an_unknown_key_is_rejected(validator):
    with pytest.raises(InvalidToken):
        validator.validate(_token(kid="not-a-known-key"))


def test_an_unsigned_token_is_rejected(validator):
    with pytest.raises(InvalidToken):
        validator.validate(_token(key=None, algorithm="none"))


def test_a_symmetric_token_is_rejected(validator):
    """Only RS256 is accepted, so a shared-secret token can't pose as one."""
    with pytest.raises(InvalidToken):
        validator.validate(
            _token(key="a-guessed-shared-secret-at-least-32-bytes", algorithm="HS256")
        )


def test_garbage_is_rejected(validator):
    with pytest.raises(InvalidToken):
        validator.validate("not.a.token")
