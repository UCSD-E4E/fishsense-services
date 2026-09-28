"""The backup's two activities: dump one database to the NAS, and prune one
database's dumps.

Ported from fishsense-lite@77e8f8e5 services/fishsense-backup-worker/src/
fishsense_backup_worker/activities/pg_dump_database.py and
prune_database_backups.py.

v1's rules, kept:

* **dump and upload are one activity.** Split, the dump would live only on the
  worker that ran pg_dump, where a retry elsewhere couldn't see it; together, a
  partial dump never reaches the NAS;
* **each dump gets its own temporary directory.** On 2026-05-03 the three
  per-database dumps resolved to the same ``/tmp/<timestamp>.dump``, and the
  fastest one's cleanup deleted the file from under the others' uploads;
* the prune lists ``{root}/{db}`` and deletes exactly what
  `naming.filenames_to_prune` names, by full path. It is the only code that
  deletes backups, and the nightly backup is the rollback mechanism.

v2 change: the activities are methods of a class given typed settings and a NAS
client factory, instead of reading a global Dynaconf object.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from typing import List

from temporalio import activity

from fishsense_services_orchestrator.ops.backup.heartbeat import heartbeat_pump
from fishsense_services_orchestrator.ops.backup.naming import (
    backup_filename,
    filenames_to_prune,
)
from fishsense_services_orchestrator.ops.backup.nas import NasBackupClient
from fishsense_services_orchestrator.ops.backup.pg_dump import run_pg_dump
from fishsense_services_orchestrator.ops.backup.settings import (
    BackupNasSettings,
    BackupSettings,
)
from fishsense_services_orchestrator.ops.backup.workflow import (
    PgDumpDatabaseInput,
    PruneDatabaseBackupsInput,
)

__all__ = ["BackupActivities"]


def _folder(nas_root_path: str, db_name: str) -> str:
    return f"{nas_root_path.rstrip('/')}/{db_name}"


class BackupActivities:
    def __init__(
        self,
        *,
        settings: BackupSettings,
        nas_settings: BackupNasSettings,
        nas_client_factory: Callable[[], NasBackupClient] | None = None,
    ) -> None:
        self._settings = settings
        self._nas_client_factory = nas_client_factory or (
            lambda: NasBackupClient(
                nas_url=nas_settings.url,
                username=nas_settings.username,
                password=nas_settings.password.get_secret_value(),
            )
        )

    def _dump_and_upload(self, *, db_name: str, nas_root_path: str) -> str:
        """Dump into a temporary directory of its own, upload, clean up.
        Returns the NAS folder, for the log."""
        filename = backup_filename(datetime.now(tz=timezone.utc))
        nas_dir = _folder(nas_root_path, db_name)
        with tempfile.TemporaryDirectory(prefix=f"backup-{db_name}-") as tmpdir:
            # The local basename is what lands on the NAS, so write straight to
            # the canonical name inside this activity's own directory.
            local_path = os.path.join(tmpdir, filename)
            run_pg_dump(
                db_name=db_name,
                host=self._settings.database_host,
                port=self._settings.database_port,
                username=self._settings.database_user,
                password=self._settings.database_password.get_secret_value(),
                output_path=local_path,
            )
            self._nas_client_factory().upload(
                dest_dir=nas_dir, src_file_path=local_path
            )
        return nas_dir

    def _prune(self, *, db_name: str, nas_root_path: str, keep: int) -> List[str]:
        """Delete this database's dumps beyond the newest `keep`; returns the
        filenames deleted."""
        nas = self._nas_client_factory()
        nas_dir = _folder(nas_root_path, db_name)
        to_delete = filenames_to_prune(nas.list_filenames(folder_path=nas_dir), keep)
        for filename in to_delete:
            nas.delete(file_path=f"{nas_dir}/{filename}")
        return to_delete

    @activity.defn(name="pg_dump_database")
    async def pg_dump_database(self, payload: PgDumpDatabaseInput) -> None:
        payload = PgDumpDatabaseInput.model_validate(payload)
        activity.logger.info(
            "pg_dump_database start db=%s nas_root=%s",
            payload.db_name,
            payload.nas_root_path,
        )
        async with heartbeat_pump():
            await asyncio.to_thread(
                self._dump_and_upload,
                db_name=payload.db_name,
                nas_root_path=payload.nas_root_path,
            )
        activity.logger.info("pg_dump_database done db=%s", payload.db_name)

    @activity.defn(name="prune_database_backups")
    async def prune_database_backups(self, payload: PruneDatabaseBackupsInput) -> None:
        # Whatever the converter produced: a plain dict is coerced.
        payload = PruneDatabaseBackupsInput.model_validate(payload)
        activity.logger.info(
            "prune_database_backups start db=%s keep=%d", payload.db_name, payload.keep
        )
        async with heartbeat_pump():
            pruned = await asyncio.to_thread(
                self._prune,
                db_name=payload.db_name,
                nas_root_path=payload.nas_root_path,
                keep=payload.keep,
            )
        activity.logger.info(
            "prune_database_backups done db=%s pruned_count=%d",
            payload.db_name,
            len(pruned),
        )
