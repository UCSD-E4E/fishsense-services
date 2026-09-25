"""The worker process: configuration, the Temporal connection, registration.

v1 (fishsense-lite@a8b2c3bc fishsense_api_workflow_worker/worker.py) read a
global Dynaconf object; v2 reads typed ``FISHSENSE_*`` settings, validated at
startup, and builds the worker from them. Two v1 lessons are pinned here:

* **the namespace is required**: OSS Temporal mTLS does not pin a client to a
  namespace, so a worker that omits it silently lands in ``default``;
* **the payload converter is pydantic's**, on the client, or UUIDs and
  datetimes inside the contracts do not survive the trip.

The wiring tests run whole workflows through the real worker with the real
activities and fake catalogs: an ingest (with a fake NAS), and stage 1 across
the orchestrator and the processor's real workflow and kernel. Stubbed
activities cannot catch a name the workflow calls that nothing registers, or a
payload that does not round-trip -- these do.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_api.clustering_store import ClusteringCandidate
from fishsense_services_api.ingest_store import (
    ContentOverlap,
    RegisteredCapture,
    ResolvedDevice,
)
from fishsense_services_contracts import PROCESSOR_LIGHT_TASK_QUEUE
from fishsense_services_orchestrator.clustering.activities import (
    ClusteringActivities,
    ClusteringTarget,
)
from fishsense_services_orchestrator.clustering.workflow import (
    ClusterDiveFramesParentWorkflow,
)
from fishsense_services_orchestrator.ingest.activities import IngestActivities
from fishsense_services_orchestrator.ingest.contracts import IngestDiveRequest
from fishsense_services_orchestrator.ingest.nas import NasEntry
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings
from fishsense_services_orchestrator.ingest.workflow import IngestDiveWorkflow
from fishsense_services_orchestrator.settings import (
    OrchestratorSettings,
    TemporalSettings,
)
from fishsense_services_orchestrator.worker import (
    DEFAULT_TASK_QUEUE,
    build_worker,
    connect_options,
)
from fishsense_services_processor.clustering.activities import cluster_dive_frames
from fishsense_services_processor.clustering.workflow import (
    DiveFrameClusteringWorkflow,
)

from ._tiff_builder import build_orf

ROOT = "/fishsense_data/REEF/data"
FOLDER = "2024.06.20.REEF/082929_FishModels_FSL07"
SERIAL = "BJ6C67989"
TENANT = uuid.uuid4()
DEVICE = uuid.uuid4()


# -- settings ------------------------------------------------------------------


def test_the_namespace_is_required(monkeypatch):
    """Without it the worker would silently serve ``default`` -- v1's lesson."""
    monkeypatch.delenv("FISHSENSE_TEMPORAL_NAMESPACE", raising=False)

    with pytest.raises(ValidationError, match="namespace"):
        TemporalSettings()


def test_temporal_settings_read_the_environment_with_safe_defaults(monkeypatch):
    monkeypatch.setenv("FISHSENSE_TEMPORAL_NAMESPACE", "fishsense")

    settings = TemporalSettings()

    assert settings.address == "localhost:7233"
    assert settings.namespace == "fishsense"
    # Not v1's `fishsense_api_queue`: Temporal is shared, and during the
    # rehearsal a v1 worker on the same queue would take v2's tasks.
    assert settings.task_queue == DEFAULT_TASK_QUEUE == "fishsense_orchestrator"
    assert settings.client_cert is None


def test_a_client_cert_without_its_key_fails_at_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("FISHSENSE_TEMPORAL_NAMESPACE", "fishsense")
    monkeypatch.setenv("FISHSENSE_TEMPORAL_CLIENT_CERT", str(tmp_path / "c.pem"))

    with pytest.raises(ValidationError, match="together"):
        TemporalSettings()


def test_the_orchestrator_settings_hold_its_identity_and_database(monkeypatch):
    monkeypatch.setenv("FISHSENSE_DATABASE_URL", "postgresql+asyncpg://u:secret@db/x")
    monkeypatch.setenv("FISHSENSE_ORCHESTRATOR_SUB", "service:fishsense-orchestrator")

    settings = OrchestratorSettings()

    assert settings.orchestrator_sub == "service:fishsense-orchestrator"
    assert "secret" not in repr(settings)


# -- the connection ------------------------------------------------------------


def test_connect_options_carry_the_namespace_and_the_pydantic_converter():
    options = connect_options(TemporalSettings(namespace="fishsense"))

    assert options["target_host"] == "localhost:7233"
    assert options["namespace"] == "fishsense"
    assert options["data_converter"] is pydantic_data_converter
    assert options["tls"] is False


def test_tls_is_built_from_the_certificate_files(tmp_path: Path):
    for name in ("cert.pem", "key.pem", "ca.pem"):
        (tmp_path / name).write_bytes(name.encode())
    settings = TemporalSettings(
        namespace="fishsense",
        client_cert=tmp_path / "cert.pem",
        client_private_key=tmp_path / "key.pem",
        server_root_ca_cert=tmp_path / "ca.pem",
        domain="temporal.example",
    )

    tls = connect_options(settings)["tls"]

    assert tls.client_cert == b"cert.pem"
    assert tls.client_private_key == b"key.pem"
    assert tls.server_root_ca_cert == b"ca.pem"
    assert tls.domain == "temporal.example"


# -- the wiring: a whole ingest through the real worker -------------------------


class _Catalog:
    """Just enough database for one ingest, in memory."""

    def __init__(self):
        self.captures: dict[str, datetime] = {}
        self.finalized = None

    async def resolve_tenant(self, slug):
        return TENANT if slug == "lab" else None

    async def resolve_device(self, tenant_id, serial):
        return ResolvedDevice(DEVICE, "FSL-07", True) if serial == SERIAL else None

    async def dive_by_path(self, tenant_id, path):
        return None

    async def dives_with_leaf(self, tenant_id, leaf):
        return []

    async def slate_template(self, name):
        return None

    async def create_dive(self, tenant_id, **dive):
        self.dive_id = uuid.uuid4()
        return self.dive_id

    async def registered_captures(self, tenant_id, dive_id):
        return dict(self.captures)

    async def register_capture(self, tenant_id, **capture):
        self.captures[capture["source_path"]] = capture["captured_at"]
        return RegisteredCapture(uuid.uuid4(), True)

    async def finalize_dive(self, tenant_id, dive_id, *, priority, dived_at):
        self.finalized = (dive_id, priority, dived_at)

    async def content_overlap(self, tenant_id, dive_id):
        return [ContentOverlap(uuid.uuid4(), "backup/x", 1, 0.5)]


def _nas(names):
    frame = build_orf(date_time="2024:08:21 08:56:51", serial_number=SERIAL,
                      artist="FSL-07") + b"\0" * 10_000  # fmt: skip
    nas = MagicMock()
    nas.list_dir.side_effect = lambda *, folder_path: [
        NasEntry(path=f"{folder_path}/{n}", name=n, is_dir=False, size=len(frame))
        for n in names
    ]
    nas.download_range.side_effect = lambda **_: frame

    def _download_to(*, src_path, dest_dir):
        Path(dest_dir, src_path.rsplit("/", 1)[-1]).write_bytes(frame)

    nas.download_to.side_effect = _download_to
    return nas


async def test_a_whole_ingest_runs_through_the_real_worker():
    catalog = _Catalog()
    activities = IngestActivities(
        nas_settings=NasSettings(url="https://nas.test:6021", username="u",
                                 password="p", raw_root_path=ROOT),
        nas_client_factory=lambda: _nas(["P1.ORF", "P2.ORF"]),
        catalog=catalog,
    )  # fmt: skip

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with build_worker(
            env.client,
            ingest=activities,
            clustering=ClusteringActivities(catalog=_ClusteringCatalog()),
            task_queue="wiring",
        ):
            report = await env.client.execute_workflow(
                IngestDiveWorkflow.run,
                IngestDiveRequest(tenant="lab", dive_path=FOLDER, self_calibrates=True),
                id=f"wiring-{uuid.uuid4()}",
                task_queue="wiring",
            )

    assert report.committed is True, report
    assert report.dive_id == catalog.dive_id
    assert report.registered == 2
    assert set(catalog.captures) == {f"{FOLDER}/P1.ORF", f"{FOLDER}/P2.ORF"}
    assert catalog.finalized == (
        catalog.dive_id,
        "high",
        datetime(2024, 8, 21, 8, 56, 51, tzinfo=timezone.utc),
    )
    assert report.duplicate_overlap[0].containment == 0.5
    assert report.preflight.resolved_device_id == DEVICE


class _ClusteringCatalog:
    """One tenant, one dive in the stage-1 cohort, two bursts of frames."""

    def __init__(self):
        self.dive = uuid.uuid4()
        base = datetime(2024, 8, 21, 8, 0, tzinfo=timezone.utc)
        self.captures = [
            (
                uuid.UUID(int=10 * burst + i),
                base + timedelta(minutes=10 * burst, seconds=i),
            )
            for burst in range(2)
            for i in range(3)
        ]
        self.persisted = None

    async def member_tenants(self):
        return [TENANT]

    async def next_dive_for_clustering(self, tenant_id):
        if self.persisted is not None:
            return None
        return ClusteringCandidate(self.dive, datetime.now(timezone.utc))

    async def canonical_capture_times(self, tenant_id, dive_id):
        return self.captures

    async def persist_prediction_clusters(self, tenant_id, dive_id, clusters):
        self.persisted = (tenant_id, dive_id, clusters)
        return len(clusters)


async def test_stage_1_runs_across_the_orchestrator_and_the_processor():
    """The orchestrator's real parent and activities, the processor's real
    workflow and kernel, on their real queues: the contract between the two
    packages, exercised the way it deploys."""
    catalog = _ClusteringCatalog()
    ingest = IngestActivities(
        nas_settings=NasSettings(url="https://nas.test:6021", username="u",
                                 password="p", raw_root_path=ROOT),
        nas_client_factory=lambda: None,
        catalog=None,
    )  # fmt: skip

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with (
            build_worker(
                env.client,
                ingest=ingest,
                clustering=ClusteringActivities(catalog=catalog),
                task_queue="wiring",
            ),
            Worker(
                env.client,
                task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
                workflows=[DiveFrameClusteringWorkflow],
                activities=[cluster_dive_frames],
            ),
        ):
            target = await env.client.execute_workflow(
                ClusterDiveFramesParentWorkflow.run,
                id=f"wiring-{uuid.uuid4()}",
                task_queue="wiring",
            )

    assert target == ClusteringTarget(tenant_id=TENANT, dive_id=catalog.dive)
    tenant, dive, clusters = catalog.persisted
    assert (tenant, dive) == (TENANT, catalog.dive)
    assert sorted(sorted(c.int for c in cluster) for cluster in clusters) == [
        [0, 1, 2],
        [10, 11, 12],
    ]
