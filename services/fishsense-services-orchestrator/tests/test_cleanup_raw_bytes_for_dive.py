# pylint: disable=protected-access
"""Cleaning a dive's raw scratch out of Garage -- and only when nobody is
still reading it.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_cleanup_raw_bytes_for_dive_activity.py and
test_cleanup_raw_respects_other_children.py. v1's rules, kept:

* only Garage scratch is deleted -- the module holds no NAS client at all,
  which a tripwire asserts;
* a delete is idempotent: an absent key counts as deleted, so a retried
  cleanup never raises;
* **scratch is per dive, so deleting it is a cross-stage act.** Prod dive 442,
  2026-09-07: the species parent's cleanup deleted 984 objects while the laser
  child was mid-render, which died with NoSuchKey. Cleanup is skipped while any
  child that reads the dive's raw scratch is Running, asked of Temporal by
  exact workflow id, never by prefix (dive 44 must not hold 442's scratch);
* **it fails closed**: if Temporal can't be asked, the answer is "in use".
  Deleting under a live child kills a render silently and costs the dive's
  whole NAS staging; keeping scratch costs space until the next firing, which
  re-stages cheaply (`skipped_already_present`).

v2 changes, each pinned here:

* the target is (tenant, dive); the checksums come from the staging catalog,
  which leaves out scratch another dive owns (tested on Postgres in
  fishsense-services-api tests/test_raw_staging_store.py), and only this
  tenant's keys are deleted;
* the Temporal question is asked through the worker's own client
  (`activity.client()`), not a second connection opened per call;
* **a raw-reading child's id is built by `raw_scratch_reader_id`**, which
  refuses a reader the cleanup gate doesn't know. v1's list was a comment's
  promise ("every new child that reads raw scratch must be added here"), and
  the checkerboard pair sat outside it for days; now a parent that dispatches
  an unlisted reader fails when it builds the id.
"""

from __future__ import annotations

import inspect
import uuid

import boto3
import pytest
from moto import mock_aws
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_orchestrator.object_store import cleanup as sut
from fishsense_services_orchestrator.object_store.contracts import (
    CleanupRawBytesResult,
    StagingTarget,
)
from fishsense_services_orchestrator.object_store.layout import ObjectLayout
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)

BUCKET = "fishsense-test"
TENANT = uuid.UUID("7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11")
DIVE = uuid.UUID("11111111-2222-4333-8444-555555555555")
TARGET = StagingTarget(tenant_id=TENANT, dive_id=DIVE)
AAA, BBB = "a" * 32, "b" * 32


def _raw_key(checksum, tenant=TENANT) -> str:
    return f"tenants/{tenant}/raw/{checksum}.ORF"


class _Catalog:
    def __init__(self, checksums):
        self._checksums = checksums
        self.asked = []

    async def checksums_to_clean(self, tenant_id, dive_id):
        self.asked.append((tenant_id, dive_id))
        return list(self._checksums)


class _Execution:  # pylint: disable=too-few-public-methods
    def __init__(self, workflow_id):
        self.id = workflow_id


class _Temporal:
    """The slice of `temporalio.client.Client` the gate uses."""

    def __init__(self, running=(), error: BaseException | None = None):
        self.running = list(running)
        self.error = error
        self.queries: list[str] = []

    async def list_workflows(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        for workflow_id in self.running:
            yield _Execution(workflow_id)


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


def _raw_keys(s3) -> set[str]:
    resp = s3.list_objects_v2(Bucket=BUCKET)
    return {o["Key"] for o in resp.get("Contents", [])}


async def _cleanup(s3, checksums, temporal=None) -> CleanupRawBytesResult:
    activities = sut.RawCleanupActivities(catalog=_Catalog(checksums), store=_store(s3))
    return await ActivityEnvironment(client=temporal or _Temporal()).run(
        activities.cleanup_raw_bytes_for_dive, TARGET
    )


# -- the delete ------------------------------------------------------------------


async def test_deletes_all_raw_orfs_for_dive(s3):
    for checksum in (AAA, BBB):
        s3.put_object(Bucket=BUCKET, Key=_raw_key(checksum), Body=b"raw")

    result = await _cleanup(s3, [AAA, BBB])

    assert result.deleted == 2
    assert _raw_keys(s3) == set()


async def test_delete_of_absent_key_counted_as_success(s3):
    """Idempotent: a retried cleanup doesn't raise."""
    assert (await _cleanup(s3, [AAA])).deleted == 1


async def test_returns_zero_for_empty_dive(s3):
    assert (await _cleanup(s3, [])).deleted == 0


async def test_only_this_tenants_scratch_is_deleted(s3):
    """v2: another tenant's copy of the same frame is its own scratch."""
    theirs = _raw_key(AAA, uuid.uuid4())
    s3.put_object(Bucket=BUCKET, Key=theirs, Body=b"raw")
    s3.put_object(Bucket=BUCKET, Key=_raw_key(AAA), Body=b"raw")

    await _cleanup(s3, [AAA])

    assert _raw_keys(s3) == {theirs}


def test_activity_module_imports_no_nas_client():
    """Tripwire for the NAS-safety invariant: cleanup must never hold a NAS
    client, so nobody can add a NAS delete to it by accident."""
    source = inspect.getsource(sut)

    assert "ingest.nas" not in source, (
        "the cleanup module imported the NAS client -- cleanup must only ever "
        "delete the Garage scratch copy, never the NAS source."
    )
    assert "NasClient" not in source and "NasDownloadClient" not in source


# -- the scratch-in-use gate -----------------------------------------------------


async def test_does_not_delete_while_another_stage_is_still_reading(s3):
    for checksum in (AAA, BBB):
        s3.put_object(Bucket=BUCKET, Key=_raw_key(checksum), Body=b"raw")
    temporal = _Temporal(running=[f"preprocess-laser-{DIVE}"])

    result = await _cleanup(s3, [AAA, BBB], temporal)

    assert result.deleted == 0
    assert _raw_keys(s3) == {_raw_key(AAA), _raw_key(BBB)}, "nothing deleted"
    assert temporal.queries == [sut.build_scratch_in_use_query(DIVE)]


async def test_deletes_once_no_sibling_is_running(s3):
    s3.put_object(Bucket=BUCKET, Key=_raw_key(AAA), Body=b"raw")
    temporal = _Temporal(running=[])

    assert (await _cleanup(s3, [AAA], temporal)).deleted == 1
    assert temporal.queries == [sut.build_scratch_in_use_query(DIVE)]


async def test_an_unreachable_temporal_reads_as_in_use(s3):
    """Fails closed: an unknown answer must block the delete."""
    s3.put_object(Bucket=BUCKET, Key=_raw_key(AAA), Body=b"raw")

    result = await _cleanup(s3, [AAA], _Temporal(error=RuntimeError("dns error")))

    assert result.deleted == 0
    assert _raw_keys(s3) == {_raw_key(AAA)}


async def test_no_client_at_all_reads_as_in_use():
    """Outside a worker there is no client to ask; that is not a yes."""
    holder = await ActivityEnvironment().run(sut.scratch_in_use, DIVE)

    assert holder is not None


class TestReaderIds:
    def test_covers_every_child_that_reads_raw_scratch(self):
        """Preprocess, predict and both checkerboard children read
        `raw/{checksum}.ORF`; missing one means cleanup can delete under it.
        The checkerboard pair were absent until 2026-09-11 (v1)."""
        assert sut.raw_scratch_reader_ids(DIVE) == [
            f"preprocess-laser-{DIVE}",
            f"preprocess-species-{DIVE}",
            f"preprocess-headtail-{DIVE}",
            f"preprocess-slate-{DIVE}",
            f"predict-laser-{DIVE}",
            f"predict-slate-{DIVE}",
            f"perform-checkerboard-calibration-{DIVE}",
            f"verify-checkerboard-lattice-{DIVE}",
        ]

    def test_a_parent_builds_its_childs_id_from_the_same_list(self):
        for reader in sut.RAW_SCRATCH_READERS:
            assert sut.raw_scratch_reader_id(reader, DIVE) in set(
                sut.raw_scratch_reader_ids(DIVE)
            )

    def test_a_reader_the_gate_does_not_know_is_refused(self):
        """v2: the tripwire is structural. A new raw-reading child that isn't
        listed would be invisible to every other stage's cleanup."""
        with pytest.raises(ValueError, match="RAW_SCRATCH_READERS"):
            sut.raw_scratch_reader_id("preprocess-fins", DIVE)


class TestQuery:
    def test_asks_only_for_running_children_of_this_dive(self):
        q = sut.build_scratch_in_use_query(DIVE)

        assert 'ExecutionStatus = "Running"' in q
        assert f"'preprocess-laser-{DIVE}'" in q
        assert f"'predict-slate-{DIVE}'" in q

    def test_does_not_match_a_different_dive(self):
        """`WorkflowId IN (...)`, never a prefix match."""
        other = uuid.UUID("11111111-2222-4333-8444-555555555556")
        q = sut.build_scratch_in_use_query(other)

        assert str(DIVE) not in q
        assert " in (" in q.lower() and "startswith" not in q.lower()


def test_a_lost_membership_stops_cleanup_rather_than_retrying_for_its_window():
    """v1 gave cleanup no retry policy. The catalog now raises NotAMember when
    the orchestrator's membership is revoked, which no retry can fix; without
    a policy saying so, cleanup retried it for its whole 15 minutes (review of
    foundation/object-store). Transient failures still retry within the window."""
    from fishsense_services_orchestrator.object_store.steps import (
        CLEANUP_RAW_RETRY_POLICY,
    )

    assert "NotAMember" in (CLEANUP_RAW_RETRY_POLICY.non_retryable_error_types or [])
    assert not CLEANUP_RAW_RETRY_POLICY.maximum_attempts  # unbounded, as v1
