# pylint: disable=protected-access
"""Staging a dive's raw frames: NAS -> Garage scratch.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_stage_raw_bytes_for_dive_activity.py. v1's rules, kept:

1. HEAD-skips a frame already staged (no NAS download, no PUT).
2. Stages a new frame: NAS download, then PUT keyed by checksum.
3. A frame with no path (or checksum) is counted as ``no_path`` -- never a
   crash, never silently dropped.
4. Returns the per-dive summary for the parent to log.
5. The share-relative path the database stores gets the NAS root prepended
   before FileStation sees it (which surfaces a bare path as a 502, not a 404);
   an absolute path passes through. (The resolver itself is pinned in
   test_nas_frames.py.)
6. The 2026-05-07 invariant: when the NAS download fails, nothing is uploaded.
7. No inner retry: a transient error (502, 407) propagates for Temporal's
   bounded policy; a permanent one (408) is a non-retryable
   ``NasFileNotFound``, surfaced from under the TaskGroup's ExceptionGroup.
8. The retry policy parents stage with is bounded and names ``NasFileNotFound``.
9. Concurrency defaults to one download at a time (FileStation's shared
   download backend falls over under load) and clamps to at least one.

v2 changes, each pinned here:

* the target is (tenant, dive), and the frame is staged under the tenant's
  key; the frames come from the staging catalog (canonical-only, tested on
  Postgres in fishsense-services-api tests/test_raw_staging_store.py);
* the concurrency is a setting read at startup (``FISHSENSE_NAS_STAGE_
  CONCURRENCY``): a value that isn't a number fails the worker's start, where
  v1 silently fell back to the default.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
import time
import uuid
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from pydantic import ValidationError
from synology_filestation import DSMError, TransportError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

import hashlib

from fishsense_services_api.raw_staging_store import RawCopy, StagingCapture
from fishsense_services_api.tenant_stores import NoTenantStore
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.ingest.nas_errors import NAS_FILE_NOT_FOUND_TYPE
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings
from fishsense_services_orchestrator.object_store import staging as sut
from fishsense_services_orchestrator.object_store.contracts import (
    StageRawBytesResult,
    StagingTarget,
)
from fishsense_services_orchestrator.object_store.steps import (
    LONG_STAGE_RAW_TIMEOUT,
    STAGE_RAW_RETRY_POLICY,
    STAGE_RAW_TIMEOUT,
)
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)
from fishsense_services_orchestrator.object_store.layout import ObjectLayout

BUCKET = "fishsense-test"
ROOT = "/fishsense_data/REEF/data"
TENANT = uuid.UUID("7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11")
MISSION = uuid.UUID("11111111-2222-4333-8444-555555555555")
TARGET = StagingTarget(tenant_id=TENANT, mission_id=MISSION)
REL = "2024.06.20.REEF/dive_42/IMG.ORF"


def _md5(n: int) -> str:
    return f"{n:032x}"


def _capture(n: int, *, path: str | None = REL, checksum: str | None = None):
    return StagingCapture(
        uuid.UUID(int=n), path, _md5(n) if checksum is None else checksum
    )


def _raw_key(checksum: str, tenant=TENANT) -> str:
    return f"tenants/{tenant}/raw/{checksum}.ORF"


class _Catalog:
    def __init__(self, captures, slug_error=None):
        self._captures = captures
        self._slug_error = slug_error
        self.asked = []
        self.asked_slug = 0
        self.asked_ids = []

    async def tenant_slug(self, tenant_id):
        self.asked_slug += 1
        if self._slug_error is not None:
            raise self._slug_error
        return "lab"

    async def captures_to_stage(self, tenant_id, mission_id, capture_ids=None):
        self.asked.append((tenant_id, mission_id))
        self.asked_ids.append(capture_ids)
        return list(self._captures)


class _Nas:
    """FileStation's `download_to`: the file lands at dest_dir/basename.
    `there` is what `exists` answers: True, False, or an error to raise."""

    def __init__(
        self,
        payload=b"raw-bytes",
        error: BaseException | None = None,
        there: bool | BaseException = True,
    ):
        self.payload = payload
        self.error = error
        self.there = there
        self.calls: list[str] = []
        self.checked: list[str] = []

    def exists(self, *, file_path: str) -> bool:
        self.checked.append(file_path)
        if isinstance(self.there, BaseException):
            raise self.there
        return self.there

    def download_to(self, *, src_path: str, dest_dir: str) -> None:
        self.calls.append(src_path)
        if self.error is not None:
            raise self.error
        (Path(dest_dir) / Path(src_path).name).write_bytes(self.payload)


def _nas_settings() -> NasSettings:
    return NasSettings(
        url="https://nas.example.test:6021",
        username="svc",
        password="unused",
        raw_root_path=ROOT,
    )


@pytest.fixture(name="s3")
def s3_fixture():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def _store(s3) -> OrchestratorObjectStore:
    settings = ObjectStoreConnection(
        endpoint_url="http://garage.example.com",
        region="garage",
        access_key_id="k",
        secret_access_key="s",
        bucket=BUCKET,
        legacy_labels_prefix="fishsense-lite",
    )
    return OrchestratorObjectStore(s3, ObjectLayout(settings))


def _activities(s3, captures, nas, *, concurrency=1, stores=None, catalog=None):
    return sut.RawStagingActivities(
        catalog=catalog or _Catalog(captures),
        store=_store(s3),
        nas_settings=_nas_settings(),
        nas_client_factory=lambda: nas,
        staging_settings=sut.RawStagingSettings(stage_concurrency=concurrency),
        stores=stores,
    )


def _raw_keys(s3) -> set[str]:
    resp = s3.list_objects_v2(Bucket=BUCKET)
    return {o["Key"] for o in resp.get("Contents", [])}


async def _stage(activities, target=TARGET) -> StageRawBytesResult:
    return await ActivityEnvironment().run(
        activities.stage_raw_bytes_for_mission, target
    )


async def test_skips_already_staged_checksums_no_nas_download(s3):
    for n in (1, 2):
        s3.put_object(Bucket=BUCKET, Key=_raw_key(_md5(n)), Body=b"old")
    nas = _Nas()

    result = await _stage(_activities(s3, [_capture(1), _capture(2)], nas))

    assert result == StageRawBytesResult(staged=0, skipped_already_present=2, no_path=0)
    assert nas.calls == []
    assert s3.get_object(Bucket=BUCKET, Key=_raw_key(_md5(1)))["Body"].read() == (
        b"old"
    )


async def test_stages_new_checksums_via_nas_download_then_put(s3):
    """The share-relative path must reach FileStation absolute."""
    nas = _Nas(payload=b"raw-bytes")
    activities = _activities(s3, [_capture(1)], nas)

    result = await _stage(activities)

    assert result.staged == 1
    assert result.skipped_already_present == 0
    assert nas.calls == [f"{ROOT}/{REL}"]
    assert nas.calls != [REL]
    assert _raw_keys(s3) == {_raw_key(_md5(1))}
    assert s3.get_object(Bucket=BUCKET, Key=_raw_key(_md5(1)))["Body"].read() == (
        b"raw-bytes"
    )
    assert activities._catalog.asked == [(TENANT, MISSION)]


async def test_an_absolute_path_passes_through_to_the_nas(s3):
    nas = _Nas()

    await _stage(_activities(s3, [_capture(1, path="/already/absolute/P.ORF")], nas))

    assert nas.calls == ["/already/absolute/P.ORF"]


async def test_the_frame_is_staged_under_its_tenant_and_nowhere_else(s3):
    """v2: another tenant's copy of the same frame is not this tenant's scratch."""
    s3.put_object(Bucket=BUCKET, Key=_raw_key(_md5(1), uuid.uuid4()), Body=b"theirs")
    nas = _Nas(payload=b"ours")

    result = await _stage(_activities(s3, [_capture(1)], nas))

    assert result.staged == 1
    assert s3.get_object(Bucket=BUCKET, Key=_raw_key(_md5(1)))["Body"].read() == (
        b"ours"
    )


async def test_counts_no_path_images_without_crashing(s3):
    captures = [
        _capture(1, path=None),
        _capture(2, path="dive_x/file.ORF", checksum=""),
        _capture(3, path="dive_y/file.ORF"),
    ]
    nas = _Nas()

    result = await _stage(_activities(s3, captures, nas))

    assert result.no_path == 2
    assert result.staged == 1
    assert nas.calls == [f"{ROOT}/dive_y/file.ORF"]


async def test_failed_nas_download_does_not_upload_to_object_store(s3):
    """The 2026-05-07 stage-2 incident: a DSM JSON-error body was staged as
    `.ORF` content. Whatever the NAS client does, a failed download writes
    nothing."""
    nas = _Nas(error=RuntimeError("simulated DSM session expired (code 119)"))

    with pytest.raises(BaseException):
        await _stage(_activities(s3, [_capture(1)], nas))

    assert len(nas.calls) == 1
    assert (
        _raw_keys(s3) == set()
    ), "staging uploaded to the object store after the NAS download raised"


async def test_returns_zeros_when_mission_has_no_images(s3):
    nas = _Nas()

    result = await _stage(_activities(s3, [], nas))

    assert result == StageRawBytesResult(staged=0, skipped_already_present=0, no_path=0)
    assert nas.calls == []


async def test_transient_502_propagates_without_inner_retry(s3):
    """One attempt per activity execution: an inner retry under Temporal's
    outer one is what produced the 200x download storm (krg-infra#501)."""
    nas = _Nas(error=TransportError("download HTTP 502 Bad Gateway"))

    with pytest.raises(BaseException) as excinfo:
        await _stage(_activities(s3, [_capture(1)], nas))

    leaves = list(sut._iter_leaf_exceptions(excinfo.value))
    assert not any(
        isinstance(leaf, ApplicationError) and leaf.non_retryable for leaf in leaves
    )
    assert len(nas.calls) == 1
    assert _raw_keys(s3) == set()


async def test_permanent_408_raises_non_retryable_application_error(s3):
    """Surfaced un-wrapped from the TaskGroup, so Temporal honours it."""
    nas = _Nas(error=DSMError("Synology API error 408"))

    with pytest.raises(ApplicationError) as excinfo:
        await _stage(_activities(s3, [_capture(1)], nas))

    assert excinfo.value.non_retryable is True
    assert excinfo.value.type == NAS_FILE_NOT_FOUND_TYPE
    assert len(nas.calls) == 1
    assert _raw_keys(s3) == set()


async def test_a_502_for_a_file_the_nas_does_not_have_is_permanent(s3):
    """FileStation answers a download of a moved or deleted file with a 502,
    not a 408: retried, it held the slate scan on one dive for hours. A
    failed download asks whether the file is there; if not, it is final."""
    nas = _Nas(error=TransportError("download HTTP 502 Bad Gateway"), there=False)

    with pytest.raises(ApplicationError) as excinfo:
        await _stage(_activities(s3, [_capture(1)], nas))

    assert excinfo.value.non_retryable is True
    assert excinfo.value.type == NAS_FILE_NOT_FOUND_TYPE
    assert nas.calls[0] in str(excinfo.value)
    assert nas.checked == nas.calls
    assert _raw_keys(s3) == set()


async def test_a_502_for_a_file_that_is_there_stays_transient(s3):
    nas = _Nas(error=TransportError("download HTTP 502 Bad Gateway"), there=True)

    with pytest.raises(BaseException) as excinfo:
        await _stage(_activities(s3, [_capture(1)], nas))

    leaves = list(sut._iter_leaf_exceptions(excinfo.value))
    assert not any(isinstance(leaf, ApplicationError) for leaf in leaves)
    assert any(isinstance(leaf, TransportError) for leaf in leaves)


async def test_an_existence_check_that_fails_leaves_the_download_error_transient(s3):
    """The NAS that 502s a download may 502 the check too: that proves
    nothing missing, so the download's own (transient) error stands."""
    nas = _Nas(
        error=TransportError("download HTTP 502 Bad Gateway"),
        there=TransportError("list HTTP 502 Bad Gateway"),
    )

    with pytest.raises(BaseException) as excinfo:
        await _stage(_activities(s3, [_capture(1)], nas))

    leaves = list(sut._iter_leaf_exceptions(excinfo.value))
    assert not any(isinstance(leaf, ApplicationError) for leaf in leaves)
    assert [str(leaf) for leaf in leaves] == ["download HTTP 502 Bad Gateway"]


async def test_transient_dsm_407_propagates_retryable(s3):
    """407 was the backend fail-closing during the incident: transient."""
    nas = _Nas(error=DSMError("Synology API error 407"))

    with pytest.raises(BaseException) as excinfo:
        await _stage(_activities(s3, [_capture(1)], nas))

    leaves = list(sut._iter_leaf_exceptions(excinfo.value))
    assert not any(
        isinstance(leaf, ApplicationError) and leaf.non_retryable for leaf in leaves
    )


def test_stage_raw_retry_policy_is_bounded_and_marks_missing_file_non_retryable():
    """The type the activity raises for a 408 must be exactly what the parents'
    policy lists -- Temporal matches the string."""
    assert STAGE_RAW_RETRY_POLICY.maximum_attempts == 5
    assert NAS_FILE_NOT_FOUND_TYPE in (
        STAGE_RAW_RETRY_POLICY.non_retryable_error_types or []
    )


def test_a_lost_membership_is_not_retried():
    """v2: the catalog acts only as a member of the tenant, and a membership
    revoked mid-flight will not come back by retrying (as for every catalog
    call; the clustering parent's policy says the same)."""
    assert "NotAMember" in (STAGE_RAW_RETRY_POLICY.non_retryable_error_types or [])


# -- concurrency -----------------------------------------------------------------


def test_stage_concurrency_defaults_to_one(monkeypatch):
    monkeypatch.delenv("FISHSENSE_NAS_STAGE_CONCURRENCY", raising=False)

    assert sut.RawStagingSettings().stage_concurrency == 1


def test_stage_concurrency_reads_config(monkeypatch):
    monkeypatch.setenv("FISHSENSE_NAS_STAGE_CONCURRENCY", "3")

    assert sut.RawStagingSettings().stage_concurrency == 3


@pytest.mark.parametrize("value", ["0", "-2"])
def test_stage_concurrency_clamps_to_at_least_one(monkeypatch, value):
    monkeypatch.setenv("FISHSENSE_NAS_STAGE_CONCURRENCY", value)

    assert sut.RawStagingSettings().stage_concurrency == 1


def test_a_stage_concurrency_that_is_not_a_number_fails_startup(monkeypatch):
    """v2: v1 fell back to the default, so a typo silently ran serially."""
    monkeypatch.setenv("FISHSENSE_NAS_STAGE_CONCURRENCY", "three")

    with pytest.raises(ValidationError):
        sut.RawStagingSettings()


@pytest.mark.parametrize("limit", [1, 2])
async def test_no_more_downloads_run_at_once_than_the_limit(s3, limit):
    active, peak = 0, 0
    lock = threading.Lock()

    class _SlowNas(_Nas):
        def download_to(self, *, src_path, dest_dir):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            super().download_to(src_path=src_path, dest_dir=dest_dir)

    captures = [_capture(n, path=f"d/P{n}.ORF") for n in range(1, 6)]

    result = await asyncio.wait_for(
        _stage(_activities(s3, captures, _SlowNas(), concurrency=limit)), 10
    )

    assert result.staged == 5
    assert peak == limit


# -- from the tenant's bucket (2026-10-21) ----------------------------------------
#
# Mission 440's staging hit its hour copying 1,201 raws from the NAS, every one
# of them already copied into the tenant's bucket by the render stage. A copy
# in the tenant's current bucket, checked against what was recorded, is staged
# from there; anything else about it -- missing, another bucket, other bytes,
# an error -- falls back to the NAS, as before.

TENANT_BUCKET = "fishsense-tenant-lab"
COPY = b"raw-bytes-from-the-bucket"


class _Objects:
    def __init__(self, bucket, objects, error=None, delay=0.0):
        self.bucket = bucket
        self._objects = objects
        self._error = error
        self._delay = delay
        self.read: list[str] = []
        self._lock = threading.Lock()
        self.in_flight = 0
        self.most_in_flight = 0

    def get(self, key):
        with self._lock:
            self.read.append(key)
            self.in_flight += 1
            self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            if self._delay:
                time.sleep(self._delay)
            if self._error is not None:
                raise self._error
            return self._objects.get(key)
        finally:
            with self._lock:
                self.in_flight -= 1


class _Stores:
    def __init__(self, objects=None, tenants=("lab",)):
        self._objects = objects
        self.tenants = set(tenants)

    def for_tenant(self, slug):
        if self._objects is None:
            raise NoTenantStore(slug)
        return self._objects


def _copied(n, data=COPY, *, bucket=TENANT_BUCKET, md5=None, size=None,
            path=REL, checksum=None):  # fmt: skip
    """A capture whose copy is `data`; its checksum is the data's MD5, as for
    every copy in production (2026-10-21: 57,462 of 57,462)."""
    digest = hashlib.md5(data).hexdigest()
    return StagingCapture(
        uuid.UUID(int=n), path, checksum or digest,
        (RawCopy(bucket=bucket, key=f"raws/{n}.ORF",
                 bytes=len(data) if size is None else size,
                 md5=md5 or digest),),
    )  # fmt: skip


def _key_of(capture) -> str:
    return _raw_key(capture.checksum)


async def test_a_raw_copied_to_the_tenants_bucket_is_staged_from_it_not_the_nas(s3):
    nas = _Nas(payload=b"from-the-nas")
    objects = _Objects(TENANT_BUCKET, {"raws/1.ORF": COPY})

    capture = _copied(1)
    result = await _stage(_activities(s3, [capture], nas, stores=_Stores(objects)))

    assert nas.calls == []
    assert objects.read == ["raws/1.ORF"]
    assert s3.get_object(Bucket=BUCKET, Key=_key_of(capture))["Body"].read() == COPY
    assert (result.staged, result.from_bucket) == (1, 1)


async def test_frames_with_and_without_a_copy_are_staged_each_from_its_source(s3):
    nas = _Nas(payload=b"from-the-nas")
    objects = _Objects(TENANT_BUCKET, {"raws/1.ORF": COPY})

    result = await _stage(_activities(s3, [_copied(1), _capture(2)], nas,
                                      stores=_Stores(objects)))  # fmt: skip

    assert nas.calls == [f"{ROOT}/{REL}"]
    assert (result.staged, result.from_bucket) == (2, 1)


@pytest.mark.parametrize(
    "case",
    ["other_bytes", "other_size", "missing", "error", "other_bucket", "no_store",
     "not_the_captures_checksum", "slug_unknown"],
)  # fmt: skip
async def test_a_copy_that_cannot_be_trusted_or_read_falls_back_to_the_nas(s3, case):
    """Never a frame staged from bytes that aren't the ones recorded."""
    nas = _Nas(payload=b"from-the-nas")
    capture = _copied(1)
    objects = _Objects(TENANT_BUCKET, {"raws/1.ORF": COPY})
    if case == "other_bytes":
        objects = _Objects(TENANT_BUCKET, {"raws/1.ORF": b"x" * len(COPY)})
    elif case == "other_size":
        capture = _copied(1, size=len(COPY) + 1)
    elif case == "missing":
        objects = _Objects(TENANT_BUCKET, {})
    elif case == "error":
        objects = _Objects(TENANT_BUCKET, {}, error=RuntimeError("garage down"))
    elif case == "other_bucket":
        capture = _copied(1, bucket="some-other-bucket")
    elif case == "not_the_captures_checksum":
        # Review of #219: the copy is checked against what it recorded, and
        # that against the capture it is staged as.
        capture = _copied(1, checksum=_md5(1))
    stores = _Stores(None if case == "no_store" else objects)
    catalog = (
        _Catalog([capture], slug_error=RuntimeError("no such tenant row"))
        if case == "slug_unknown"
        else None
    )

    result = await _stage(
        _activities(s3, [capture], nas, stores=stores, catalog=catalog)
    )

    assert nas.calls == [f"{ROOT}/{REL}"]
    assert s3.get_object(Bucket=BUCKET, Key=_key_of(capture))["Body"].read() == (
        b"from-the-nas"
    )
    assert (result.staged, result.from_bucket) == (1, 0)


async def test_an_already_staged_frame_reads_neither_source(s3):
    s3.put_object(Bucket=BUCKET, Key=_key_of(_copied(1)), Body=b"old")
    nas = _Nas()
    objects = _Objects(TENANT_BUCKET, {"raws/1.ORF": COPY})

    result = await _stage(_activities(s3, [_copied(1)], nas, stores=_Stores(objects)))

    assert nas.calls == [] and objects.read == []
    assert result.skipped_already_present == 1


def test_staging_has_hours_where_the_parent_budgets_them_and_an_hour_elsewhere():
    """B (2026-10-21): 440's 1,201 raws didn't fit in an hour from the NAS.
    The staging activity heartbeats per file, so a stall is still caught in
    minutes; the long ceiling only has to cover a large mission's honest
    pace. Review of #219: parents whose own run timeout is an hour or less
    keep the hour, so their staging still ends in a failure they clean up
    after, not in their run's timeout."""
    assert LONG_STAGE_RAW_TIMEOUT >= timedelta(hours=6)
    assert STAGE_RAW_TIMEOUT == timedelta(hours=1)


from temporalio import workflow as _workflow  # noqa: E402

from fishsense_services_orchestrator.object_store.steps import (  # noqa: E402
    stage_raw,
)


@_workflow.defn(name="StageOnce")
class _StageOnce:  # pylint: disable=too-few-public-methods
    @_workflow.run
    async def run(self) -> None:
        await stage_raw(TARGET)


async def test_the_staging_step_schedules_the_whole_ceiling_and_the_run_budgets_it():
    """What `stage_raw` schedules by default is `STAGE_RAW_TIMEOUT`, and the
    automatic run's timeout sums the long figure it stages with, so a long
    staging isn't cut short by its parent instead."""
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker

    from fishsense_services_contracts.temporal import DATA_CONVERTER
    from fishsense_services_orchestrator.automatic_results import (
        workflow as automatic,
    )

    seen = []

    @activity.defn(name="stage_raw_bytes_for_dive")
    async def stage(target: StagingTarget) -> StageRawBytesResult:
        seen.append(activity.info().schedule_to_close_timeout)
        return StageRawBytesResult(staged=0, skipped_already_present=0, no_path=0)

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(env.client, task_queue="stage-once", workflows=[_StageOnce],
                          activities=[stage],
                          workflow_runner=UnsandboxedWorkflowRunner()):  # fmt: skip
            await env.client.execute_workflow(
                "StageOnce", id=f"stage-once-{uuid.uuid4()}", task_queue="stage-once"
            )

    assert seen == [STAGE_RAW_TIMEOUT]
    assert automatic.STAGE_TIMEOUT == LONG_STAGE_RAW_TIMEOUT


# -- review of #219 ----------------------------------------------------------------


async def test_a_capture_with_no_nas_path_but_a_bucket_copy_is_staged_from_it(s3):
    """Its bytes are in the tenant's bucket: it is not `no_path`."""
    nas = _Nas()
    capture = _copied(1, path=None)
    objects = _Objects(TENANT_BUCKET, {"raws/1.ORF": COPY})

    result = await _stage(_activities(s3, [capture], nas, stores=_Stores(objects)))

    assert (result.staged, result.from_bucket, result.no_path) == (1, 1, 0)
    assert nas.calls == []


async def test_a_capture_with_no_path_and_no_usable_copy_is_still_no_path(s3):
    nas = _Nas()
    capture = _copied(1, path=None)
    objects = _Objects(TENANT_BUCKET, {})

    result = await _stage(_activities(s3, [capture], nas, stores=_Stores(objects)))

    assert (result.staged, result.no_path) == (0, 1)
    assert nas.calls == []


async def test_with_no_tenant_stores_configured_the_slug_is_never_asked(s3):
    """An empty stores file (or none) is the NAS, with no database round trip
    that could fail a staging the NAS would have done."""
    catalog = _Catalog([_copied(1)])

    await _stage(_activities(s3, [], _Nas(), stores=_Stores(None, tenants=()),
                             catalog=catalog))  # fmt: skip

    assert catalog.asked_slug == 0


async def test_a_bucket_that_errors_is_given_up_on_after_the_first_read(s3, caplog):
    """A rotated key or a missing bucket fails every read alike: one warning,
    then the NAS for the rest -- not a failed read and a warning per frame.
    Reads already in flight when the first fails may finish failing too: at
    most the worker's bucket slots, never the mission's every frame."""
    nas = _Nas(payload=b"from-the-nas")
    captures = [_copied(n, data=bytes([n]) * 8) for n in range(1, 41)]
    objects = _Objects(TENANT_BUCKET, {}, error=RuntimeError("AccessDenied"))

    result = await _stage(_activities(s3, captures, nas, stores=_Stores(objects)))

    assert 1 <= len(objects.read) <= sut.BUCKET_CONCURRENCY
    assert len(nas.calls) == 40
    assert (result.staged, result.from_bucket) == (40, 0)
    assert sum("AccessDenied" in r.getMessage() for r in caplog.records) == 1


async def test_a_frame_falling_back_to_the_nas_is_checked_in_scratch_once(s3):
    nas = _Nas()
    activities = _activities(s3, [_copied(1)], nas,
                             stores=_Stores(_Objects(TENANT_BUCKET, {})))  # fmt: skip
    heads = []
    real = activities._store.has_raw

    async def counting(tenant_id, checksum):
        heads.append(checksum)
        return await real(tenant_id, checksum)

    activities._store.has_raw = counting

    await _stage(activities)

    assert len(heads) == 1


async def test_bucket_reads_are_limited_across_every_staging_on_the_worker(s3):
    """Each read holds a whole raw (~15 MB) in memory: the limit is the
    worker's, not each staging's, or three runs hold three times as many."""
    captures = [_copied(n, data=bytes([n]) * 8) for n in range(1, 13)]
    objects = _Objects(
        TENANT_BUCKET,
        {f"raws/{c.capture_id.int}.ORF": bytes([c.capture_id.int]) * 8
         for c in captures},
        delay=0.05,
    )  # fmt: skip
    activities = _activities(s3, [], _Nas(), stores=_Stores(objects))
    first, second = captures[:6], captures[6:]
    other = StagingTarget(tenant_id=TENANT, mission_id=uuid.uuid4())

    class _Split(_Catalog):
        async def captures_to_stage(self, tenant_id, mission_id, capture_ids=None):
            return list(first if mission_id == MISSION else second)

    activities._catalog = _Split([])

    await asyncio.gather(_stage(activities), _stage(activities, other))

    assert len(objects.read) == 12
    assert objects.most_in_flight <= sut.BUCKET_CONCURRENCY


async def test_a_target_naming_captures_stages_only_those(s3):
    """The frames step stages the frames it owes (review of #219)."""
    catalog = _Catalog([_capture(1)])
    ids = [uuid.UUID(int=1)]

    await _stage(_activities(s3, [], _Nas(), catalog=catalog),
                 StagingTarget(tenant_id=TENANT, mission_id=MISSION,
                               capture_ids=ids))  # fmt: skip

    assert catalog.asked_ids == [ids]
