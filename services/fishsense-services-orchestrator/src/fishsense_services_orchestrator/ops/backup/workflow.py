"""The nightly backup: dump every database to the NAS, then enforce retention.

Ported from fishsense-lite@77e8f8e5 services/fishsense-backup-worker/src/
fishsense_backup_worker/workflows/backup_databases_workflow.py.

Fired by the backup process's schedule (03:00 UTC). One `pg_dump_database` per
database, in parallel (Postgres dumps from an MVCC snapshot, so concurrent
dumps don't interfere), then one `prune_database_backups` per database.

v1's rule, kept: **a database is pruned only after its own dump succeeded**, or
a failed dump plus a prune could leave fewer than `retention_count` good
backups.

v2 changes:

* **one database's failure no longer stops the others.** v1 gathered the dumps
  without `return_exceptions`, so the first failure failed the workflow --
  cancelling the other dumps mid-flight and skipping every prune, though its
  docstring promised the databases were independent. v2 lets every dump
  finish, prunes each database whose dump succeeded, and then fails, naming the
  databases that didn't, so the failure is still seen;
* **the activities' heartbeat is live**: they pump every 30 s, and v2 sets a
  heartbeat timeout, so a dead worker's dump is retried in minutes rather than
  after its two-hour timeout (v1 set none, so its pump was inert).
"""

import asyncio
from datetime import timedelta
from typing import List

from pydantic import BaseModel
from temporalio import workflow
from temporalio.exceptions import ApplicationError

__all__ = [
    "BackupDatabasesInput",
    "BackupDatabasesWorkflow",
    "PgDumpDatabaseInput",
    "PruneDatabaseBackupsInput",
]

_HEARTBEAT_TIMEOUT = timedelta(minutes=2)


class PgDumpDatabaseInput(BaseModel):
    """One database to dump, and the NAS folder dumps go under."""

    db_name: str
    nas_root_path: str


class PruneDatabaseBackupsInput(BaseModel):
    """One database's dumps to prune down to the newest `keep`."""

    db_name: str
    nas_root_path: str
    keep: int


class BackupDatabasesInput(BaseModel):
    """The whole run, as the schedule carries it."""

    databases: List[str]
    nas_root_path: str
    retention_count: int


@workflow.defn
class BackupDatabasesWorkflow:
    # pylint: disable=too-few-public-methods
    @workflow.run
    async def run(self, payload: BackupDatabasesInput) -> None:
        workflow.logger.info(
            "backup start dbs=%s retention=%d",
            payload.databases,
            payload.retention_count,
        )

        outcomes = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "pg_dump_database",
                    PgDumpDatabaseInput(
                        db_name=db, nas_root_path=payload.nas_root_path
                    ),
                    schedule_to_close_timeout=timedelta(hours=2),
                    start_to_close_timeout=timedelta(hours=2),
                    heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                )
                for db in payload.databases
            ],
            return_exceptions=True,
        )
        failed = [
            db
            for db, outcome in zip(payload.databases, outcomes)
            if isinstance(outcome, BaseException)
        ]
        dumped = [db for db in payload.databases if db not in failed]

        # Only now, with every dump finished: never a database whose own dump
        # failed.
        await asyncio.gather(
            *[
                workflow.execute_activity(
                    "prune_database_backups",
                    PruneDatabaseBackupsInput(
                        db_name=db,
                        nas_root_path=payload.nas_root_path,
                        keep=payload.retention_count,
                    ),
                    schedule_to_close_timeout=timedelta(minutes=10),
                    heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                )
                for db in dumped
            ]
        )

        if failed:
            raise ApplicationError(
                f"backup failed for database(s) {failed}; their backups were not "
                f"pruned. {len(dumped)} other database(s) were backed up and pruned."
            )
