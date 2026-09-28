"""What the object-store activities ask the database, as an interface.

The real implementation is `fishsense_services_api.raw_staging_store.
RawStagingCatalog`, tenant-scoped, acting as the orchestrator's service
principal; the unit tests answer it from memory (v1's faked the API client the
same way).
"""

from __future__ import annotations

import uuid
from typing import Protocol

from fishsense_services_api.raw_staging_store import CaptureChecksum, StagingCapture

__all__ = ["CaptureChecksum", "RawStagingCatalog", "StagingCapture"]


class RawStagingCatalog(Protocol):
    async def captures_to_stage(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> list[StagingCapture]:
        """The dive's canonical captures, with NAS paths and checksums."""

    async def checksums_to_clean(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> list[str]:
        """The checksums whose scratch this dive may delete."""

    async def capture_checksum(
        self, tenant_id: uuid.UUID, capture_id: uuid.UUID
    ) -> CaptureChecksum | None:
        """The capture's checksum, and whether it came from v1."""
