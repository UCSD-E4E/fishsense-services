"""Is a capture's processed JPEG written, and where?

Ported from fishsense-lite@77e8f8e5 `ObjectStoreClient.has_processed_jpeg`
(services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/
object_store.py): a HEAD in the labels bucket, which v1's decoupled populate
used to gate task import -- a task for a frame whose JPEG isn't written would
drop the dive out of the preprocess cohort with a broken image.

v2 changes:

* it takes a capture: the catalog gives its checksum and whether it came from
  v1, and the legacy key resolver looks at the tenant's key, then -- for a
  migrated frame only -- where v1 wrote it (`ObjectLayout.
  processed_jpeg_candidates`);
* it returns the ``ObjectRef`` found (None: not written yet), because populate
  needs the location for the task, not only the yes/no;
* an unknown capture or stage folder is refused, non-retryably: "not written
  yet" would have a caller wait for something that can never appear.
"""

from __future__ import annotations

from typing import Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_orchestrator.object_store.catalog import RawStagingCatalog
from fishsense_services_orchestrator.object_store.contracts import (
    ProcessedJpegRequest,
)
from fishsense_services_orchestrator.object_store.layout import JPEG_FOLDERS
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)

__all__ = ["ProcessedJpegActivities"]


class ProcessedJpegActivities:
    def __init__(
        self, *, catalog: RawStagingCatalog, store: OrchestratorObjectStore
    ) -> None:
        self._catalog = catalog
        self._store = store

    @activity.defn(name="locate_processed_jpeg")
    async def locate_processed_jpeg(
        self, request: ProcessedJpegRequest
    ) -> Optional[ObjectRef]:
        if request.folder not in JPEG_FOLDERS:
            raise ApplicationError(
                f"no stage writes JPEGs to {request.folder!r}; one of {JPEG_FOLDERS}",
                type="UnknownJpegFolder",
                non_retryable=True,
            )
        capture = await self._catalog.capture_checksum(
            request.tenant_id, request.capture_id
        )
        if capture is None:
            raise ApplicationError(
                f"tenant {request.tenant_id} has no capture {request.capture_id}",
                type="UnknownCapture",
                non_retryable=True,
            )
        return await self._store.locate_processed_jpeg(
            request.tenant_id,
            request.folder,
            capture.checksum,
            from_v1=capture.from_v1,
        )
