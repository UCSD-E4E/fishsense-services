"""Configuration comes from the environment and is validated at startup.

A deployment that is missing a setting must fail to start, loudly -- never
boot and then fail on the first request.
"""

import secrets

import pytest
from pydantic import ValidationError

from fishsense_services_api.settings import Settings

ISSUER = "https://auth.example.test/application/o/fishsense/"
# Generated per run, never written in the source: a password-shaped literal
# trips secret scanners even when it is fake.
DB_PASSWORD = secrets.token_hex(8)
REQUIRED = {
    "FISHSENSE_DATABASE_URL": f"postgresql+asyncpg://app:{DB_PASSWORD}@db/fishsense",
    "FISHSENSE_OIDC_ISSUER": ISSUER,
    "FISHSENSE_OIDC_AUDIENCES": "fishsense-web,fishsense-mobile",
}


@pytest.fixture
def env(monkeypatch):
    for name in list(REQUIRED) + ["FISHSENSE_OIDC_JWKS_URL"]:
        monkeypatch.delenv(name, raising=False)

    def set_env(**values: str) -> None:
        for name, value in values.items():
            monkeypatch.setenv(name, value)

    return set_env


def test_settings_load_from_the_environment(env):
    env(**REQUIRED)

    settings = Settings()

    assert settings.oidc_issuer == ISSUER
    assert settings.oidc_audiences == ("fishsense-web", "fishsense-mobile")
    assert (
        settings.database_url.get_secret_value() == REQUIRED["FISHSENSE_DATABASE_URL"]
    )


def test_the_jwks_url_defaults_to_authentiks_location_under_the_issuer(env):
    env(**REQUIRED)

    assert Settings().oidc_jwks_url == f"{ISSUER}jwks/"


def test_an_explicit_jwks_url_wins(env):
    env(**REQUIRED, FISHSENSE_OIDC_JWKS_URL="https://keys.example.test/jwks")

    assert Settings().oidc_jwks_url == "https://keys.example.test/jwks"


def test_missing_settings_fail_at_startup_naming_what_is_missing(env):
    with pytest.raises(ValidationError) as error:
        Settings()

    missing = {e["loc"][0] for e in error.value.errors()}
    assert missing == {"database_url", "oidc_issuer", "oidc_audiences"}


def test_an_empty_audience_list_is_refused(env):
    env(**{**REQUIRED, "FISHSENSE_OIDC_AUDIENCES": " , "})

    with pytest.raises(ValidationError):
        Settings()


def test_the_database_password_never_appears_in_the_settings_repr(env):
    env(**REQUIRED)

    assert DB_PASSWORD not in repr(Settings())
