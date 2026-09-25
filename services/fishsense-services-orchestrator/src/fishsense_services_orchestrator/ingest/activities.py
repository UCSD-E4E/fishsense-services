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
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import List

from synology_filestation import DSMError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_orchestrator.ingest.catalog import Catalog, ResolvedDevice
from fishsense_services_orchestrator.ingest.contracts import (
    DuplicateOverlap,
    IngestDiveRequest,
    IngestPreflight,
    IngestReport,
    PreflightImage,
    RejectedImage,
    SubfolderReport,
)
from fishsense_services_orchestrator.ingest.exif import read_exif
from fishsense_services_orchestrator.ingest.nas import NasClient, NasEntry
from fishsense_services_orchestrator.ingest.nas_errors import (
    raise_if_permanent_dsm_error,
)
from fishsense_services_orchestrator.ingest.nas_frames import (
    EXIF_HEADER_BYTES,
    NasSettings,
    build_nas_client,
    parse_taken_datetime,
)

# Case-insensitive: Olympus writes `.ORF`, but operators and copy tools produce
# `.orf` and occasionally `.Orf`. A case-sensitive match would ingest a partial
# dive that no later step can detect as partial, because the missing frames
# were never listed in the first place.
_RAW_SUFFIX = ".orf"

# Kept from v1's varchar(255) paths: the research repos and the NAS itself still
# work to it. Checked against the *stored*, share-relative form.
MAX_PATH_LENGTH = 255

# `type` on finalize's non-retryable refusal, so a retry policy can name it.
INCOMPLETE_INGEST_TYPE = "IncompleteIngest"

__all__ = [
    "EXIF_HEADER_BYTES",
    "INCOMPLETE_INGEST_TYPE",
    "IngestTotals",
    "MAX_PATH_LENGTH",
    "DiveFolderListing",
    "IngestActivities",
    "resolve_nas_folder",
    "stored_path",
]


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


@dataclass
class IngestTotals:
    """What the scan batches added up to, accumulated by the workflow."""

    total: int = 0
    registered: int = 0
    skipped_existing: int = 0
    rejected: List[RejectedImage] = field(default_factory=list)
    max_taken_datetime: datetime | None = None


def leaf_name(path: str) -> str:
    """The folder's own name, which is what a dive is called by default. The
    name feeds the per-dive Label Studio project title, so leaving it unset
    gives labelers a project called just its id."""
    return path.rstrip("/").rsplit("/", 1)[-1]


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
        catalog: Catalog | None = None,
    ) -> None:
        self._nas_settings = nas_settings
        self._catalog = catalog
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

    @activity.defn(name="preflight")
    async def preflight(
        self, request: IngestDiveRequest, listing: DiveFolderListing
    ) -> IngestPreflight:
        """Decide whether the folder can become a dive, and say so completely:
        **every problem at once, never first-wins.** Writes nothing."""
        # pylint: disable=too-many-locals,too-many-branches
        # Preflight is a checklist: the branch count IS the feature. Splitting it
        # into per-check helpers would scatter the "collect, never raise" contract
        # that makes the all-at-once report work.
        catalog = self._catalog
        if catalog is None:
            raise RuntimeError("preflight needs a catalog")
        errors: List[str] = []
        warnings: List[str] = []

        tenant_id = await catalog.resolve_tenant(request.tenant)
        if tenant_id is None:
            errors.append(
                f"Tenant {request.tenant!r} is unknown, or the orchestrator is not "
                "a member of it. Ingest acts for a tenant only as its member."
            )

        errors.extend(_check_calibration_intent(request))
        source_dive_id = None
        if request.calibration_source_path and tenant_id is not None:
            source_dive_id = await catalog.dive_by_path(
                tenant_id, request.calibration_source_path
            )
            if source_dive_id is None:
                errors.append(
                    f"No dive at {request.calibration_source_path!r} in tenant "
                    f"{request.tenant!r} to borrow calibration from."
                )

        slate_template_id = None
        if request.slate_template:
            slate_template_id = await catalog.slate_template(request.slate_template)
            if slate_template_id is None:
                errors.append(f"No slate template named {request.slate_template!r}.")

        if not listing.files:
            errors.append(
                f"No .ORF frames directly inside {listing.folder_path}. Ingest is "
                "non-recursive -- a dive is exactly one directory."
            )
        for subfolder in listing.subfolders:
            warnings.append(
                f"{subfolder.path} contains {subfolder.orf_count} .ORF files. "
                "Under the existing convention that is a separate dive -- submit it "
                "as its own request; it is not included here."
            )

        nas = self._nas_client_factory()
        images: List[PreflightImage] = []
        serials: set[str] = set()
        artists: set[str] = set()
        for entry in listing.files:
            activity.heartbeat(entry.path)
            path = stored_path(entry.path, self._nas_settings)
            if len(path) > MAX_PATH_LENGTH:
                errors.append(
                    f"Path exceeds {MAX_PATH_LENGTH} characters ({len(path)}): {path}"
                )
                continue
            exif = read_exif(await _read_header(nas, entry.path))
            taken = parse_taken_datetime(exif.date_time)
            if taken is None:
                errors.append(
                    f"No readable EXIF timestamp in {path}. Stage-1 clustering is "
                    "pure timestamp maths, so the frame cannot be ingested with a "
                    "defaulted value."
                )
                continue
            if exif.date_time_is_fallback:
                warnings.append(
                    f"{path} has no DateTime (0x0132); fell back to "
                    "DateTimeOriginal (0x9003)."
                )
            if exif.serial_number:
                serials.add(exif.serial_number)
            if exif.artist:
                artists.add(exif.artist)
            images.append(
                PreflightImage(
                    path=path,
                    size=entry.size,
                    taken_datetime=taken,
                    exif_offset=exif.offset_time,
                    serial_number=exif.serial_number,
                    artist=exif.artist,
                )
            )

        device = None
        if tenant_id is not None:
            device = await _resolve_device(
                catalog, tenant_id, request, serials, artists, errors, warnings
            )
        if device is not None and not device.has_camera_calibration:
            errors.append(
                f"Device {device.name or device.device_id} has no intrinsics (no "
                "camera calibration). Stage 14 cannot measure this dive until "
                "they exist."
            )

        # Layer 1 duplicate detection: leaf-name collision. Catches the real prod
        # case -- dives 64 and 66 are both `082929_FishModels_FSL07`. Content-based
        # containment needs checksums, so it runs after the scan.
        if tenant_id is not None:
            leaf = listing.folder_path.rstrip("/").rsplit("/", 1)[-1]
            for dive_id, dive_path in await catalog.dives_with_leaf(tenant_id, leaf):
                warnings.append(
                    f"Dive {dive_id} has the same folder name ({leaf!r}) at "
                    f"{dive_path}. Dive names are not unique; this may be a "
                    "re-ingest."
                )

        activity.logger.info(
            "preflight path=%s frames=%d errors=%d warnings=%d",
            listing.folder_path,
            len(images),
            len(errors),
            len(warnings),
        )
        return IngestPreflight(
            dive_path=listing.folder_path,
            tenant_id=tenant_id,
            resolved_calibration_source_dive_id=source_dive_id,
            resolved_slate_template_id=slate_template_id,
            images=images,
            subfolders=list(listing.subfolders),
            resolved_device_id=device.device_id if device else None,
            resolved_device_name=device.name if device else None,
            total_bytes=sum(e.size for e in listing.files),
            errors=errors,
            warnings=warnings,
        )

    @activity.defn(name="create_dive")
    async def create_dive(
        self, request: IngestDiveRequest, preflight: IngestPreflight
    ) -> uuid.UUID:
        """Create the dive, always at **low** -- half of a two-phase commit.

        Every hourly cohort selects on high, so a dive created high before its
        images exist would be picked up mid-ingest and processed against a
        partial set. Priority is the commit flag, and this is the half that
        keeps it closed; v2's store takes no priority here at all. The store
        upserts on the path, so re-running an interrupted ingest finds the same
        dive rather than making a second one.
        """
        if preflight.errors or preflight.tenant_id is None:
            raise ApplicationError(
                "refusing to create a dive from a failed preflight: "
                + "; ".join(preflight.errors[:3] or ["no tenant resolved"]),
                non_retryable=True,
            )
        # `dived_at` is NOT NULL and no frame has been hashed yet, so seed it
        # from preflight's headers. Finalize replaces it with the scan's max,
        # which read every frame rather than a 1 MB prefix.
        stamps = [i.taken_datetime for i in preflight.images if i.taken_datetime]
        if not stamps:
            raise ApplicationError(
                "preflight produced no usable timestamps; refusing to create a "
                "dive with a fabricated datetime",
                non_retryable=True,
            )

        dive_id = await self._catalog.create_dive(
            preflight.tenant_id,
            source_path=request.dive_path,
            name=request.dive_name or leaf_name(request.dive_path),
            dived_at=max(stamps),
            device_id=preflight.resolved_device_id,
            slate_template_id=preflight.resolved_slate_template_id,
            # NULL means "self-calibrates". A link on a self-calibrating dive
            # would be a lie the resolver happens to ignore (own wins).
            calibration_source_dive_id=preflight.resolved_calibration_source_dive_id,
            flip_dive_slate=request.flip_dive_slate,
        )
        activity.logger.info(
            "created dive id=%s path=%s at low (commit flag closed)",
            dive_id,
            request.dive_path,
        )
        return dive_id

    @activity.defn(name="finalize_dive")
    async def finalize_dive(
        self,
        tenant_id: uuid.UUID,
        dive_id: uuid.UUID,
        request: IngestDiveRequest,
        totals: IngestTotals,
    ) -> IngestReport:
        """Verify the set is complete, then flip the dive to its real priority.

        Two refusals, both non-retryable because neither is transient -- the
        fix is an operator reading the report:

        * **any rejection**: a frame with no readable timestamp was refused
          rather than given a fabricated one, so the dive is missing an image;
        * **registered + skipped != total**: a frame was neither written nor
          recognised as already present -- silence rather than a reported
          failure, which is worse.

        Content overlap is computed here because it needs every frame's
        checksum, which only exists once the scan has written the rows. It is
        **reported, never blocking**: re-ingesting the same frames under a
        second path is legitimate (prod dives 64 and 66), and the duplicates
        land non-canonical.
        """
        accounted = totals.registered + totals.skipped_existing
        if totals.rejected:
            raise ApplicationError(
                f"refusing to promote dive {dive_id}: {len(totals.rejected)} "
                "frame(s) rejected -- "
                f"{'; '.join(r.reason for r in totals.rejected[:3])}. The dive "
                "stays at low so no pipeline stage picks up a partial set.",
                type=INCOMPLETE_INGEST_TYPE,
                non_retryable=True,
            )
        if accounted != totals.total:
            raise ApplicationError(
                f"refusing to promote dive {dive_id}: {accounted} of "
                f"{totals.total} frames accounted for. A frame was neither "
                "written nor recognised as already present, which is silence "
                "rather than a reported failure.",
                type=INCOMPLETE_INGEST_TYPE,
                non_retryable=True,
            )

        overlap = [
            DuplicateOverlap(
                dive_id=o.dive_id,
                dive_path=o.dive_path,
                shared_images=o.shared_images,
                containment=o.containment,
            )
            for o in await self._catalog.content_overlap(tenant_id, dive_id)
        ]
        # THE COMMIT FLAG. The scan read every frame in full, so its max is the
        # dive's datetime -- how every existing dive's was derived.
        await self._catalog.finalize_dive(
            tenant_id,
            dive_id,
            priority=request.priority,
            dived_at=totals.max_taken_datetime,
        )

        for item in overlap:
            activity.logger.warning(
                "dive %s shares %d/%d frames with dive %s (containment %.2f); "
                "those images are non-canonical",
                dive_id,
                item.shared_images,
                totals.total,
                item.dive_id,
                item.containment,
            )
        activity.logger.info(
            "committed dive %s at %s: registered=%d skipped=%d",
            dive_id,
            request.priority,
            totals.registered,
            totals.skipped_existing,
        )
        return IngestReport(
            dive_path=request.dive_path,
            dive_id=dive_id,
            total=totals.total,
            registered=totals.registered,
            skipped_existing=totals.skipped_existing,
            rejected=[],
            dive_datetime=totals.max_taken_datetime,
            committed=True,
            duplicate_overlap=overlap,
        )


def stored_path(absolute_path: str, settings: NasSettings) -> str:
    """Strip the NAS raw root back off, giving the form the database stores.

    A path **outside** the root keeps its leading slash, because that is the
    only form that survives the round trip through `resolve_nas_path` -- a
    relative path that is not under the root resolves to root + itself, a place
    that does not exist (FileStation: a 502 per frame, forever). The 2025-01-17
    pool test is the live case: same share, outside `REEF/data`.
    """
    root = settings.raw_root_path.rstrip("/") + "/"
    if absolute_path.startswith(root):
        return absolute_path[len(root) :]
    return absolute_path


async def _read_header(nas: NasClient, file_path: str) -> bytes:
    """One ranged read. No inner retry -- Temporal's bounded policy owns
    backoff, and an inner loop under it is what tripped the NAS auto-block."""
    try:
        return await asyncio.to_thread(
            nas.download_range, file_path=file_path, offset=0, length=EXIF_HEADER_BYTES
        )
    except DSMError as exc:
        raise_if_permanent_dsm_error(exc, context=file_path)
        raise


def _check_calibration_intent(request: IngestDiveRequest) -> List[str]:
    """Exactly one of the two must be given. Both is contradictory: own-wins
    would silently ignore the link. Neither leaves a dive that can never be
    measured and never says why."""
    borrows = request.calibration_source_path is not None
    if request.self_calibrates and borrows:
        return [
            "Contradictory calibration intent: self_calibrates=True and "
            f"calibration_source_path={request.calibration_source_path!r}. A dive "
            "with its own slate always self-calibrates, so the link would be "
            "ignored -- pass exactly one."
        ]
    if not request.self_calibrates and not borrows:
        return [
            "No calibration intent given. Pass self_calibrates=True if this dive "
            "has its own slate frames, or calibration_source_path=<dive path> to "
            "borrow a sibling's calibration. Without one, stage 14 can never "
            "measure this dive."
        ]
    return []


async def _resolve_device(
    catalog: Catalog,
    tenant_id: uuid.UUID,
    request: IngestDiveRequest,
    serials: set[str],
    artists: set[str],
    errors: List[str],
    warnings: List[str],
) -> ResolvedDevice | None:
    """Resolve the tenant's device, or None (with the reason in `errors`)."""
    if len(serials) > 1:
        errors.append(
            "Frames span more than one camera serial "
            f"({', '.join(sorted(serials))}). One folder is one rig -- split the "
            "folder and submit each dive separately."
        )
        return None
    if request.device_serial is not None:
        match = await catalog.resolve_device(tenant_id, request.device_serial)
        if match is None:
            errors.append(f"No device with serial {request.device_serial!r}.")
        return match
    if not serials:
        errors.append(
            "No camera serial found in any frame's Olympus MakerNote, and no "
            "device_serial override was given."
        )
        return None
    serial = next(iter(serials))
    match = await catalog.resolve_device(tenant_id, serial)
    if match is None:
        errors.append(
            f"Camera serial {serial} matches no device. Add the device (with its "
            "intrinsics) before ingesting. Deliberately not falling back to the "
            "EXIF Artist tag -- a free-text rig label would bind the wrong "
            "intrinsics and stage 14 would report confident wrong lengths."
        )
        return None
    # The serial is authoritative; a disagreeing Artist means a mislabelled
    # device name or a re-housed body. Nothing else would ever notice.
    for artist in sorted(a for a in artists if a and a != match.name):
        warnings.append(
            f"EXIF Artist {artist!r} disagrees with the resolved device's name "
            f"{match.name!r} (serial {serial})."
        )
    return match
