"""Runtime configuration, from ``FISHSENSE_*`` environment variables.

Validated when the app is built, so a misconfigured deployment fails to start
rather than failing on its first request. Secrets are ``SecretStr``: they never
appear in a repr, a log line or a traceback.
"""

import logging
from pathlib import Path
from typing import Annotated

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)

#: What consul-template renders for a key that is missing from its secret.
_NO_VALUE = "<no value>"


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
    #: The mobile app's own Authentik application (a public client with
    #: PKCE): Authentik's issuer is per application, so it is a second issuer
    #: with its own audiences and keys. Unset, the API accepts web tokens only.
    oidc_mobile_issuer: str | None = None
    oidc_mobile_audiences: Annotated[tuple[str, ...], NoDecode] = ()
    #: Defaults to ``{mobile issuer}jwks/``.
    oidc_mobile_jwks_url: str | None = None
    jwks_cache_seconds: float = 300
    jwks_refetch_cooldown_seconds: float = 30
    #: Each tenant's own bucket and key (tenant_stores.py), rendered as JSON
    #: ``{slug: {bucket, access_key_id, secret_access_key}}``. Unset, no
    #: tenant has storage, so none can sync mobile captures.
    tenant_stores_file: Path | None = None
    #: Garage as the API reaches it (HEADs at commit).
    object_store_endpoint_url: str | None = None
    #: Garage as phones and browsers reach it; presigned URLs are signed for it.
    object_store_public_url: str | None = None
    object_store_region: str = "garage"
    #: Our own Photon geocoder (place_names.py), which names the places of
    #: captures the app couldn't name in the field. Self-hosted, so capture
    #: locations stay ours. Unset, no place names are derived.
    geocoder_url: str | None = None
    geocoder_timeout_seconds: float = 3.0
    #: How far from a capture a named place may be.
    geocoder_radius_km: float = 25.0

    @field_validator("oidc_audiences", mode="before")
    @classmethod
    def _split_audiences(cls, value: object) -> object:
        audiences = _split(value)
        if not audiences:
            raise ValueError("at least one audience is required")
        return audiences

    @field_validator("oidc_mobile_audiences", mode="before")
    @classmethod
    def _split_mobile_audiences(cls, value: object) -> object:
        return tuple(a for a in _split(value) if a != _NO_VALUE)

    @field_validator("oidc_mobile_issuer", mode="before")
    @classmethod
    def _unrendered_mobile_issuer(cls, value: object) -> object:
        if isinstance(value, str) and value.strip() in {"", _NO_VALUE}:
            return None
        return value

    @model_validator(mode="after")
    def _default_jwks_url(self) -> "Settings":
        if self.oidc_jwks_url is None:
            self.oidc_jwks_url = _jwks_under(self.oidc_issuer)
        # The mobile client is a soft render: half-seeded, it is off -- loudly
        # -- and never stops the API the web depends on (review of v0.6.1).
        if (self.oidc_mobile_issuer is None) != (not self.oidc_mobile_audiences):
            logger.warning(
                "the mobile OIDC client needs both FISHSENSE_OIDC_MOBILE_ISSUER and"
                " FISHSENSE_OIDC_MOBILE_AUDIENCES; only one is set, so mobile"
                " tokens are refused until both are seeded"
            )
            self.oidc_mobile_issuer = None
            self.oidc_mobile_audiences = ()
        if self.oidc_mobile_issuer is not None:
            if self.oidc_mobile_jwks_url is None:
                self.oidc_mobile_jwks_url = _jwks_under(self.oidc_mobile_issuer)
        if self.tenant_stores_file is not None and not (
            self.object_store_endpoint_url and self.object_store_public_url
        ):
            raise ValueError(
                "FISHSENSE_TENANT_STORES_FILE needs FISHSENSE_OBJECT_STORE_ENDPOINT_URL"
                " and FISHSENSE_OBJECT_STORE_PUBLIC_URL"
            )
        return self


def _split(value: object) -> object:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    return tuple(a for a in value if a)


def _jwks_under(issuer: str) -> str:
    return f"{issuer.rstrip('/')}/jwks/"


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


class DevSeedSettings(BaseSettings):
    """For ``dev-seed`` only: the local UI harness (compose.ui.yml), never
    production. The owner's DSN, and the local Garage: its admin API, to
    create the tenant buckets, and the stores file the API reads."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_")

    migration_database_url: SecretStr
    tenant_stores_file: Path
    object_store_endpoint_url: str
    object_store_public_url: str
    object_store_region: str = "garage"
    dev_garage_admin_url: str
    dev_garage_admin_token: SecretStr


class AuditSettings(BaseSettings):
    """For the operator commands that run as one role under a tenant's RLS
    scope: the read-only audits (``audit-range-trend``) and the dry-run-first
    ``backfill-mission-facts``.

    Just the database: an audit runs as the app role under a tenant's RLS
    scope, and needs none of the API's OIDC configuration.
    """

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_")

    #: DSN for the role the audit reads as -- normally the app role.
    database_url: SecretStr
