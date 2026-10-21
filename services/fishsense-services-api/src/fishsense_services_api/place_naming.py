"""Naming the phone captures the app couldn't name, after the response (0041).

A capture is named after its commit, and again whenever it is read and still
has no name: the geocoder may have been down the first time. The reads that
retry are a capture's own page and the Fish list. The capture list did it
until /captures was retired (phase 7b), and the Fish list, which is also how a
mission's fish are listed, took its place.

Each naming also places the capture's phone mission, if it had no place yet
(`record_place_name`).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncEngine

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.place_name_store import (
    needing_place_names,
    record_place_name,
)
from fishsense_services_api.place_names import Geocoder, GeocoderUnavailable

__all__ = ["MAX_PLACE_NAMES_PER_REQUEST", "PlaceNamer"]

#: At most this many geocoder calls per request; the rest wait for a later read.
MAX_PLACE_NAMES_PER_REQUEST = 20

logger = logging.getLogger(__name__)


class PlaceNamer:
    """``geocoder`` None: no derived names."""

    def __init__(self, engine: AsyncEngine, geocoder: Geocoder | None) -> None:
        self._engine = engine
        self._geocoder = geocoder

    def later(
        self,
        background: BackgroundTasks,
        tenant_id: uuid.UUID,
        capture_ids: Sequence[uuid.UUID],
    ) -> None:
        """Name those of ``capture_ids`` that need it, after the response."""
        if self._geocoder is not None and capture_ids:
            background.add_task(self._fill, tenant_id, list(capture_ids))

    async def _fill(self, tenant_id: uuid.UUID, capture_ids: list[uuid.UUID]) -> None:
        """An outage is logged and retried on a later read."""
        async with tenant_transaction(self._engine, tenant_id) as conn:
            unnamed = await needing_place_names(conn, tenant_id, capture_ids)
        for capture in unnamed[:MAX_PLACE_NAMES_PER_REQUEST]:
            try:
                found = await self._geocoder.reverse(
                    capture.latitude, capture.longitude
                )
            except GeocoderUnavailable as error:
                logger.warning("geocoder unavailable; place names wait: %s", error)
                return
            async with tenant_transaction(self._engine, tenant_id) as conn:
                # Another request may have named it meanwhile.
                if await needing_place_names(conn, tenant_id, [capture.capture_id]):
                    await record_place_name(conn, tenant_id, capture.capture_id, found)
