"""The backup process: register the nightly schedule, then serve its queue.

Ported from fishsense-lite@77e8f8e5 services/fishsense-backup-worker/src/
fishsense_backup_worker/worker.py.

    python -m fishsense_services_orchestrator.ops.backup

**A process of its own, not an orchestrator stage.** Backing up v2's database
takes a role that reads every tenant's rows (`BYPASSRLS`, see settings), and
the orchestrator acts for a tenant only as a member of it, never with a
bypass (PLAN.md §9.11). So, as in v1, the backup is its own worker, on its own
queue, holding the one credential nobody else has, and the NAS write access
nobody else needs. It ships in the orchestrator's distribution (its
dependencies are a subset of the orchestrator's) and runs from the image's
`backup` target, which adds `pg_dump` -- the way `migrate` runs from the API's
image with the owner's credential.

v2 changes: typed settings (``FISHSENSE_BACKUP_*``, ``FISHSENSE_NAS_*``, the
shared ``FISHSENSE_TEMPORAL_*``) instead of Dynaconf; the shared Temporal
connection (namespace required, the pydantic converter); no thread pool, since
both activities are async and run their blocking work in threads themselves.
"""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from fishsense_services_contracts.temporal import TemporalConnection, connect_options
from fishsense_services_orchestrator.ops.backup.activities import BackupActivities
from fishsense_services_orchestrator.ops.backup.schedule import (
    build_backup_schedule,
    ensure_schedule,
)
from fishsense_services_orchestrator.ops.backup.settings import (
    BackupNasSettings,
    BackupSettings,
)
from fishsense_services_orchestrator.ops.backup.workflow import (
    BackupDatabasesWorkflow,
)

__all__ = ["main", "run"]

log = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = BackupSettings()
    activities = BackupActivities(settings=settings, nas_settings=BackupNasSettings())
    temporal = TemporalConnection()

    client = await Client.connect(**connect_options(temporal))
    await ensure_schedule(
        client,
        schedule_id=settings.schedule_id,
        schedule=build_backup_schedule(
            databases=list(settings.databases),
            nas_root_path=settings.nas_root_path,
            retention_count=settings.retention_count,
            cron_expression=settings.schedule_cron,
            task_queue=settings.task_queue,
        ),
    )
    worker = Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[BackupDatabasesWorkflow],
        activities=[activities.pg_dump_database, activities.prune_database_backups],
    )
    log.info(
        "backup worker started: queue=%s schedule=%s dbs=%s",
        settings.task_queue,
        settings.schedule_id,
        list(settings.databases),
    )
    await worker.run()


def run() -> None:
    asyncio.run(main())
