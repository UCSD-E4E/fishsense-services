"""The model-class replay's GPU activity: `score_species_models`, one frame
(`score.score_frame`), its render read by presigned GET, over the species
stage's loaded BioCLIP (2.5, or 2 after an out-of-memory fall-back: the result
names which). A render not there or not decoding is the frame's status."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from temporalio import activity

from fishsense_services_contracts.species_models import (
    SpeciesModelsInput,
    SpeciesModelsResult,
)
from fishsense_services_processor.species_models.score import score_frame

__all__ = ["SpeciesModelsActivities"]


class SpeciesModelsActivities:  # pylint: disable=too-few-public-methods
    def __init__(
        self,
        *,
        store_factory: Callable[[], Any],
        classifier: Callable[[], Awaitable[Any]],
    ) -> None:
        self._store_factory = store_factory
        self._store: Any = None
        self._classifier = classifier

    def _object_store(self) -> Any:
        if self._store is None:
            self._store = self._store_factory()
        return self._store

    @activity.defn(name="score_species_models")
    async def score_species_models(
        self, request: SpeciesModelsInput
    ) -> SpeciesModelsResult:
        """The request's one frame, scored both ways."""
        (frame,) = request.frames
        ids = {"prediction_id": frame.prediction_id, "capture_id": frame.capture_id}
        try:
            jpeg = await self._object_store().download_image(url=frame.render_url)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            activity.logger.warning("capture=%s: render unavailable: %s",
                                    frame.capture_id, exc)  # fmt: skip
            return SpeciesModelsResult(**ids, status="render_unavailable")
        classifier = await self._classifier()
        found = await asyncio.to_thread(score_frame, jpeg, frame.mask_bbox,
                                        request.species, request.models, classifier)  # fmt: skip
        return SpeciesModelsResult(**ids, **found)
