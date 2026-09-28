"""The species stages' orchestrator activities.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/:

* select_next_high_priority_dive_for_species_preprocessing_activity.py,
  resolve_species_preprocess_inputs_activity.py,
  clear_species_reprocess_flags_activity.py (stage 2);
* create_species_label_studio_project_activity.py,
  select_dives_needing_species_population_activity.py,
  populate_species_label_studio_project_activity.py (Label Studio);
* get_species_label_studio_project_ids_activity.py,
  sync_species_labels_for_label_studio_project_activity.py (stage 4.2);
* update_dive_image_groups_activity.py (stage 6.1).

v1's were SDK round trips over the API; v2's call the species catalog
(`fishsense_services_api.species_store`), which owns the cohorts and the
writes, and keep v1's decisions here, where v1's tests pin them.

v2 changes:

* targets are (tenant, dive); the selectors take the oldest candidate across
  every tenant the orchestrator serves;
* the resolver hands the processor the refs to read and write (the staged raw
  frame, and the JPEG target -- over v1's JPEG for a migrated frame), and the
  dive's device's current intrinsics stand in for v1's `dive.camera_id`;
* populate is canonical-only, like its cohort; its JPEG gate is the object
  store's `locate_processed_jpeg`, whose answer is also the task's URL;
* the sync applies each task through a column-scoped update, and a dive-link
  write expires a standing calibration refusal (see species_store);
* stage 6.1 persists all or nothing, and the catalog's refusal of a group
  set is final (non-retryable).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any, List, Optional, Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.clustering_store import InvalidClusters
from fishsense_services_api.label_sync_store import SpeciesSync, SyncedLabel
from fishsense_services_api.species_store import (
    SpeciesCandidate,
    SpeciesGroupingFacts,
    SpeciesPopulationFacts,
    SpeciesPreprocessFacts,
)
from fishsense_services_contracts import taxonomy
from fishsense_services_contracts.object_store import SPECIES_JPEG_FOLDER
from fishsense_services_contracts.species import (
    PreprocessSpeciesImagesInput,
    SpeciesClusterMember,
)
from fishsense_services_orchestrator.labels.label_studio import (
    LabelStudioTask,
    heartbeat_again,
)
from fishsense_services_orchestrator.labels.populate import (
    ImportResult,
    TaskImage,
    import_tasks_and_record_labels,
    publish_label_studio_project,
)
from fishsense_services_orchestrator.labels.sync import (
    LabelProject,
    sync_label_studio_project,
)
from fishsense_services_orchestrator.species.contracts import (
    ClearReprocessFlagsInput,
    SpeciesTarget,
    UpdateDiveImageGroupsResult,
)
from fishsense_services_orchestrator.species.grouping import (
    regroup_by_species_labels,
    select_species_label_per_image,
)
from fishsense_services_orchestrator.species.labeling import (
    SPECIES_LABELING_CONFIG_XML,
    SPECIES_PROJECT_TITLE_SUFFIX,
)
from fishsense_services_orchestrator.species.parsing import (
    calibration_target_choice,
    reduce_winners,
    slate_not_in_list,
    slate_type_choice,
    species_sync_from_task,
)
from fishsense_services_orchestrator.species.populate import (
    build_species_task,
    select_target_captures,
    sentinel_judgements,
)
from fishsense_services_orchestrator.species.preprocess import plan_species_preprocess

__all__ = ["KIND", "SpeciesActivities", "SpeciesCatalog", "unidentified_slate_note"]

#: The Label Studio project kind and sync-cursor kind.
KIND = "species"


def unidentified_slate_note() -> str:
    """v1's note for a dive whose labelers said its slate is not a template."""
    return (
        f"Species labeling reported '{taxonomy.SLATE_NOT_IN_LIST_LEAF}': "
        "the slate in frame is not one of the slate templates, so "
        "this dive cannot self-calibrate. Link a sibling dive via its "
        "calibration source, or set priority=NONE to park it."
    )


class SpeciesCatalog(Protocol):
    """See ``fishsense_services_api.species_store.SpeciesCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def next_dive_for_species_preprocessing(
        self, tenant_id: uuid.UUID
    ) -> SpeciesCandidate | None: ...

    async def dives_needing_species_population(
        self, tenant_id: uuid.UUID
    ) -> list[SpeciesCandidate]: ...

    async def species_preprocess_facts(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> SpeciesPreprocessFacts | None: ...

    async def set_species_needs_reprocess(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        value: bool,
        *,
        only_incomplete: bool = True,
        capture_ids: list[uuid.UUID] | None = None,
    ) -> int: ...

    async def species_population_facts(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> SpeciesPopulationFacts: ...

    async def record_species_label(
        self,
        tenant_id: uuid.UUID,
        *,
        capture_id: uuid.UUID,
        ls_project_id: int,
        ls_task_id: int,
        image_url: str,
    ) -> bool: ...

    async def supersede_species_labels(
        self, tenant_id: uuid.UUID, label_ids: list[uuid.UUID]
    ) -> int: ...

    async def species_grouping_facts(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> SpeciesGroupingFacts: ...

    async def persist_label_studio_clusters(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, groups: list[list[uuid.UUID]]
    ) -> int | None: ...

    async def slate_templates_by_name(
        self, tenant_id: uuid.UUID
    ) -> dict[str, uuid.UUID]: ...

    async def calibration_targets_by_name(
        self, tenant_id: uuid.UUID
    ) -> dict[str, uuid.UUID]: ...

    async def set_dive_slate_template(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, slate_template_id: uuid.UUID
    ) -> bool: ...

    async def set_dive_calibration_target(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        calibration_target_id: uuid.UUID,
    ) -> bool: ...

    async def note_unidentified_slate(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, note: str
    ) -> bool: ...


class SpeciesSyncCatalog(Protocol):
    """See ``fishsense_services_api.label_sync_store.LabelSyncCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def label_studio_projects(
        self, tenant_id: uuid.UUID, kind: str
    ) -> list[int]: ...

    async def apply_species_sync(
        self, tenant_id: uuid.UUID, ls_task_id: int, sync: SpeciesSync
    ) -> SyncedLabel | None: ...

    async def sync_cursor(
        self, tenant_id: uuid.UUID, kind: str, ls_project_id: int
    ) -> datetime | None: ...

    async def advance_sync_cursor(
        self,
        tenant_id: uuid.UUID,
        kind: str,
        ls_project_id: int,
        last_synced_at: datetime,
    ) -> None: ...


class SpeciesActivities:
    # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        *,
        catalog: SpeciesCatalog,
        store: Any,
        sync_catalog: SpeciesSyncCatalog | None = None,
        label_projects: Any = None,
        label_studio_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._catalog = catalog
        self._store = store
        self._sync_catalog = sync_catalog
        self._label_projects = label_projects
        self._label_studio_factory = label_studio_factory

    # -- stage 2 -------------------------------------------------------------------

    @activity.defn(name="select_next_dive_for_species_preprocessing")
    async def select_next_dive_for_species_preprocessing(
        self,
    ) -> Optional[SpeciesTarget]:
        """The oldest dive in the stage-2 cohort, across tenants."""
        best: SpeciesTarget | None = None
        best_key = None
        for tenant_id in await self._catalog.member_tenants():
            candidate = await self._catalog.next_dive_for_species_preprocessing(
                tenant_id
            )
            if candidate is None:
                continue
            key = (candidate.created_at, str(candidate.dive_id))
            if best_key is None or key < best_key:
                best, best_key = SpeciesTarget(tenant_id, candidate.dive_id), key
        if best is None:
            activity.logger.info("no high-priority dives needing species preprocessing")
        else:
            activity.logger.info(
                "next high-priority dive needing species preprocessing: "
                "tenant=%s dive=%s",
                best.tenant_id,
                best.dive_id,
            )
        return best

    @activity.defn(name="resolve_species_preprocess_inputs")
    async def resolve_species_preprocess_inputs(
        self, target: SpeciesTarget
    ) -> PreprocessSpeciesImagesInput:
        """What the processor draws: each eligible frame, its i/N, its refs."""
        facts = await self._catalog.species_preprocess_facts(
            target.tenant_id, target.dive_id
        )
        if facts is None:
            raise ValueError(f"dive_id={target.dive_id} not found")
        if facts.device_id is None:
            # v1: "has no camera_id" -- the camera is v2's device.
            raise ValueError(f"dive_id={target.dive_id} has no device")
        if facts.intrinsics is None:
            raise ValueError(f"device_id={facts.device_id} has no intrinsics")

        groups = plan_species_preprocess(facts)
        members = []
        for group in groups:
            planned = []
            for member in group:
                capture = member.capture
                planned.append(
                    SpeciesClusterMember(
                        capture_id=capture.capture_id,
                        raw=self._store.layout.raw(target.tenant_id, capture.checksum),
                        jpeg=await self._store.processed_jpeg_target(
                            target.tenant_id,
                            SPECIES_JPEG_FOLDER,
                            capture.checksum,
                            from_v1=capture.from_v1,
                        ),
                        cluster_index=member.cluster_index,
                        cluster_size=member.cluster_size,
                    )
                )
            members.append(planned)
        activity.logger.info(
            "resolved species preprocess inputs dive=%s prediction_clusters=%d "
            "groups=%d total_captures=%d",
            target.dive_id,
            len(facts.prediction_clusters),
            len(members),
            sum(len(group) for group in members),
        )
        return PreprocessSpeciesImagesInput(
            dive_id=target.dive_id,
            camera_matrix=facts.intrinsics.camera_matrix,
            distortion_coefficients=facts.intrinsics.distortion_coefficients,
            cluster_members=members,
        )

    @activity.defn(name="clear_species_reprocess_flags")
    async def clear_species_reprocess_flags(
        self, payload: ClearReprocessFlagsInput
    ) -> int:
        """Lower `needs_reprocess` on the dive's canonical species labels -- the
        named frames, or the whole dive. Idempotent: 0 when none were up."""
        cleared = await self._catalog.set_species_needs_reprocess(
            payload.tenant_id,
            payload.dive_id,
            False,
            capture_ids=payload.capture_ids,
        )
        activity.logger.info(
            "cleared species reprocess flags dive=%s rows=%d scope=%s",
            payload.dive_id,
            cleared,
            "whole dive" if payload.capture_ids is None else len(payload.capture_ids),
        )
        return cleared

    # -- Label Studio: create and populate ------------------------------------------

    @activity.defn(name="create_species_label_studio_project")
    async def create_species_label_studio_project(self, target: SpeciesTarget) -> int:
        """The dive's species project, found or created (as a draft) and healed
        onto the current labeling config; its Label Studio id."""
        activity.logger.info("create species LS project dive=%s", target.dive_id)
        project_id = await self._label_projects.ensure_dive_project(
            target.tenant_id,
            target.dive_id,
            KIND,
            suffix=SPECIES_PROJECT_TITLE_SUFFIX,
            labeling_config_xml=SPECIES_LABELING_CONFIG_XML,
        )
        activity.logger.info(
            "species LS project dive=%s project_id=%d", target.dive_id, project_id
        )
        return project_id

    @activity.defn(name="select_dives_needing_species_population")
    async def select_dives_needing_species_population(self) -> List[SpeciesTarget]:
        """Every dive needing species tasks (re)populated, oldest first, across
        tenants."""
        candidates = []
        for tenant_id in await self._catalog.member_tenants():
            for candidate in await self._catalog.dives_needing_species_population(
                tenant_id
            ):
                candidates.append((candidate.created_at, str(candidate.dive_id),
                                   SpeciesTarget(tenant_id, candidate.dive_id)))  # fmt: skip
        targets = [target for *_, target in sorted(candidates)]
        activity.logger.info("%d dive(s) need species population", len(targets))
        return targets

    @activity.defn(name="populate_species_label_studio_project")
    async def populate_species_label_studio_project(
        self, target: SpeciesTarget, ls_project_id: int
    ) -> int:
        """Push species tasks for the dive and supersede stale rows; the number
        of label rows written."""
        ls = self._label_studio_factory()
        facts = await self._catalog.species_population_facts(
            target.tenant_id, target.dive_id
        )
        selected = select_target_captures(
            facts.candidates, facts.species_labels, ls_project_id
        )

        # The JPEG gate: never seed a row before stage 2 has written the frame,
        # or it leaves the preprocess cohort with no JPEG, forever.
        targets = []
        for capture in selected:
            image = await self._store.locate_processed_jpeg(
                target.tenant_id,
                SPECIES_JPEG_FOLDER,
                capture.checksum,
                from_v1=capture.from_v1,
            )
            if image is None:
                activity.logger.info(
                    "species JPEG not yet in Garage for capture %s (checksum=%s); "
                    "deferring to a later populate run",
                    capture.capture_id,
                    capture.checksum,
                )
            else:
                targets.append((capture, image))
            heartbeat_again()
        deferred = len(selected) - len(targets)

        import_result = ImportResult(recorded=0, deferred=0)
        if targets:
            judgements = sentinel_judgements(facts.species_labels)
            items = [
                (capture, TaskImage(capture.number, image, capture.captured_at))
                for capture, image in targets
            ]
            tasks = [
                build_species_task(task_image, judgements.get(capture.capture_id))
                for capture, task_image in items
            ]

            async def record(item, task_id: int) -> None:
                capture, task_image = item
                if not await self._catalog.record_species_label(
                    target.tenant_id,
                    capture_id=capture.capture_id,
                    ls_project_id=ls_project_id,
                    ls_task_id=task_id,
                    image_url=task_image.image.uri,
                ):
                    # A migrated duplicate holds the task (species_store).
                    activity.logger.warning(
                        "species task %d in project %d is already anchored to "
                        "another capture's label; left it there and wrote no "
                        "row for capture %s",
                        task_id,
                        ls_project_id,
                        capture.capture_id,
                    )

            import_result = await import_tasks_and_record_labels(
                ls,
                project_id=ls_project_id,
                tasks=tasks,
                record_label=record,
                items=items,
            )
        else:
            activity.logger.info(
                "dive %s has no laser-valid frames needing species labels; "
                "skipping task import",
                target.dive_id,
            )

        # Supersede pass: retire open rows of *other* projects, never this
        # project's own, and never a sentinel -- it is the pre-annotation
        # source, and a dead-lettered judgement is lost for any frame this
        # run deferred.
        stale = [
            label.id
            for label in facts.species_labels
            if not label.completed
            and not label.superseded
            and label.ls_project_id is not None
            and label.ls_project_id != ls_project_id
        ]
        if stale:
            await self._catalog.supersede_species_labels(target.tenant_id, stale)
            heartbeat_again()

        # Publish only a complete task set: nothing deferred, the import fully
        # visible, and the project holding tasks.
        already_in_project = any(
            label.ls_project_id == ls_project_id and not label.superseded
            for label in facts.species_labels
        )
        if (
            deferred == 0
            and import_result.complete
            and (import_result.recorded > 0 or already_in_project)
        ):
            await publish_label_studio_project(ls, ls_project_id)
        return import_result.recorded

    # -- stage 4.2: the sync ----------------------------------------------------------

    @activity.defn(name="species_label_projects")
    async def species_label_projects(self) -> List[LabelProject]:
        """Every served tenant's Label Studio projects holding live species
        labels."""
        projects = [
            LabelProject(tenant_id, project_id)
            for tenant_id in await self._sync_catalog.member_tenants()
            for project_id in await self._sync_catalog.label_studio_projects(
                tenant_id, KIND
            )
        ]
        activity.logger.info("found %d species label projects", len(projects))
        return projects

    @activity.defn(name="sync_species_labels")
    async def sync_species_labels(self, project: LabelProject) -> None:
        """Sync one project's species labels in, then set each dive's slate
        template and calibration target from what its labelers picked."""
        completed: list[tuple[uuid.UUID | None, datetime | None, list[dict]]] = []
        skipped = 0

        async def apply(task: LabelStudioTask) -> None:
            nonlocal skipped
            applied = await self._sync_catalog.apply_species_sync(
                project.tenant_id, task.id, species_sync_from_task(task)
            )
            if applied is None:
                skipped += 1
                return
            if task.is_labeled and task.annotations:
                results = task.annotations[0].get("result") or []
                completed.append((applied.dive_id, task.updated_at, results))

        await sync_label_studio_project(
            project,
            KIND,
            ls=self._label_studio_factory(),
            catalog=self._sync_catalog,
            apply=apply,
        )
        if skipped:
            activity.logger.info(
                "species sync project_id=%d skipped %d task(s) with no label",
                project.ls_project_id,
                skipped,
            )
        await self._resolve_dive_links(project.tenant_id, completed)

    async def _resolve_dive_links(
        self,
        tenant_id: uuid.UUID,
        completed: list[tuple[uuid.UUID | None, datetime | None, list[dict]]],
    ) -> None:
        """Set each dive's slate template and calibration target from its
        completed frames (most recent wins), and note a dive whose slate its
        labelers could not identify. The two links are independent."""
        if not completed:
            return
        slates = await self._catalog.slate_templates_by_name(tenant_id)
        targets = await self._catalog.calibration_targets_by_name(tenant_id)

        slate_votes, target_votes, unidentified = [], [], set()
        for dive_id, ts, results in completed:
            if dive_id is None:
                continue
            slate = slate_type_choice(results, set(slates))
            if slate is not None:
                slate_votes.append((dive_id, ts, slates[slate]))
            elif slate_not_in_list(results):
                unidentified.add(dive_id)
            board = calibration_target_choice(results, set(targets))
            if board is not None:
                target_votes.append((dive_id, ts, targets[board]))

        slate_winners = reduce_winners(slate_votes)
        for dive_id, slate_id in slate_winners.items():
            await self._catalog.set_dive_slate_template(tenant_id, dive_id, slate_id)
            activity.logger.info(
                "species sync set slate template dive=%s slate=%s", dive_id, slate_id
            )
        for dive_id, target_id in reduce_winners(target_votes).items():
            await self._catalog.set_dive_calibration_target(
                tenant_id, dive_id, target_id
            )
            activity.logger.info(
                "species sync set calibration target dive=%s target=%s",
                dive_id,
                target_id,
            )
        # A dive that got a real slate on some *other* frame is identified;
        # only the ones left with no answer at all are noted.
        for dive_id in sorted(unidentified - set(slate_winners), key=str):
            if await self._catalog.note_unidentified_slate(
                tenant_id, dive_id, unidentified_slate_note()
            ):
                activity.logger.warning(
                    "species sync: dive=%s has an unidentifiable slate; noted",
                    dive_id,
                )

    # -- stage 6.1 ----------------------------------------------------------------------

    @activity.defn(name="update_dive_image_groups")
    async def update_dive_image_groups(
        self, target: SpeciesTarget
    ) -> UpdateDiveImageGroupsResult:
        """Materialise the dive's label-studio clusters from its species
        labels, all or nothing; refuse if it already has some."""
        facts = await self._catalog.species_grouping_facts(
            target.tenant_id, target.dive_id
        )
        skipped = UpdateDiveImageGroupsResult(
            skipped_already_grouped=True,
            new_clusters_created=0,
            species_labels_seen=0,
        )
        if facts.already_grouped:
            activity.logger.info(
                "dive=%s already has label-studio clusters; skipping "
                "(delete them first to re-group)",
                target.dive_id,
            )
            return skipped

        groups = regroup_by_species_labels(
            facts.prediction_clusters,
            select_species_label_per_image(facts.species_labels),
        )
        seen = len(facts.species_labels)
        if not groups:
            activity.logger.info(
                "dive=%s: no label-studio groups to create "
                "(prediction_clusters=%d, species_labels=%d)",
                target.dive_id,
                len(facts.prediction_clusters),
                seen,
            )
            return UpdateDiveImageGroupsResult(False, 0, seen)

        try:
            created = await self._catalog.persist_label_studio_clusters(
                target.tenant_id, target.dive_id, groups
            )
        except InvalidClusters as exc:
            # Final (ForeignCapture included): a retry re-reads the same labels
            # and regroups them into the same refused set.
            raise ApplicationError(
                f"refusing the label-studio groups for dive {target.dive_id}: {exc}",
                type="InvalidClusters",
                non_retryable=True,
            ) from exc
        if created is None:
            return skipped
        activity.logger.info(
            "dive=%s: created %d label-studio clusters from %d species labels",
            target.dive_id,
            created,
            seen,
        )
        return UpdateDiveImageGroupsResult(False, created, seen)
