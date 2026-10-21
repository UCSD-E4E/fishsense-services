"""Phone captures imported from a dataset by an operator (0058; the owner,
2026-10-21).

The first is CCFRP's summer 2025 FishSense Mobile data. Those phones ran an
app from before sync, so their captures exist only as the phones' database
exports on the NAS. The orchestrator's import (``ops/mobile_import``) reads
them and commits each through :func:`commit_capture`, the same path the app's
sync takes, so an import is a phone capture like any other: its files, its
manifest, and a phone mission (0057).

What differs is who it came from. Nobody synced it, so it has no uploader,
and ``imported_from`` names the dataset file and row instead. This catalog
only imports: a capture that names an uploader, or no source, is refused
before it reaches the database, which enforces the same rule.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import text

from fishsense_services_api.mobile_capture_store import (
    Committed,
    MobileCapture,
    commit_capture,
)
from fishsense_services_api.service_principal import ServicePrincipal

__all__ = ["MobileImportCatalog", "NotAnImport"]


class NotAnImport(ValueError):
    """A capture without a source, or with an uploader: not an import."""


class MobileImportCatalog(ServicePrincipal):
    """The orchestrator's import, as a member of the tenant it imports into."""

    async def import_capture(
        self, tenant_id: uuid.UUID, capture: MobileCapture
    ) -> Committed:
        if not capture.imported_from:
            raise NotAnImport("an imported capture names where it came from")
        if capture.uploaded_by is not None:
            raise NotAnImport("an imported capture has no uploader")
        async with self._tenant(tenant_id) as conn:
            return await commit_capture(conn, tenant_id, capture, [])

    async def imported(
        self, tenant_id: uuid.UUID, client_capture_uuids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Those of ``client_capture_uuids`` the tenant already holds."""
        async with self._tenant(tenant_id) as conn:
            rows = await conn.execute(
                text("""
                    SELECT client_capture_uuid FROM captures
                    WHERE tenant_id = :t AND client_capture_uuid = ANY(:ids)
                    """),
                {"t": tenant_id, "ids": list(client_capture_uuids)},
            )
            return set(rows.scalars())
