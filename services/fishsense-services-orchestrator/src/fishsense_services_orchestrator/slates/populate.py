"""The dive-slate Label Studio project: create it, and populate it.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/
(create_dive_slate_label_studio_project_activity.py,
populate_dive_slate_label_studio_project_activity.py). Stage 11: the slate
frames stage 9 drew become tasks for a labeler, who places the template's
reference points on the photo.

v1's rules, kept:

* the project is per dive, titled `{dive.name} #{number} - Dive Slate
  Labeling` (the number is v1's dive id for a migrated dive, so v1's title),
  created as a draft;
* targets are the dive's slate-marked frames with no completed slate label;
* **the JPEG gate**: a frame whose composite is not written yet is deferred
  to a later run, never given a task with a missing image (dive 84);
* the import dedupes by URL and reconciles rather than re-importing
  (`labels.populate.import_tasks_and_record_labels`); each task anchors one
  slate label row;
* the supersede pass dead-letters incomplete rows this project no longer
  owns, sparing a row only when it is in this project AND its frame was a
  candidate this run (dive 341 oscillated without both halves; a deferred
  JPEG still counts as a candidate);
* the project is published only when the import completed and the project
  holds tasks -- this run's, or rows it had before.

v2 changes:

* the project is found or created through `LabelProjects` (recorded, looked
  up first, v1's title search as the fallback);
* a task's image is the JPEG where the object store located it (v1's key for
  a migrated frame, whose task already holds that URL, so dedup finds it);
* the retired slate predictor's keypoint pre-annotations are not seeded: its
  rows were removed in prod when it was retired (2026-08-03), and v1's
  populate read an empty table. A task carries no predictions;
* **the slate detector queues frames too** (new): in a dive with no person's
  slate work, the store's candidates include the frames the presence
  detector calls slate (`fishsense_services_api.slate_store`), and each row
  records the prediction that queued it. The task is the same: a labeler
  places the reference points.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from temporalio import activity

from fishsense_services_api.slate_store import SlatePopulateCapture
from fishsense_services_contracts.object_store import SLATE_JPEG_FOLDER, ObjectRef
from fishsense_services_orchestrator.labels.label_studio import heartbeat_again
from fishsense_services_orchestrator.labels.populate import (
    TaskImage,
    build_task_data,
    import_tasks_and_record_labels,
    publish_label_studio_project,
)
from fishsense_services_orchestrator.object_store.contracts import StagingTarget
from fishsense_services_orchestrator.slates.contracts import PopulateSlateProject

__all__ = [
    "DIVE_SLATE_LABELING_CONFIG_XML",
    "DIVE_SLATE_PROJECT_TITLE_SUFFIX",
    "DiveSlateProjectActivities",
    "build_slate_task",
]

DIVE_SLATE_PROJECT_TITLE_SUFFIX = "Dive Slate Labeling"

# Labeling-config XML from the prod dive-slate project (v1's constant). Control
# names map 1:1 to the slate label's fields, and the sync reads them by name:
#   * `reference_points` (KeyPoints) -> reference_points
#   * `slate` (RectangleLabels)      -> slate_rectangle
#   * `skipped_points` (TextArea)    -> skipped_points
# The `upside_down` Choices control was removed 2026-07-31 -- never read
# downstream, and the slate geometry made it redundant.
DIVE_SLATE_LABELING_CONFIG_XML = """\
<View>

  <Image name="image" value="$image" zoom="true"/>

  <KeyPointLabels name="reference_points" toName="image">
    <Label value="Reference Point" background="red"/>
  </KeyPointLabels>

  <RectangleLabels name="slate" toName="image">
    <Label value="Slate" background="green" />
  </RectangleLabels>

  <Header value="Skipped points.  Use a comma separated list." />
  <TextArea name="skipped_points" toName="image"/>

</View>
"""


class _Catalog(Protocol):
    """See ``fishsense_services_api.slate_store.SlateCatalog``."""

    async def slate_populate_candidates(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> list[SlatePopulateCapture]: ...

    async def dive_has_slate_labels_in_project(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID, ls_project_id: int
    ) -> bool: ...

    async def record_slate_label(
        self,
        tenant_id: uuid.UUID,
        capture_id: uuid.UUID,
        *,
        ls_project_id: int,
        ls_task_id: int,
        image_url: str,
        slate_presence_prediction_id: uuid.UUID | None = None,
    ) -> None: ...

    async def supersede_stale_slate_labels(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        *,
        ls_project_id: int,
        keep_capture_ids: list[uuid.UUID],
    ) -> int: ...


def build_slate_task(capture: SlatePopulateCapture, jpeg: ObjectRef) -> dict:
    """One Label Studio task: the composite, as located, and no predictions."""
    return {
        "data": build_task_data(
            TaskImage(
                number=capture.number, image=jpeg, captured_at=capture.captured_at
            )
        ),
        "predictions": [],
        "annotations": [],
    }


class DiveSlateProjectActivities:
    def __init__(self, *, catalog: _Catalog, label_projects, label_studio, store):
        self._catalog = catalog
        self._projects = label_projects
        self._ls = label_studio
        self._store = store

    @activity.defn(name="create_dive_slate_label_studio_project")
    async def create_dive_slate_label_studio_project(
        self, target: StagingTarget
    ) -> int:
        """The dive's slate-labeling project, found or created; its id."""
        project_id = await self._projects.ensure_dive_project(
            target.tenant_id,
            target.dive_id,
            "slate",
            suffix=DIVE_SLATE_PROJECT_TITLE_SUFFIX,
            labeling_config_xml=DIVE_SLATE_LABELING_CONFIG_XML,
        )
        activity.logger.info(
            "dive-slate LS project dive=%s project_id=%d", target.dive_id, project_id
        )
        return project_id

    async def _located(self, tenant_id, candidates):
        """The candidates whose composite is written, with where it is."""
        present = []
        for capture in candidates:
            jpeg = await self._store.locate_processed_jpeg(
                tenant_id, SLATE_JPEG_FOLDER, capture.checksum, from_v1=capture.from_v1
            )
            if jpeg is None:
                activity.logger.info(
                    "slate JPEG not yet written for capture %s (checksum=%s); "
                    "deferring to a later populate run",
                    capture.capture_id,
                    capture.checksum,
                )
            else:
                present.append((capture, jpeg))
            heartbeat_again()
        return present

    @activity.defn(name="populate_dive_slate_label_studio_project")
    async def populate_dive_slate_label_studio_project(
        self, payload: PopulateSlateProject
    ) -> int:
        """Push slate tasks for the dive into its project; the rows recorded."""
        tenant_id, dive_id = payload.tenant_id, payload.dive_id
        project_id = payload.ls_project_id

        candidates = await self._catalog.slate_populate_candidates(tenant_id, dive_id)
        had_rows = await self._catalog.dive_has_slate_labels_in_project(
            tenant_id, dive_id, project_id
        )
        present = await self._located(tenant_id, candidates)

        async def record(item, task_id: int) -> None:
            capture, jpeg = item
            await self._catalog.record_slate_label(
                tenant_id,
                capture.capture_id,
                ls_project_id=project_id,
                ls_task_id=task_id,
                image_url=jpeg.uri,
                slate_presence_prediction_id=capture.slate_presence_prediction_id,
            )

        result = await import_tasks_and_record_labels(
            self._ls,
            project_id=project_id,
            tasks=[build_slate_task(capture, jpeg) for capture, jpeg in present],
            record_label=record,
            items=present,
        )
        if not present:
            activity.logger.info(
                "dive %s has no slate frames ready for labeling; nothing imported",
                dive_id,
            )

        # Exempt on CANDIDATES, not on what survived the JPEG gate: a deferred
        # JPEG means "not yet", not "no longer wanted".
        superseded = await self._catalog.supersede_stale_slate_labels(
            tenant_id,
            dive_id,
            ls_project_id=project_id,
            keep_capture_ids=[capture.capture_id for capture in candidates],
        )
        if superseded:
            activity.logger.info(
                "superseded %d stale slate label(s) of dive %s", superseded, dive_id
            )

        if result.complete and (result.recorded > 0 or had_rows):
            await publish_label_studio_project(self._ls, project_id)
        return result.recorded
