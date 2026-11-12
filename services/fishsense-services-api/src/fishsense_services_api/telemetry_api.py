"""What visitors' browsers measured of the web's pages (0091).

``POST /telemetry/page-timings``: the web's server posts, as its service
account, the timings its pages' browsers sent it (``apps/web/app/api/vitals``):
time to first byte and the Core Web Vitals on a page load, and ``NAV``, how
long a click within the app took to show the next page. Any signed-in caller
may post; each value is bounded, so a stray or hostile post can add noise but
not break a report. The rows land in ``page_timings`` through
``record_page_timings``, which the app role may call and nothing else.
"""

import json
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Response, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

#: The most one post carries; a page sends a handful.
MAX_TIMINGS = 50
#: Ten minutes, in milliseconds: anything longer is no page load's.
MAX_VALUE = 600_000

Metric = Literal["TTFB", "FCP", "LCP", "INP", "CLS", "NAV"]
Kind = Literal["load", "navigation"]
Route = Annotated[str, StringConstraints(pattern=r"^/", max_length=200)]


class PageTiming(BaseModel):
    #: The page's template, not its path (``/[tenant]/fish``).
    route: Route
    metric: Metric
    #: Milliseconds; ``CLS`` alone is a unitless score.
    value: float = Field(ge=0, le=MAX_VALUE)
    kind: Kind


class PageTimings(BaseModel):
    #: The web's release, so a change can be compared before and after.
    release: Annotated[str, StringConstraints(max_length=40)] | None = None
    timings: list[PageTiming] = Field(min_length=1, max_length=MAX_TIMINGS)


def add_telemetry_routes(
    app: FastAPI, *, engine: AsyncEngine, authenticate: Callable
) -> None:
    @app.post(
        "/telemetry/page-timings",
        operation_id="record_page_timings",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def record_page_timings(
        body: PageTimings, _caller: Annotated[object, Depends(authenticate)]
    ) -> Response:
        rows = [{**t.model_dump(), "release": body.release} for t in body.timings]
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT record_page_timings(CAST(:rows AS jsonb))"),
                {"rows": json.dumps(rows)},
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)
