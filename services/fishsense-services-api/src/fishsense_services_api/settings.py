"""Runtime configuration, from ``FISHSENSE_*`` environment variables.

Validated when the app is built, so a misconfigured deployment fails to start
rather than failing on its first request. Secrets are ``SecretStr``: they never
appear in a repr, a log line or a traceback.
"""

from typing import Annotated

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FISHSENSE_")

    #: DSN for the unprivileged app role -- never the schema owner (PLAN §9.10).
    database_url: SecretStr
    #: Authentik's issuer URL for the v2 application, e.g.
    #: ``https://auth.example/application/o/fishsense/``.
    oidc_issuer: str
    #: Client ids whose access tokens are accepted (web, mobile), comma-separated.
    oidc_audiences: Annotated[tuple[str, ...], NoDecode]
    #: Defaults to Authentik's standard location, ``{issuer}jwks/``.
    oidc_jwks_url: str | None = None
    jwks_cache_seconds: float = 300
    jwks_refetch_cooldown_seconds: float = 30

    @field_validator("oidc_audiences", mode="before")
    @classmethod
    def _split_audiences(cls, value: object) -> object:
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",")]
        audiences = tuple(a for a in value if a)
        if not audiences:
            raise ValueError("at least one audience is required")
        return audiences

    @model_validator(mode="after")
    def _default_jwks_url(self) -> "Settings":
        if self.oidc_jwks_url is None:
            self.oidc_jwks_url = f"{self.oidc_issuer.rstrip('/')}/jwks/"
        return self


class MigrationSettings(BaseSettings):
    """For the one-shot ``migrate`` command only -- never the running API.

    Kept separate from :class:`Settings` so the API process is never
    configured with, and so can never leak, the schema owner's credentials.
    """

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_")

    #: DSN for the schema *owner* role, which runs migrations.
    migration_database_url: SecretStr
    #: The role the API runs as; migrations grant it its runtime privileges.
    app_role: str = "fishsense_app"


class V1MigrationSettings(BaseSettings):
    """For the one-shot ``migrate-v1`` command (PLAN.md §6.4) only.

    The target must be written by a role that bypasses RLS (superuser or
    BYPASSRLS): FORCE ROW LEVEL SECURITY binds the table owner too.
    """

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_")

    #: v2, as a role that can write every tenant's rows (see above).
    migration_database_url: SecretStr
    #: v1's ``fishsense`` database, read-only use.
    v1_database_url: SecretStr
    app_role: str = "fishsense_app"
