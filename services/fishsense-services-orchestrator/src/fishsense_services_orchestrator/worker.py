"""The worker process: connect to Temporal and serve the orchestrator's queue.

Ported in shape from fishsense-lite@a8b2c3bc fishsense_api_workflow_worker/
worker.py (connect with TLS and an explicit namespace, then run one worker).
v2 changes: typed settings instead of global Dynaconf; the pydantic payload
converter; activities are bound methods of `IngestActivities`, built once with
their dependencies; the schedules are ensured at startup.

    python -m fishsense_services_orchestrator
"""

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import create_async_engine
from temporalio.client import Client, TLSConfig
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from fishsense_services_api.clustering_store import ClusteringCatalog
from fishsense_services_api.ingest_store import IngestCatalog
from fishsense_services_orchestrator.clustering.activities import (
    ClusteringActivities,
)
from fishsense_services_orchestrator.clustering.workflow import (
    ClusterDiveFramesParentWorkflow,
)
from fishsense_services_orchestrator.ingest.activities import IngestActivities
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings
from fishsense_services_orchestrator.ingest.workflow import IngestDiveWorkflow
from fishsense_services_orchestrator.schedules import ensure_schedules
from fishsense_services_orchestrator.settings import (
    DEFAULT_TASK_QUEUE,
    OrchestratorSettings,
    TemporalSettings,
)

__all__ = [
    "DEFAULT_TASK_QUEUE",
    "WORKFLOWS",
    "build_worker",
    "connect_options",
    "main",
    "run",
]

log = logging.getLogger(__name__)


def connect_options(settings: TemporalSettings) -> dict[str, Any]:
    """Keyword arguments for `Client.connect`."""
    tls: TLSConfig | bool = False
    if settings.client_cert is not None:
        tls = TLSConfig(
            client_cert=settings.client_cert.read_bytes(),
            client_private_key=settings.client_private_key.read_bytes(),
            server_root_ca_cert=(
                settings.server_root_ca_cert.read_bytes()
                if settings.server_root_ca_cert
                else None
            ),
            domain=settings.domain,
        )
    return {
        "target_host": settings.address,
        "namespace": settings.namespace,
        "tls": tls,
        # The contracts are pydantic models carrying UUIDs and datetimes.
        "data_converter": pydantic_data_converter,
    }


#: Every workflow the orchestrator serves.
WORKFLOWS = [IngestDiveWorkflow, ClusterDiveFramesParentWorkflow]


def build_worker(
    client: Client,
    *,
    ingest: IngestActivities,
    clustering: ClusteringActivities,
    task_queue: str,
) -> Worker:
    """Register every workflow and activity the orchestrator serves."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=WORKFLOWS,
        activities=[
            ingest.list_dive_folder,
            ingest.preflight,
            ingest.create_dive,
            ingest.scan_and_register,
            ingest.finalize_dive,
            clustering.select_next_dive_for_clustering,
            clustering.resolve_clustering_inputs,
            clustering.persist_prediction_clusters,
        ],
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    temporal = TemporalSettings()
    orchestrator = OrchestratorSettings()
    nas = NasSettings()

    engine = create_async_engine(
        orchestrator.database_url.get_secret_value(), pool_pre_ping=True
    )
    try:
        sub = orchestrator.orchestrator_sub
        ingest = IngestActivities(
            nas_settings=nas, catalog=IngestCatalog(engine, sub=sub)
        )
        clustering = ClusteringActivities(catalog=ClusteringCatalog(engine, sub=sub))
        options = connect_options(temporal)
        log.info(
            "connecting to Temporal address=%s namespace=%s queue=%s tls=%s",
            temporal.address,
            temporal.namespace,
            temporal.task_queue,
            bool(options["tls"]),
        )
        client = await Client.connect(**options)
        await ensure_schedules(client, task_queue=temporal.task_queue)
        await build_worker(
            client,
            ingest=ingest,
            clustering=clustering,
            task_queue=temporal.task_queue,
        ).run()
    finally:
        await engine.dispose()


def run() -> None:
    asyncio.run(main())
