"""The processor's worker process: serve one role, on its queue.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/worker.py. v1
ran one role per Deployment from one codebase, chosen by settings; v2 keeps
that shape. The role comes from ``FISHSENSE_PROCESSOR_ROLE`` (``per_image``,
``light`` or ``gpu``; see `registry`), and what the role serves comes from the
stages that declare it. It connects with the shared Temporal settings
(``fishsense_services_contracts.temporal``) and has no database or NAS access
-- everything it needs arrives in the payload.

Kept from v1: the per-role activity cap, and a graceful drain on SIGTERM. The
drain matters more in v2: the orchestrator tears the processor down by
deleting its Deployment, which SIGTERMs a pod that may be mid-activity.

    FISHSENSE_PROCESSOR_ROLE=light python -m fishsense_services_processor
"""

import asyncio
import logging
import signal
from collections.abc import Iterable
from datetime import timedelta
from typing import Literal

from pydantic import PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict
from temporalio.client import Client
from temporalio.worker import Worker

from fishsense_services_contracts.temporal import TemporalConnection, connect_options
from fishsense_services_processor.registry import Stage, registration_for_role

__all__ = [
    "GRACEFUL_SHUTDOWN_TIMEOUT",
    "ProcessorSettings",
    "build_worker",
    "main",
    "run",
]

log = logging.getLogger(__name__)

#: How long in-flight activities get to finish when the pod is told to stop
#: (v1's 30 s). Every manifest's ``terminationGracePeriodSeconds`` is longer.
#: Without it the work in progress is cancelled at once and re-run -- safe,
#: since activities are idempotent, but it throws away a nearly done image.
GRACEFUL_SHUTDOWN_TIMEOUT = timedelta(seconds=30)


class ProcessorSettings(BaseSettings):
    """Which role this process serves, from ``FISHSENSE_PROCESSOR_*``."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_PROCESSOR_")

    #: Required: a pod whose manifest forgot it must fail to start, not serve
    #: some other queue while its own sits unpolled.
    role: Literal["per_image", "light", "gpu"]
    #: Lowers (or raises) the role's cap; see
    #: `registry.ROLE_MAX_CONCURRENT_ACTIVITIES` before raising one.
    max_concurrent_activities: PositiveInt | None = None


def build_worker(
    client: Client,
    *,
    role: str,
    stages: Iterable[Stage] | None = None,
    max_concurrent_activities: int | None = None,
) -> Worker:
    """The Temporal worker for one role."""
    registration = registration_for_role(role, stages)
    if not registration.workflows and not registration.activities:
        raise ValueError(
            f"the {role!r} role has nothing registered: no ported stage declares it"
        )
    return Worker(
        client,
        task_queue=registration.task_queue,
        workflows=list(registration.workflows),
        activities=list(registration.activities),
        max_concurrent_activities=(
            max_concurrent_activities or registration.max_concurrent_activities
        ),
        graceful_shutdown_timeout=GRACEFUL_SHUTDOWN_TIMEOUT,
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = ProcessorSettings()
    temporal = TemporalConnection()
    options = connect_options(temporal)
    client = await Client.connect(**options)
    worker = build_worker(
        client,
        role=settings.role,
        max_concurrent_activities=settings.max_concurrent_activities,
    )
    log.info(
        "serving role=%s queue=%s on Temporal address=%s namespace=%s tls=%s",
        settings.role,
        worker.task_queue,
        temporal.address,
        temporal.namespace,
        bool(options["tls"]),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with worker:
        await stop.wait()
        log.info(
            "shutdown signal received; draining (graceful_shutdown_timeout=%s)",
            GRACEFUL_SHUTDOWN_TIMEOUT,
        )


def run() -> None:
    asyncio.run(main())
