"""The species pre-annotation stage's orchestrator activities: select, resolve,
persist, and the backfill.

**New in v2; v1 has no counterpart.** The classifier (BioCLIP, on the
processor) is ported from coral-gardeners-fish-detector@67c8627; these
activities are built the way head/tail prediction's are
(`headtail.activities`, `headtail.populate`'s backfill), and keep their rules:

* the target is (tenant, dive), and the selector takes the best candidate
  across every tenant the orchestrator serves: never-predicted work first, so
  the fallback's permanently stale rows can't starve it; then the oldest;
* **the orchestrator issues the keys** (PLAN.md §9.11): each fish's image is
  the head/tail stage's rendered JPEG, where the object store finds it (v1's
  key for a migrated frame); one not yet written is deferred;
* the candidates are the species labeling config's (`candidates`), so the
  processor never reads it;
* a refusal of the processor's output is final (non-retryable), and a worker
  status (`skipped_no_upgrade_available`) is refused, never written;
* the backfill attaches each suggestion to its capture's incomplete task,
  deduped on (task, tag), then points each dive-owned project's
  `model_version` at the tag most of its tasks carry, or they stay invisible
  (`labels.populate.ensure_project_shows_predictions`).

**Pre-annotation only, and off by default** (`settings`): nothing here writes
or completes a species label, and with the stage disabled the backfill
attaches nothing, so predictions can be collected for an evaluation without a
labeler seeing one.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, Sequence

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.species_prediction_store import (
    InvalidSpeciesPredictions,
    SpeciesPredictCapture,
    SpeciesPredictionCandidate,
    SpeciesPredictionRow,
    SpeciesPredictionState,
)
from fishsense_services_api.species_store import SpeciesPopulationFacts
from fishsense_services_contracts.object_store import HEADTAIL_JPEG_FOLDER
from fishsense_services_contracts.species_prediction import (
    SPECIES_PREDICTOR_VERSION,
    SPECIES_STATUSES,
    PredictSpeciesImage,
    PredictSpeciesImagesInput,
    SpeciesPredictionResult,
)
from fishsense_services_orchestrator.labels.label_studio import LabelStudioClient
from fishsense_services_orchestrator.labels.populate import (
    ensure_project_shows_predictions,
)
from fishsense_services_orchestrator.species.populate import sentinel_judgements
from fishsense_services_orchestrator.species_predict.candidates import (
    species_candidates,
)
from fishsense_services_orchestrator.species_predict.labeling import (
    prediction_annotations,
    project_tags,
    select_attach_targets,
    species_model_version_tag,
)
from fishsense_services_orchestrator.species_predict.settings import (
    SpeciesPredictionSettings,
)

__all__ = [
    "SpeciesPredictActivities",
    "SpeciesPredictTarget",
    "SpeciesPredictionCatalog",
]


@dataclass(frozen=True)
class SpeciesPredictTarget:
    """A dive of a tenant."""

    tenant_id: uuid.UUID
    dive_id: uuid.UUID


class SpeciesPredictionCatalog(Protocol):
    """See ``fishsense_services_api.species_prediction_store``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def next_dive_for_species_prediction(
        self, tenant_id: uuid.UUID, *, predictor_version: int
    ) -> SpeciesPredictionCandidate | None: ...

    async def species_predict_captures(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, *, predictor_version: int
    ) -> list[SpeciesPredictCapture]: ...

    async def persist_species_predictions(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        rows: Sequence[SpeciesPredictionRow],
    ) -> int: ...

    async def species_prediction_state(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> SpeciesPredictionState: ...


class SpeciesLabelsCatalog(Protocol):
    """The part of ``fishsense_services_api.species_store.SpeciesCatalog``
    read here: the dive's species labels, sentinels included."""

    async def species_population_facts(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> SpeciesPopulationFacts: ...


def _refuse(target: SpeciesPredictTarget, why: str) -> ApplicationError:
    return ApplicationError(
        f"refusing the processor's species predictions for dive "
        f"{target.dive_id}: {why}",
        type="InvalidPredictions",
        non_retryable=True,
    )


class SpeciesPredictActivities:
    # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        *,
        catalog: SpeciesPredictionCatalog,
        species_catalog: SpeciesLabelsCatalog,
        store: Any,
        settings: SpeciesPredictionSettings,
        label_studio_factory: Callable[[], LabelStudioClient],
    ) -> None:
        self._catalog = catalog
        self._species_catalog = species_catalog
        self._store = store
        self._settings = settings
        self._label_studio_factory = label_studio_factory

    @activity.defn(name="select_next_dive_for_species_prediction")
    async def select_next_dive_for_species_prediction(
        self,
    ) -> Optional[SpeciesPredictTarget]:
        """The next dive for BioCLIP across tenants: never-predicted work
        first, then the oldest."""
        best, best_key = None, None
        for tenant_id in await self._catalog.member_tenants():
            candidate = await self._catalog.next_dive_for_species_prediction(
                tenant_id, predictor_version=SPECIES_PREDICTOR_VERSION
            )
            if candidate is None:
                continue
            key = (
                not candidate.never_predicted,
                candidate.created_at,
                str(candidate.dive_id),
            )
            if best_key is None or key < best_key:
                best = SpeciesPredictTarget(tenant_id, candidate.dive_id)
                best_key = key
        activity.logger.info("next dive for species prediction: %s", best)
        return best

    @activity.defn(name="resolve_species_predict_inputs")
    async def resolve_species_predict_inputs(
        self, target: SpeciesPredictTarget
    ) -> PredictSpeciesImagesInput:
        """The dive's fish needing a prediction whose head/tail JPEG is
        written, each with its mask's box; the rest are deferred."""
        captures = await self._catalog.species_predict_captures(
            target.tenant_id,
            target.dive_id,
            predictor_version=SPECIES_PREDICTOR_VERSION,
        )
        images = []
        for capture in captures:
            jpeg = await self._store.locate_processed_jpeg(
                target.tenant_id,
                HEADTAIL_JPEG_FOLDER,
                capture.checksum,
                from_v1=capture.from_v1,
            )
            activity.heartbeat()
            if jpeg is None:
                activity.logger.info(
                    "headtail JPEG not in Garage for capture %s; deferring",
                    capture.capture_id,
                )
                continue
            images.append(
                PredictSpeciesImage(
                    capture_id=capture.capture_id,
                    headtail_prediction_id=capture.headtail_prediction_id,
                    jpeg=jpeg,
                    mask_bbox=capture.mask_bbox,
                    has_existing_prediction=capture.has_existing_prediction,
                )
            )
        activity.logger.info(
            "resolved species predict inputs dive=%s needing=%d deferred_no_jpeg=%d",
            target.dive_id,
            len(images),
            len(captures) - len(images),
        )
        return PredictSpeciesImagesInput(
            tenant_id=target.tenant_id,
            dive_id=target.dive_id,
            candidates=species_candidates(),
            images=images,
        )

    @activity.defn(name="persist_species_predictions")
    async def persist_species_predictions(
        self, target: SpeciesPredictTarget, results: List[SpeciesPredictionResult]
    ) -> int:
        """Append each prediction, abstentions included (the cohort selects on
        a row's absence). A refusal is final."""
        refused = sorted({r.status for r in results} - set(SPECIES_STATUSES))
        if refused:
            raise _refuse(target, f"status {refused} is not a prediction")
        choices = {c.choice for c in species_candidates()}
        named = {r.predicted_choice for r in results if r.predicted_choice} | {
            s.choice for r in results for s in r.top5
        }
        if foreign := sorted(named - choices):
            raise _refuse(target, f"{foreign} are not candidates")
        if any(r.predictor_version is None or not r.model_id for r in results):
            raise _refuse(target, "a result names no predictor version or model")
        rows = [
            SpeciesPredictionRow(
                capture_id=r.capture_id,
                headtail_prediction_id=r.headtail_prediction_id,
                status=r.status,
                predictor_version=r.predictor_version,
                model_id=r.model_id,
                predicted_choice=r.predicted_choice,
                top1_probability=r.top1_probability,
                margin=r.margin,
                top5=[s.model_dump() for s in r.top5],
            )
            for r in results
        ]
        try:
            written = await self._catalog.persist_species_predictions(
                target.tenant_id, target.dive_id, rows
            )
        except InvalidSpeciesPredictions as exc:
            raise _refuse(target, str(exc)) from exc
        activity.logger.info(
            "persisted %d species predictions for dive=%s", written, target.dive_id
        )
        return written

    @activity.defn(name="backfill_species_predictions_for_dive")
    async def backfill_species_predictions_for_dive(
        self, target: SpeciesPredictTarget
    ) -> int:
        """Attach the dive's suggestions to its existing incomplete species
        tasks, and make them visible; nothing while the stage is disabled.
        Idempotent; returns how many were attached."""
        if not self._settings.enabled:
            activity.logger.info(
                "species prediction is disabled; attaching nothing for dive %s",
                target.dive_id,
            )
            return 0
        threshold = self._settings.other_threshold
        state = await self._catalog.species_prediction_state(
            target.tenant_id, target.dive_id
        )
        facts = await self._species_catalog.species_population_facts(
            target.tenant_id, target.dive_id
        )
        judged = set(sentinel_judgements(facts.species_labels))
        targets = select_attach_targets(state.predictions, state.tasks, judged)
        if not targets:
            activity.logger.info(
                "dive %s: no species suggestions with attachable tasks",
                target.dive_id,
            )
            return 0

        ls = self._label_studio_factory()
        already = set()
        for project_id in sorted({project for _, project in targets.values()}):
            for prediction in await ls.predictions(project_id):
                already.add((prediction.task_id, prediction.model_version))

        by_capture = {p.capture_id: p for p in state.predictions}
        attached = 0
        for capture_id, (task_id, _) in targets.items():
            (body,) = prediction_annotations(by_capture[capture_id], threshold)
            if (task_id, body["model_version"]) in already:
                continue
            await ls.create_prediction(task_id, body["model_version"], body["result"])
            attached += 1

        # Counted over every task, not only the new ones: on a re-run nothing
        # attaches, and the project must still show its suggestions.
        await ensure_project_shows_predictions(
            ls,
            state.dive_number,
            project_tags(state, judged, threshold),
            species_model_version_tag(SPECIES_PREDICTOR_VERSION, threshold),
        )
        activity.logger.info(
            "dive %s: attached %d species suggestion(s) to existing tasks",
            target.dive_id,
            attached,
        )
        return attached
