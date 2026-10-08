"""Point captures whose files moved on the NAS at where they went.

New in v2 (2026-10-07). Frames of three dives were moved into subfolders of
their dive folders after ingest (219, 237, 249), so their rows named files
that no longer existed and every stage that staged one failed. Dive 237 was
repaired by hand, from a laptop over a FUSE mount; this is that repair as an
ops workflow, in the slot, with the NAS doing the hashing (`NasClient.md5`) so
nothing crosses the network but listings and 32-character digests.

The rules:

* **a dry run unless told otherwise**: `apply=False` reports what would change
  and changes nothing;
* a frame is re-pointed only when its file is in **exactly one** subfolder of
  its own folder (one level down, which is how the moves were made), the
  NAS's md5 of that file **is the row's checksum**, and **no other capture**
  holds the path. Everything else is reported by reason, for a person;
* the NAS hashes only frames missing from their path, and each folder is
  listed once -- and its subfolders only if a frame of it is missing;
* the re-point is conditional on the row's path and checksum
  (`capture_path_store.repoint_capture`), so a row changed meanwhile is left
  as it is now. Re-running after an apply finds the moved rows at their paths;
* a folder that is gone is a finding; any other NAS error is an outage and
  propagates for the retry policy.
"""

from __future__ import annotations

import asyncio
import posixpath
import uuid
from collections import defaultdict
from collections.abc import Callable
from typing import Protocol

from synology_filestation import FileStationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.capture_path_store import PathCapture
from fishsense_services_orchestrator.ingest.nas import NasClient
from fishsense_services_orchestrator.ingest.nas_errors import is_nas_not_found
from fishsense_services_orchestrator.ingest.nas_frames import (
    NasSettings,
    build_nas_client,
    resolve_nas_path,
)
from fishsense_services_orchestrator.ops.checksums.activities import (
    DIVE_NOT_FOUND_TYPE,
)
from fishsense_services_orchestrator.ops.paths.contracts import (
    MovedFrame,
    PathFinding,
    PathRepairReport,
)

__all__ = ["PathRepairActivities", "PathRepairCatalog"]


class PathRepairCatalog(Protocol):
    """See ``fishsense_services_api.capture_path_store.CapturePathCatalog``."""

    async def member_tenants(self) -> list[uuid.UUID]: ...

    async def dive_by_number(
        self, tenant_id: uuid.UUID, number: int
    ) -> uuid.UUID | None: ...

    async def captures_with_paths(
        self, tenant_id: uuid.UUID, dive_id: uuid.UUID
    ) -> list[PathCapture]: ...

    async def path_holder(
        self, tenant_id: uuid.UUID, path: str
    ) -> uuid.UUID | None: ...

    async def repoint_capture(
        self,
        tenant_id: uuid.UUID,
        capture_id: uuid.UUID,
        *,
        old_path: str,
        new_path: str,
        checksum: str,
    ) -> bool: ...


class _Folder:
    """One folder of the dive, as the NAS has it: the files in it, and (only
    once asked) where each name appears in its subfolders."""

    def __init__(self, nas: NasClient, folder: str, settings: NasSettings) -> None:
        self._nas = nas
        self._settings = settings
        self.folder = folder
        self.absolute = resolve_nas_path(folder, settings)
        self.gone = False
        self.files: set[str] = set()
        self._subfolders: list[str] = []
        self._moved: dict[str, list[str]] | None = None

    def list(self) -> None:
        try:
            entries = self._nas.list_dir(folder_path=self.absolute)
        except FileStationError as exc:
            if is_nas_not_found(exc):
                self.gone = True
                return
            raise
        self.files = {e.name for e in entries if not e.is_dir}
        self._subfolders = sorted(e.name for e in entries if e.is_dir)

    def candidates(self, name: str) -> list[str]:
        """Each subfolder-relative path holding a file called ``name``."""
        if self._moved is None:
            self._moved = defaultdict(list)
            for sub in self._subfolders:
                for entry in self._nas.list_dir(folder_path=f"{self.absolute}/{sub}"):
                    if not entry.is_dir:
                        self._moved[entry.name].append(f"{sub}/{entry.name}")
        return sorted(self._moved.get(name, []))


class PathRepairActivities:
    def __init__(
        self,
        *,
        catalog: PathRepairCatalog,
        nas_settings: NasSettings,
        nas_client_factory: Callable[[], NasClient] | None = None,
    ) -> None:
        self._catalog = catalog
        self._nas_settings = nas_settings
        self._nas_client_factory = nas_client_factory or (
            lambda: build_nas_client(nas_settings)
        )

    async def _find_dive(self, dive_number: int) -> tuple[uuid.UUID, uuid.UUID]:
        for tenant_id in await self._catalog.member_tenants():
            dive_id = await self._catalog.dive_by_number(tenant_id, dive_number)
            if dive_id is not None:
                return tenant_id, dive_id
        raise ApplicationError(
            f"no dive numbered {dive_number} in any tenant the orchestrator serves",
            type=DIVE_NOT_FOUND_TYPE,
            non_retryable=True,
        )

    @activity.defn(name="repair_moved_capture_paths")
    async def repair_moved_capture_paths(
        self, dive_number: int, apply: bool = False
    ) -> PathRepairReport:
        """Find the dive's frames that are not at their paths, and (with
        ``apply``) point each verified one at where it went."""
        tenant_id, dive_id = await self._find_dive(dive_number)
        captures = await self._catalog.captures_with_paths(tenant_id, dive_id)
        report = PathRepairReport(
            dive_number=dive_number, applied=apply, frames=len(captures)
        )
        nas = await asyncio.to_thread(self._nas_client_factory)

        folders: dict[str, _Folder] = {}
        for capture in captures:
            folder_path, name = posixpath.split(capture.source_path)
            folder = folders.get(folder_path)
            if folder is None:
                folder = folders[folder_path] = _Folder(
                    nas, folder_path, self._nas_settings
                )
                await asyncio.to_thread(folder.list)
            await self._one(report, tenant_id, nas, folder, capture, name)
            activity.heartbeat()

        activity.logger.info(
            "path repair dive=%d applied=%s frames=%d at_path=%d repaired=%d "
            "would_repair=%d left_alone=%d",
            dive_number,
            apply,
            report.frames,
            report.at_path,
            len(report.repaired),
            len(report.would_repair),
            report.left_alone,
        )
        return report

    async def _one(
        self,
        report: PathRepairReport,
        tenant_id: uuid.UUID,
        nas: NasClient,
        folder: _Folder,
        capture: PathCapture,
        name: str,
    ) -> None:
        def finding(candidates=(), detail=None) -> PathFinding:
            return PathFinding(
                capture_number=capture.number,
                path=capture.source_path,
                canonical=capture.is_canonical,
                candidates=[f"{folder.folder}/{c}" for c in candidates],
                detail=detail,
            )

        if folder.gone:
            report.not_found.append(finding(detail="its folder is not on the NAS"))
            return
        if name in folder.files:
            report.at_path += 1
            return

        candidates = await asyncio.to_thread(folder.candidates, name)
        if not candidates:
            report.not_found.append(finding(detail="in no subfolder of its folder"))
            return
        if len(candidates) > 1:
            report.ambiguous.append(finding(candidates))
            return
        if capture.checksum_algorithm != "md5":
            report.unsupported.append(
                finding(candidates, detail=f"{capture.checksum_algorithm} row")
            )
            return

        (candidate,) = candidates
        computed = await asyncio.to_thread(
            nas.md5, file_path=f"{folder.absolute}/{candidate}"
        )
        if computed != capture.checksum:
            report.checksum_mismatch.append(
                finding(
                    candidates,
                    detail=f"nas md5 {computed}, row {capture.checksum}",
                )
            )
            return

        new_path = f"{folder.folder}/{candidate}"
        holder = await self._catalog.path_holder(tenant_id, new_path)
        if holder is not None and holder != capture.id:
            report.path_taken.append(finding(candidates, detail=f"held by {holder}"))
            return

        moved = MovedFrame(
            capture_number=capture.number,
            old_path=capture.source_path,
            new_path=new_path,
            canonical=capture.is_canonical,
        )
        if not report.applied:
            report.would_repair.append(moved)
            return
        if await self._catalog.repoint_capture(
            tenant_id,
            capture.id,
            old_path=capture.source_path,
            new_path=new_path,
            checksum=capture.checksum,
        ):
            report.repaired.append(moved)
        else:
            report.changed_since_checked.append(finding(candidates))
