"""Process entry point: build the app from the environment.

uvicorn --factory fishsense_services_api.main:create_app_from_env
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from fishsense_services_api.app import create_app
from fishsense_services_api.auth import JwksKeySource, TokenValidator
from fishsense_services_api.settings import Settings


def create_app_from_env() -> FastAPI:
    settings = Settings()
    engine = create_async_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True
    )
    validator = TokenValidator(
        issuer=settings.oidc_issuer,
        audiences=settings.oidc_audiences,
        keys=JwksKeySource(
            settings.oidc_jwks_url,
            cache_seconds=settings.jwks_cache_seconds,
            refetch_cooldown_seconds=settings.jwks_refetch_cooldown_seconds,
        ),
    )
    app = create_app(engine=engine, validator=validator)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app.router.lifespan_context = lifespan
    return app
