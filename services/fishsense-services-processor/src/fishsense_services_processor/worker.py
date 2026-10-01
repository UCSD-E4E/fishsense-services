"""The processor's worker process.

v1's data-worker ran one image per role (per-image, light, GPU) from one
codebase (fishsense-lite@a8b2c3bc roles.py); v2 keeps that shape and starts with
the light role, whose only work so far is stage 1. It connects with the shared
Temporal settings (``fishsense_services_contracts.temporal``) and has no
database or NAS access -- everything it needs arrives in the payload.

    python -m fishsense_services_processor
"""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from fishsense_services_contracts import PROCESSOR_LIGHT_TASK_QUEUE
from fishsense_services_contracts.temporal import TemporalConnection, connect_options
from fishsense_services_processor.clustering.activities import cluster_dive_frames
from fishsense_services_processor.clustering.workflow import (
    DiveFrameClusteringWorkflow,
)

__all__ = ["build_light_worker", "main", "run"]

log = logging.getLogger(__name__)


def build_light_worker(client: Client) -> Worker:
    """The stages that hold no image bytes: rows in, numpy, rows out."""
    return Worker(
        client,
        task_queue=PROCESSOR_LIGHT_TASK_QUEUE,
        workflows=[DiveFrameClusteringWorkflow],
        activities=[cluster_dive_frames],
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    temporal = TemporalConnection()
    options = connect_options(temporal)
    log.info(
        "connecting to Temporal address=%s namespace=%s queue=%s tls=%s",
        temporal.address,
        temporal.namespace,
        PROCESSOR_LIGHT_TASK_QUEUE,
        bool(options["tls"]),
    )
    client = await Client.connect(**options)
    await build_light_worker(client).run()


def run() -> None:
    asyncio.run(main())
