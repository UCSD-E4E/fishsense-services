"""Import an FSM export's captures into a tenant: plan, then apply that plan.

New in v2 (2026-10-21; the owner: CCFRP's summer 2025 FishSense Mobile data
into ccfrp-ucsd). The rules for which rows are captures are in ``plan``. Here:

* the scan reads the NAS only: listings, each folder's ``metadata.json``, and a
  copy of each folder's database, read in a temp dir and thrown away;
* a phone needs its intrinsics, or nothing is planned. The export has none, so
  the request gives them, with their source, which every capture keeps in its
  manifest;
* an apply plans again and refuses a plan that is not the one reviewed;
* each capture's photo, depth and confidence go into the tenant's bucket by a
  presigned PUT signed with their SHA-256, as the app's sync does, then the
  capture is committed as an import (``MobileImportCatalog``): no uploader, and
  ``imported_from`` naming the database row;
* a capture the tenant already holds is skipped, so a retried apply is safe.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import posixpath
import tempfile
import urllib.request
from collections import Counter
from typing import Any, Callable, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_api.mobile_api import ROLES
from fishsense_services_api.mobile_capture_store import (
    CaptureConflict,
    CaptureFile,
    MobileCapture,
)
from fishsense_services_orchestrator.ingest.nas_frames import (
    NasSettings,
    build_nas_client,
)
from fishsense_services_orchestrator.object_store.tenant_buckets import tenant_objects
from fishsense_services_orchestrator.ops.mobile_import.fsm_export import (
    read_blobs,
    read_rows,
)
from fishsense_services_orchestrator.ops.mobile_import.contracts import (
    FsmImportReport,
    FsmImportRequest,
)
from fishsense_services_orchestrator.ops.mobile_import.plan import (
    Folder,
    PlannedCapture,
    ed_folder,
    fingerprint,
    local_day,
    plan,
)

__all__ = ["FsmImportActivities"]

PLAN_MISMATCH = "FsmImportPlanMismatch"
REFUSED = "FsmImportRefused"
APP = "FishSense Mobile, an export from before sync (photos table only)"


def _put(url: str, body: bytes, headers: dict[str, str]) -> None:
    request = urllib.request.Request(url, data=body, method="PUT", headers=headers)
    with urllib.request.urlopen(request, timeout=300) as response:
        if response.status // 100 != 2:
            raise RuntimeError(f"PUT {url.split('?')[0]}: {response.status}")


def _sha256_b64(body: bytes) -> str:
    return base64.b64encode(hashlib.sha256(body).digest()).decode()


class FsmImportActivities:
    def __init__(
        self,
        *,
        catalog: Any,
        nas_settings: NasSettings,
        stores: Any,
        nas_client_factory: Optional[Callable[[], Any]] = None,
        put: Callable[[str, bytes, dict[str, str]], None] = _put,
    ) -> None:
        self._catalog = catalog
        self._stores = stores
        self._nas_client_factory = nas_client_factory or (
            lambda: build_nas_client(nas_settings)
        )
        self._put = put

    @activity.defn(name="plan_fsm_import")
    async def plan_fsm_import(self, request: FsmImportRequest) -> FsmImportReport:
        nas = await asyncio.to_thread(self._nas_client_factory)
        with tempfile.TemporaryDirectory() as tmp:
            planned, excluded = await self._plan(nas, request, tmp)
        return self._report(request, planned, excluded, applied=False)

    @activity.defn(name="import_fsm_captures")
    async def import_fsm_captures(
        self, request: FsmImportRequest, expected_plan_sha256: str
    ) -> FsmImportReport:
        tenant_id = await self._catalog.resolve_tenant(request.tenant)
        if tenant_id is None:
            raise ApplicationError(
                f"the orchestrator is not a member of {request.tenant!r}",
                type=REFUSED,
                non_retryable=True,
            )
        objects = await tenant_objects(self._catalog, self._stores, tenant_id)
        if objects is None:
            raise ApplicationError(
                f"{request.tenant!r} has no bucket", type=REFUSED, non_retryable=True
            )
        nas = await asyncio.to_thread(self._nas_client_factory)
        with tempfile.TemporaryDirectory() as tmp:
            planned, excluded = await self._plan(nas, request, tmp)
            report = self._report(request, planned, excluded, applied=True)
            if report.plan_sha256 != expected_plan_sha256:
                raise ApplicationError(
                    "the plan has changed since it was reviewed: plan again",
                    type=PLAN_MISMATCH,
                    non_retryable=True,
                )
            held = await self._catalog.imported(
                tenant_id, [p.client_capture_uuid for p in planned]
            )
            for capture in planned:
                activity.heartbeat(str(capture.client_capture_uuid))
                if capture.client_capture_uuid in held:
                    report.already_imported += 1
                    continue
                try:
                    await self._import(nas, objects, tenant_id, request, capture, tmp)
                except CaptureConflict as error:
                    activity.logger.warning("%s: %s", capture.photo_path, error)
                    report.excluded["conflict"] = report.excluded.get("conflict", 0) + 1
                    continue
                report.imported += 1
        return report

    async def _plan(self, nas, request: FsmImportRequest, tmp: str):
        folders = await self._scan(nas, request.dataset, tmp)
        planned, excluded = plan(
            folders,
            dataset=request.dataset,
            captured_from=request.captured_from,
            captured_to=request.captured_to,
            time_zone=request.time_zone,
        )
        missing = sorted({p.phone for p in planned} - set(request.intrinsics))
        bad = sorted(k for k, v in request.intrinsics.items() if len(v) != 9)
        if missing or bad:
            raise ApplicationError(
                f"intrinsics missing for {missing}, malformed for {bad}: "
                "every phone needs its 3x3 K, row-major",
                type=REFUSED,
                non_retryable=True,
            )
        return planned, excluded

    async def _scan(self, nas, dataset: str, tmp: str) -> list[Folder]:
        """Each ``ED-xx/<phone>/FSM`` folder: its photos, its database's rows
        (a copy left in ``tmp`` for the blobs), and its metadata."""
        folders = []
        top = await asyncio.to_thread(nas.list_dir, folder_path=dataset)
        for ed in sorted((e for e in top if e.is_dir and e.name.startswith("ED-")),
                         key=lambda e: e.name):  # fmt: skip
            for phone in await asyncio.to_thread(nas.list_dir, folder_path=ed.path):
                if not phone.is_dir:
                    continue
                path = f"{ed.path}/{phone.name}/FSM"
                names = {e.name: e for e in await asyncio.to_thread(
                    nas.list_dir, folder_path=f"{ed.path}/{phone.name}"
                )}  # fmt: skip
                if "FSM" not in names or not names["FSM"].is_dir:
                    continue
                activity.heartbeat(path)
                entries = await asyncio.to_thread(nas.list_dir, folder_path=path)
                files = {e.name: e for e in entries if not e.is_dir}
                if "database.sqlite" not in files:
                    continue
                local = self._local_database(tmp, path, dataset)
                body = await asyncio.to_thread(
                    nas.download_range, file_path=f"{path}/database.sqlite"
                )
                with open(local, "wb") as f:
                    f.write(body)
                metadata = {}
                if "metadata.json" in files:
                    raw = await asyncio.to_thread(
                        nas.download_range, file_path=f"{path}/metadata.json"
                    )
                    metadata = json.loads(raw)
                photos = {
                    n: e.size for n, e in files.items() if n.lower().endswith(".jpg")
                }
                folders.append(Folder(path=path, phone=phone.name, photos=photos,
                                      rows=read_rows(local), metadata=metadata))  # fmt: skip
        return folders

    @staticmethod
    def _local_database(tmp: str, folder: str, dataset: str) -> str:
        name = ed_folder(folder, dataset).replace("/", "_") + ".sqlite"
        return os.path.join(tmp, name)

    async def _import(self, nas, objects, tenant_id, request, capture: PlannedCapture,
                      tmp: str) -> None:  # fmt: skip
        photo = await asyncio.to_thread(
            nas.download_range, file_path=capture.photo_path
        )
        depth, confidence = read_blobs(
            self._local_database(tmp, capture.folder, request.dataset), capture.row_id
        )
        files = []
        for role, body in (
            ("rgb_jpeg", photo),
            ("depth_f32", depth),
            ("confidence_u8", confidence),
        ):
            spec = ROLES[role]
            key = f"mobile/captures/{capture.client_capture_uuid}/{role}{spec.suffix}"
            upload = objects.presign_put(
                key, sha256=_sha256_b64(body), size=len(body),
                content_type=spec.content_type,
            )  # fmt: skip
            await asyncio.to_thread(self._put, upload.url, body, upload.headers)
            files.append(CaptureFile(
                role=role, bucket=objects.bucket, key=key,
                sha256=hashlib.sha256(body).hexdigest(), size=len(body),
                content_type=spec.content_type,
            ))  # fmt: skip
        dataset = posixpath.basename(request.dataset.rstrip("/"))
        await self._catalog.import_capture(
            tenant_id,
            MobileCapture(
                client_capture_uuid=capture.client_capture_uuid,
                uploaded_by=None,
                captured_at=capture.captured_at,
                device_install_id=f"fsm-import:{dataset}:{capture.phone}",
                device_model=capture.phone,
                device_info=None,
                app_version=None,
                core_version=None,
                model_version=None,
                latitude=None,
                longitude=None,
                horizontal_accuracy_m=None,
                place_name=capture.metadata.get("site") or None,
                orientation=None,
                intrinsics=tuple(float(v) for v in request.intrinsics[capture.phone]),
                camera_pose=None,
                depth_width=capture.depth_width,
                depth_height=capture.depth_height,
                depth_row_bytes=capture.depth_width * 4,
                confidence_width=capture.confidence_width,
                confidence_height=capture.confidence_height,
                mask_width=None,
                mask_height=None,
                files=tuple(files),
                manifest={
                    "time_zone": request.time_zone,
                    "import": {
                        "app": APP,
                        "dataset": request.dataset,
                        "photo": capture.photo_path,
                        "database_row": capture.row_id,
                        "intrinsics_source": request.intrinsics_source,
                        "metadata": capture.metadata,
                    },
                },
                imported_from=f"{capture.database}#photos/{capture.row_id}",
            ),
        )

    @staticmethod
    def _report(
        request, planned, excluded: Counter, *, applied: bool
    ) -> FsmImportReport:
        return FsmImportReport(
            applied=applied,
            plan_sha256=fingerprint(planned),
            planned=len(planned),
            by_day=dict(sorted(Counter(local_day(p.timestamp, request.time_zone)
                                       for p in planned).items())),  # fmt: skip
            by_phone=dict(sorted(Counter(p.phone for p in planned).items())),
            by_folder=dict(sorted(Counter(ed_folder(p.folder, request.dataset)
                                          for p in planned).items())),  # fmt: skip
            excluded={k: excluded.get(k, 0)
                      for k in ("outside_window", "no_photo", "depth_size_mismatch")},  # fmt: skip
        )
