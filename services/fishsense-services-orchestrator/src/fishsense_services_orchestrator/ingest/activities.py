"""Ingest activities, as methods of `IngestActivities`.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/ (list_dive_folder_activity.py, ...).
Behaviour is v1's. v2 change: activities are methods of a class that is *given*
its dependencies -- NAS settings, a NAS client factory -- instead of reading
global settings and building clients itself. Temporal registers the bound
methods; tests pass fakes instead of monkeypatching module globals.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import List

from synology_filestation import DSMError
from temporalio import activity

from fishsense_services_orchestrator.ingest.contracts import (
    IngestDiveRequest,
    SubfolderReport,
)
from fishsense_services_orchestrator.ingest.nas import NasClient, NasEntry
from fishsense_services_orchestrator.ingest.nas_errors import (
    raise_if_permanent_dsm_error,
)
from fishsense_services_orchestrator.ingest.nas_frames import (
    NasSettings,
    build_nas_client,
)

# Case-insensitive: Olympus writes `.ORF`, but operators and copy tools produce
# `.orf` and occasionally `.Orf`. A case-sensitive match would ingest a partial
# dive that no later step can detect as partial, because the missing frames
# were never listed in the first place.
_RAW_SUFFIX = ".orf"

__all__ = ["DiveFolderListing", "IngestActivities", "resolve_nas_folder"]


@dataclass
class DiveFolderListing:
    """What is in the folder, before anything has been read or written."""

    #: The absolute NAS path actually listed, after root resolution.
    folder_path: str
    #: `.ORF` files directly inside `folder_path`, name-sorted.
    files: List[NasEntry] = field(default_factory=list)
    #: Immediate subdirectories that hold `.ORF`s -- separate dives, reported
    #: for the operator to submit themselves.
    subfolders: List[SubfolderReport] = field(default_factory=list)


def resolve_nas_folder(relative_path: str, settings: NasSettings) -> str:
    """Join the NAS raw root with a share-relative request path.

    The DB stores paths share-relative while FileStation needs them absolute,
    and worth getting right because FileStation surfaces an unresolved path as a
    502 rather than a 404. An already-absolute path passes through, so an
    operator pasting a full NAS path isn't double-prefixed.
    """
    if relative_path.startswith("/"):
        return relative_path.rstrip("/")
    root = settings.raw_root_path.rstrip("/")
    return f"{root}/{relative_path.strip('/')}"


def _is_raw(entry: NasEntry) -> bool:
    return not entry.is_dir and entry.name.lower().endswith(_RAW_SUFFIX)


async def _list(client: NasClient, folder_path: str) -> List[NasEntry]:
    """One `list_dir`, with permanent errors classified.

    No inner retry loop: the bounded jittered Temporal policy owns backoff, and
    an inner loop underneath it is what produced the download storm that
    tripped the NAS auto-block.
    """
    try:
        return await asyncio.to_thread(client.list_dir, folder_path=folder_path)
    except DSMError as exc:
        raise_if_permanent_dsm_error(exc, context=folder_path)
        raise


class IngestActivities:
    """The ingest activities and what they depend on."""

    def __init__(
        self,
        *,
        nas_settings: NasSettings,
        nas_client_factory: Callable[[], NasClient] | None = None,
    ) -> None:
        self._nas_settings = nas_settings
        self._nas_client_factory = nas_client_factory or (
            lambda: build_nas_client(nas_settings)
        )

    @activity.defn(name="list_dive_folder")
    async def list_dive_folder(self, request: IngestDiveRequest) -> DiveFolderListing:
        client = self._nas_client_factory()
        folder_path = resolve_nas_folder(request.dive_path, self._nas_settings)

        entries = await _list(client, folder_path)
        # Name-sorted, because batching and heartbeat-resume both index into this
        # list. DSM promises no order, so a retry that saw a different one would
        # re-download frames it had already registered and skip ones it hadn't.
        files = sorted((e for e in entries if _is_raw(e)), key=lambda e: e.name)

        subfolders: List[SubfolderReport] = []
        for entry in sorted((e for e in entries if e.is_dir), key=lambda e: e.name):
            # Exactly one level down: enough to count a rollover folder's frames,
            # and no more. A full walk would turn a path mistyped near the share
            # root into an enumeration of the entire NAS, over a download backend
            # that already falls over under load.
            children = await _list(client, entry.path)
            orf_count = sum(1 for child in children if _is_raw(child))
            if orf_count:
                subfolders.append(SubfolderReport(path=entry.path, orf_count=orf_count))

        activity.logger.info(
            "listed dive folder path=%s frames=%d subfolders=%d",
            folder_path,
            len(files),
            len(subfolders),
        )
        return DiveFolderListing(
            folder_path=folder_path, files=files, subfolders=subfolders
        )
