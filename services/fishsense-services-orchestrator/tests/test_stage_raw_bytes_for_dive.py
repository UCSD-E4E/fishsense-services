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

from fishsense_services_api.raw_staging_store import StagingCapture
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.ingest.nas_errors import NAS_FILE_NOT_FOUND_TYPE
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings
from fishsense_services_orchestrator.object_store import staging as sut
from fishsense_services_orchestrator.object_store.contracts import (
    StageRawBytesResult,
    StagingTarget,
)
from fishsense_services_orchestrator.object_store.steps import (
    STAGE_RAW_RETRY_POLICY,
)
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)
from fishsense_services_orchestrator.object_store.layout import ObjectLayout

BUCKET = "fishsense-test"
ROOT = "/fishsense_data/REEF/data"
TENANT = uuid.UUID("7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11")
DIVE = uuid.UUID("11111111-2222-4333-8444-555555555555")
TARGET = StagingTarget(tenant_id=TENANT, dive_id=DIVE)
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
    def __init__(self, captures):
        self._captures = captures
        self.asked = []

    async def captures_to_stage(self, tenant_id, dive_id):
        self.asked.append((tenant_id, dive_id))
        return list(self._captures)


class _Nas:
    """FileStation's `download_to`: the file lands at dest_dir/basename."""

    def __init__(self, payload=b"raw-bytes", error: BaseException | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[str] = []

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
    )
    return OrchestratorObjectStore(s3, ObjectLayout(settings))


def _activities(s3, captures, nas, *, concurrency=1):
    return sut.RawStagingActivities(
        catalog=_Catalog(captures),
        store=_store(s3),
        nas_settings=_nas_settings(),
        nas_client_factory=lambda: nas,
        staging_settings=sut.RawStagingSettings(stage_concurrency=concurrency),
    )


def _raw_keys(s3) -> set[str]:
    resp = s3.list_objects_v2(Bucket=BUCKET)
    return {o["Key"] for o in resp.get("Contents", [])}


async def _stage(activities) -> StageRawBytesResult:
    return await ActivityEnvironment().run(activities.stage_raw_bytes_for_dive, TARGET)


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
    assert activities._catalog.asked == [(TENANT, DIVE)]


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


async def test_returns_zeros_when_dive_has_no_images(s3):
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
