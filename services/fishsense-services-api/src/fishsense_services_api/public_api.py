"""``GET /public/summary``: what the signed-out page may say, for anyone
(`public_store`; docs/design/README.md, Home pages). No caller, no tenant, and
nothing finer than a broad region.
"""

from dataclasses import asdict

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from fishsense_services_api.public_store import MINIMUM, public_summary


class PublicRegionView(BaseModel):
    name: str
    latitude: float
    longitude: float
    #: Rounded to the nearest 10; null below ``under``.
    count: int | None
    under: int | None


class PublicSummaryView(BaseModel):
    lengths: int
    #: Lengths of a species, not of a fish model.
    wild_fish: int
    #: Fish-model lengths: decoys of known length, in pool trials.
    model_lengths: int
    species: int
    missions: int
    deployments: int
    #: A region shows its count from this many wild fish.
    minimum: int
    regions: list[PublicRegionView]


def add_public_routes(app: FastAPI, *, engine: AsyncEngine) -> None:
    @app.get(
        "/public/summary",
        operation_id="get_public_summary",
        response_model=PublicSummaryView,
    )
    async def summary() -> PublicSummaryView:
        async with engine.connect() as conn:
            found = await public_summary(conn)
        return PublicSummaryView(**asdict(found), minimum=MINIMUM)
