"""Head/tail in Label Studio: which dives, create, populate, and the backfill.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/:
select_dives_needing_headtail_population_activity.py,
create_headtail_label_studio_project_activity.py,
populate_headtail_label_studio_project_activity.py and
backfill_headtail_predictions_activity.py. Behaviour is v1's:

* **populate is prediction-gated** and **JPEG-gated**: an image is imported
  only once the detector has visited it (an abstention counts) and its
  stage-5.1 JPEG exists; tasks carry the prediction inline, tagged with the
  row's own tier;
* the supersede pass retires incomplete live rows the project no longer owns
  and exempts (same project AND still a candidate) -- never "targets";
* the project is published only when the import is complete and the project
  holds rows, never half-populated;
* the backfill attaches placeable predictions to incomplete live tasks,
  deduped on (task, tier) from one listing per project, then points each
  dive-owned project's `model_version` at the tier most of its predictions
  carry, or they stay invisible.

v2 changes: the target is (tenant, dive); the catalog's one snapshot replaces
v1's four SDK reads; a task shows the JPEG where the object store found it
(v1's key for a migrated frame); projects are the recorded ones
(`LabelProjects`); Label Studio is reached only through `LabelStudioClient`.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable
from typing import Any, List, Protocol, Sequence

from temporalio import activity

from fishsense_services_api.headtail_store import (
    HeadtailCandidate,
    PopulateCandidate,
    PopulateState,
)
from fishsense_services_contracts.headtail import headtail_model_version_tag
from fishsense_services_contracts.object_store import HEADTAIL_JPEG_FOLDER, ObjectRef
from fishsense_services_orchestrator.headtail.activities import HeadtailTarget
from fishsense_services_orchestrator.headtail.labeling import (
    HEADTAIL_LABELING_CONFIG_XML,
    HEADTAIL_PROJECT_TITLE_SUFFIX,
    build_task,
    prediction_annotations,
    select_attach_targets,
    select_predicted_capture_ids,
)
from fishsense_services_orchestrator.labels.label_studio import (
    LabelStudioClient,
    heartbeat_again,
)
from fishsense_services_orchestrator.labels.populate import (
    ImportResult,
    TaskImage,
    ensure_project_shows_predictions,
    import_tasks_and_record_labels,
    publish_label_studio_project,
)
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)

__all__ = ["HEAD_TAIL_KIND", "HeadtailLabelActivities", "HeadtailLabelCatalog"]

#: The kind head/tail projects are recorded under (label_studio_projects).
HEAD_TAIL_KIND = "head_tail"


class HeadtailLabelCatalog(Protocol):
    """See ``fishsense_services_api.headtail_store.HeadtailCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def dives_needing_headtail_population(
        self, tenant_id: uuid.UUID
    ) -> list[HeadtailCandidate]: ...

    async def headtail_populate_state(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> PopulateState: ...

    async def record_head_tail_task(
        self,
        tenant_id: uuid.UUID,
        capture_id: uuid.UUID,
        *,
        ls_project_id: int,
        ls_task_id: int,
    ) -> None: ...

    async def supersede_head_tail_labels(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        label_ids: Sequence[uuid.UUID],
    ) -> int: ...


class HeadtailLabelActivities:
    def __init__(
        self,
        *,
        catalog: HeadtailLabelCatalog,
        store: OrchestratorObjectStore,
        label_studio_factory: Callable[[], LabelStudioClient],
        label_projects_factory: Callable[[], Any],
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._label_studio_factory = label_studio_factory
        self._label_projects_factory = label_projects_factory

    @activity.defn(name="select_dives_needing_headtail_population")
    async def select_dives_needing_headtail_population(self) -> List[HeadtailTarget]:
        """Every dive needing head/tail tasks (re)populated, across tenants,
        oldest first: the parent fans out one populate per dive."""
        found = [
            (candidate.created_at, str(candidate.dive_id), tenant_id, candidate)
            for tenant_id in await self._catalog.member_tenants()
            for candidate in await self._catalog.dives_needing_headtail_population(
                tenant_id
            )
        ]
        targets = [
            HeadtailTarget(tenant_id, candidate.dive_id)
            for _, _, tenant_id, candidate in sorted(found, key=lambda f: f[:2])
        ]
        activity.logger.info("%d dive(s) need headtail population", len(targets))
        return targets

    @activity.defn(name="create_headtail_label_studio_project")
    async def create_headtail_label_studio_project(self, target: HeadtailTarget) -> int:
        """The dive's head/tail project, found (the record, then v1's title)
        or created, and recorded."""
        project_id = await self._label_projects_factory().ensure_dive_project(
            target.tenant_id,
            target.dive_id,
            HEAD_TAIL_KIND,
            suffix=HEADTAIL_PROJECT_TITLE_SUFFIX,
            labeling_config_xml=HEADTAIL_LABELING_CONFIG_XML,
        )
        activity.logger.info(
            "headtail LS project dive=%s project_id=%d", target.dive_id, project_id
        )
        return project_id

    async def _with_jpeg(
        self, tenant_id: uuid.UUID, candidates: list[PopulateCandidate]
    ) -> list[tuple[PopulateCandidate, ObjectRef]]:
        """The candidates whose stage-5.1 JPEG is written, with where it is.
        A task pointing at nothing wedged dive 84; deferring costs nothing."""
        present = []
        for candidate in candidates:
            ref = await self._store.locate_processed_jpeg(
                tenant_id,
                HEADTAIL_JPEG_FOLDER,
                candidate.checksum,
                from_v1=candidate.from_v1,
            )
            heartbeat_again()
            if ref is None:
                activity.logger.info(
                    "headtail JPEG not yet written for capture %s; deferring",
                    candidate.capture_id,
                )
                continue
            present.append((candidate, ref))
        return present

    @activity.defn(name="populate_headtail_label_studio_project")
    async def populate_headtail_label_studio_project(
        self, target: HeadtailTarget, project_id: int
    ) -> int:
        """Import the dive's ready head/tail tasks, anchor a row per task,
        retire the rows the project no longer owns, and publish when whole.
        Returns the number of rows recorded."""
        state = await self._catalog.headtail_populate_state(
            target.tenant_id, target.dive_id
        )
        predictions = {p.capture_id: p for p in state.predictions}
        predicted = select_predicted_capture_ids(state.predictions)
        targets = await self._with_jpeg(
            target.tenant_id,
            [c for c in state.candidates if c.capture_id in predicted],
        )

        ls = self._label_studio_factory()
        result = ImportResult(recorded=0, deferred=0)
        if targets:

            async def _record(item: tuple[PopulateCandidate, ObjectRef], task_id):
                await self._catalog.record_head_tail_task(
                    target.tenant_id,
                    item[0].capture_id,
                    ls_project_id=project_id,
                    ls_task_id=task_id,
                )

            tasks = [
                build_task(
                    TaskImage(candidate.number, ref, candidate.captured_at),
                    predictions.get(candidate.capture_id),
                )
                for candidate, ref in targets
            ]
            result = await import_tasks_and_record_labels(
                ls,
                project_id=project_id,
                tasks=tasks,
                record_label=_record,
                items=targets,
            )
        else:
            activity.logger.info(
                "dive %s has no predicted, rendered laser-valid images needing "
                "head/tail labels; skipping task import",
                target.dive_id,
            )

        # Exempt on CANDIDATES, not on what survived the gates: "not yet" is
        # not "no longer wanted" (projects erased from the landing page while
        # their work was outstanding), and same project too (a legacy row on
        # a refreshed image kept the dive incomplete forever).
        candidates = {c.capture_id for c in state.candidates}
        stale = [
            label.id
            for label in state.labels
            if not label.completed
            and not (
                label.ls_project_id == project_id and label.capture_id in candidates
            )
        ]
        if stale:
            await self._catalog.supersede_head_tail_labels(
                target.tenant_id, target.dive_id, stale
            )

        if result.complete and (
            result.recorded > 0
            or any(label.ls_project_id == project_id for label in state.labels)
        ):
            await publish_label_studio_project(ls, project_id)
        return result.recorded

    @activity.defn(name="backfill_headtail_predictions_for_dive")
    async def backfill_headtail_predictions_for_dive(
        self, target: HeadtailTarget
    ) -> int:
        """Attach the dive's placeable predictions to its existing incomplete
        tasks, and make them visible. Idempotent; returns how many attached."""
        state = await self._catalog.headtail_populate_state(
            target.tenant_id, target.dive_id
        )
        targets = select_attach_targets(state.predictions, state.labels)
        if not targets:
            activity.logger.info(
                "dive %s: no placeable head/tail predictions with attachable tasks",
                target.dive_id,
            )
            return 0

        ls = self._label_studio_factory()
        by_capture = {p.capture_id: p for p in state.predictions}
        already = set()
        for project_id in sorted({project for _, project in targets.values()}):
            for prediction in await ls.predictions(project_id):
                already.add((prediction.task_id, prediction.model_version))

        attached = 0
        # Counted over every placeable prediction, not only the new ones: on a
        # re-run nothing attaches, and the project must still show its tier.
        tags_by_project: dict[int, Counter] = {}
        for capture_id, (task_id, project_id) in targets.items():
            (body,) = prediction_annotations(by_capture[capture_id])
            tags_by_project.setdefault(project_id, Counter())[
                body["model_version"]
            ] += 1
            if (task_id, body["model_version"]) in already:
                continue
            await ls.create_prediction(task_id, body["model_version"], body["result"])
            attached += 1

        await ensure_project_shows_predictions(
            ls, state.dive_number, tags_by_project, headtail_model_version_tag()
        )
        activity.logger.info(
            "dive %s: attached %d head/tail prediction(s) to existing tasks",
            target.dive_id,
            attached,
        )
        return attached
