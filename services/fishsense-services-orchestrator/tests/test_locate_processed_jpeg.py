"""The processed-JPEG check, as an activity.

Ported from fishsense-lite@77e8f8e5 `ObjectStoreClient.has_processed_jpeg`
(services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/
object_store.py) and its test in tests/test_object_store.py: a HEAD in the
labels bucket, so a decoupled populate never seeds a task for a frame whose
JPEG isn't written yet.

v2 changes, each pinned here:

* it takes a capture, not a checksum: the catalog says which checksum, and
  whether the frame came from v1 -- the one case where the JPEG may still be
  where v1 wrote it, which Label Studio tasks point at;
* it answers *where* (an ``ObjectRef``) rather than yes or no, because
  populate needs the location for the task it creates; None is "not yet";
* a capture the tenant doesn't have, or a stage folder that doesn't exist, is
  a final refusal rather than "not yet", which a caller would wait on forever.
"""

from __future__ import annotations

import uuid

import boto3
import pytest
from moto import mock_aws
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.raw_staging_store import CaptureChecksum
from fishsense_services_contracts.object_store import ObjectRef, ObjectStoreConnection
from fishsense_services_orchestrator.object_store.contracts import (
    ProcessedJpegRequest,
)
from fishsense_services_orchestrator.object_store.jpegs import ProcessedJpegActivities
from fishsense_services_orchestrator.object_store.layout import ObjectLayout
from fishsense_services_orchestrator.object_store.store import (
    OrchestratorObjectStore,
)

BUCKET = "fishsense-test"
LABELS = "labels-fishsense-test"
TENANT = uuid.UUID("7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11")
MIGRATED, INGESTED = uuid.UUID(int=1), uuid.UUID(int=2)
SUM = "c" * 32
NEW_KEY = f"tenants/{TENANT}/preprocess_headtail_jpeg/{SUM}.JPG"
V1_KEY = f"fishsense-lite/preprocess_headtail_jpeg/{SUM}.JPG"


class _Catalog:
    async def capture_checksum(self, tenant_id, capture_id):
        assert tenant_id == TENANT
        return {
            MIGRATED: CaptureChecksum(SUM, from_v1=True),
            INGESTED: CaptureChecksum(SUM, from_v1=False),
        }.get(capture_id)


@pytest.fixture(name="s3")
def s3_fixture():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        client.create_bucket(Bucket=LABELS)
        yield client


async def _locate(s3, capture, folder="preprocess_headtail_jpeg"):
    settings = ObjectStoreConnection(
        endpoint_url="http://garage.example.com",
        region="garage",
        access_key_id="k",
        secret_access_key="s",
        bucket=BUCKET,
        labels_bucket=LABELS,
        legacy_labels_prefix="fishsense-lite",
    )
    activities = ProcessedJpegActivities(
        catalog=_Catalog(),
        store=OrchestratorObjectStore(s3, ObjectLayout(settings)),
    )
    return await ActivityEnvironment().run(
        activities.locate_processed_jpeg,
        ProcessedJpegRequest(tenant_id=TENANT, capture_id=capture, folder=folder),
    )


async def test_not_written_yet_is_none(s3):
    assert await _locate(s3, INGESTED) is None


async def test_a_jpeg_the_processor_wrote_is_found_under_the_tenant(s3):
    s3.put_object(Bucket=LABELS, Key=NEW_KEY, Body=b"x")

    assert await _locate(s3, INGESTED) == ObjectRef(bucket=LABELS, key=NEW_KEY)


async def test_a_migrated_frames_jpeg_is_found_where_v1_wrote_it(s3):
    s3.put_object(Bucket=LABELS, Key=V1_KEY, Body=b"x")

    assert await _locate(s3, MIGRATED) == ObjectRef(bucket=LABELS, key=V1_KEY)


async def test_a_frame_v2_ingested_is_never_answered_with_a_v1_key(s3):
    s3.put_object(Bucket=LABELS, Key=V1_KEY, Body=b"x")

    assert await _locate(s3, INGESTED) is None


async def test_a_capture_the_tenant_does_not_have_is_refused(s3):
    with pytest.raises(ApplicationError) as excinfo:
        await _locate(s3, uuid.uuid4())

    assert excinfo.value.non_retryable
    assert excinfo.value.type == "UnknownCapture"


async def test_a_folder_no_stage_writes_is_refused(s3):
    with pytest.raises(ApplicationError) as excinfo:
        await _locate(s3, INGESTED, folder="preprocess_jpg")

    assert excinfo.value.non_retryable
    assert excinfo.value.type == "UnknownJpegFolder"
