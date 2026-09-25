"""What ingest asks the database, as an interface.

Preflight and the later activities depend on this protocol, not on a database:
the unit tests (ported from v1, whose tests faked the API client the same way)
answer it from memory, and the real implementation answers it from Postgres,
tenant-scoped, as the orchestrator's service principal. That one is
`fishsense_services_api.ingest_store.IngestCatalog`.

**The orchestrator acts for a tenant only as a member of it** (PLAN.md §9.11):
`resolve_tenant` resolves a slug the way a person's request does, so an
unknown tenant and one the orchestrator doesn't serve look the same -- and
ingest into a tenant needs an explicit, auditable membership, never an RLS
bypass.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Protocol

from fishsense_services_api.ingest_store import (
    ContentOverlap,
    RegisteredCapture,
    ResolvedDevice,
)

__all__ = ["Catalog", "ContentOverlap", "RegisteredCapture", "ResolvedDevice"]


class Catalog(Protocol):
    async def resolve_tenant(self, slug: str) -> uuid.UUID | None:
        """The tenant's id, if the orchestrator is a member of it."""

    async def resolve_device(
        self, tenant_id: uuid.UUID, serial: str
    ) -> ResolvedDevice | None:
        """The tenant's device with this serial."""

    async def dive_by_path(self, tenant_id: uuid.UUID, path: str) -> uuid.UUID | None:
        """The tenant's dive at this NAS path."""

    async def dives_with_leaf(
        self, tenant_id: uuid.UUID, leaf: str
    ) -> list[tuple[uuid.UUID, str]]:
        """The tenant's dives whose folder name is ``leaf``: (id, path)."""

    async def slate_template(self, name: str) -> uuid.UUID | None:
        """The (global) slate template with this name."""

    # -- writes: the store's functions, keyword arguments passed through ------

    async def create_dive(self, tenant_id: uuid.UUID, **dive: Any) -> uuid.UUID:
        """Create or re-open the dive at its path, always at low."""

    async def register_capture(
        self, tenant_id: uuid.UUID, **capture: Any
    ) -> RegisteredCapture:
        """Upsert one frame on its path."""

    async def registered_paths(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> set[str]:
        """Paths already registered for the dive."""

    async def finalize_dive(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        *,
        priority: str,
        dived_at: datetime,
    ) -> None:
        """Open the commit flag."""

    async def content_overlap(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> list[ContentOverlap]:
        """How much of the dive already exists under other dives."""
