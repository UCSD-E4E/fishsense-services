"""Process entry point: build the app from the environment.

uvicorn --factory fishsense_services_api.main:create_app_from_env
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from fishsense_services_api.app import create_app
from fishsense_services_api.auth import (
    JwksKeySource,
    MultiIssuerValidator,
    TokenValidator,
)
from fishsense_services_api.place_names import PhotonGeocoder
from fishsense_services_api.settings import Settings
from fishsense_services_api.tenant_stores import TenantStores


def create_app_from_env() -> FastAPI:
    settings = Settings()
    # No JIT: our views' estimated costs cross its thresholds, and compiling
    # cost more than running -- 9.0 s of the Today summary's 13.5 s on
    # production (2026-10-21).
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        connect_args={"server_settings": {"jit": "off"}},
    )

    def _issuer(issuer: str, audiences: tuple[str, ...], jwks_url: str):
        return TokenValidator(
            issuer=issuer,
            audiences=audiences,
            keys=JwksKeySource(
                jwks_url,
                cache_seconds=settings.jwks_cache_seconds,
                refetch_cooldown_seconds=settings.jwks_refetch_cooldown_seconds,
            ),
        )

    validators = [
        _issuer(settings.oidc_issuer, settings.oidc_audiences, settings.oidc_jwks_url)
    ]
    if settings.oidc_mobile_issuer is not None:
        validators.append(
            _issuer(
                settings.oidc_mobile_issuer,
                settings.oidc_mobile_audiences,
                settings.oidc_mobile_jwks_url,
            )
        )
    stores = None
    if settings.tenant_stores_file is not None:
        stores = TenantStores.from_file(
            settings.tenant_stores_file,
            endpoint_url=settings.object_store_endpoint_url,
            public_endpoint_url=settings.object_store_public_url,
            region=settings.object_store_region,
        )
    geocoder = None
    if settings.geocoder_url:
        geocoder = PhotonGeocoder(
            settings.geocoder_url,
            timeout_seconds=settings.geocoder_timeout_seconds,
            radius_km=settings.geocoder_radius_km,
        )
    app = create_app(
        engine=engine,
        validator=MultiIssuerValidator(validators),
        stores=stores,
        geocoder=geocoder,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app.router.lifespan_context = lifespan
    return app
