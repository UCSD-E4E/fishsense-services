"""The worker process: connect to Temporal and serve the orchestrator's queue.

Ported in shape from fishsense-lite@a8b2c3bc fishsense_api_workflow_worker/
worker.py (connect with TLS and an explicit namespace, then run one worker).
v2 changes: typed settings instead of global Dynaconf; the pydantic payload
converter; the stages declare themselves (see `registry`), and their activities
are bound methods built once from the shared dependencies; the schedules are
ensured at startup.

    python -m fishsense_services_orchestrator
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from sqlalchemy.ext.asyncio import create_async_engine
from temporalio.client import Client
from temporalio.worker import Worker

from fishsense_services_contracts.temporal import connect_options
from fishsense_services_orchestrator.registry import Deps, stages
from fishsense_services_orchestrator.schedules import ensure_schedules
from fishsense_services_orchestrator.settings import (
    DEFAULT_TASK_QUEUE,
    OrchestratorSettings,
    TemporalSettings,
)

__all__ = [
    "DEFAULT_TASK_QUEUE",
    "WORKFLOWS",
    "build_activities",
    "build_worker",
    "connect_options",
    "main",
    "run",
]

log = logging.getLogger(__name__)


#: Every workflow the orchestrator serves: every stage's (see `registry`).
WORKFLOWS = [workflow for stage in stages() for workflow in stage.workflows]


def build_activities(deps: Deps) -> list:
    """Every stage's activities, built once."""
    return [a for stage in stages() for a in stage.build_activities(deps)]


def build_worker(
    client: Client,
    *,
    activities: Sequence[Callable],
    task_queue: str,
    workflows: Sequence[type] = tuple(WORKFLOWS),
) -> Worker:
    """Register the workflows and activities the orchestrator serves."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=list(workflows),
        activities=list(activities),
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    temporal = TemporalSettings()
    orchestrator = OrchestratorSettings()

    engine = create_async_engine(
        orchestrator.database_url.get_secret_value(), pool_pre_ping=True
    )
    try:
        activities = build_activities(
            Deps(engine=engine, sub=orchestrator.orchestrator_sub)
        )
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
            client, activities=activities, task_queue=temporal.task_queue
        ).run()
    finally:
        await engine.dispose()


def run() -> None:
    asyncio.run(main())
