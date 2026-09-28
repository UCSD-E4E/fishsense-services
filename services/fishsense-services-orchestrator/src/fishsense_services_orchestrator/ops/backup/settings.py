"""The backup process's configuration, from ``FISHSENSE_BACKUP_*`` and the NAS's
``FISHSENSE_NAS_*``.

Ported from fishsense-lite@77e8f8e5 services/fishsense-backup-worker/src/
fishsense_backup_worker/config.py (`postgres.*`, `backup.*`, `e4e_nas.*`).
Validated at startup, so a misconfigured deployment fails to start rather than
failing at 03:00.

v2 changes:

* **no default database list and no default NAS folder.** v1 defaulted to
  ``["fishsense", "superset", "temporal_db"]`` and ``/fishsense_backups``
  (production overrode both). v2's database has its own name, and during the
  cutover's rollback window v1's dumps sit beside v2's on the same NAS, so a
  guess could back up the wrong database, or prune v1's dumps as its own.
  Production names both: v2's database and Superset's;
* the queue, schedule id and (fixed) workflow id are v2's own, so a v2 worker
  never takes v1's backup on the shared Temporal (PLAN.md §6.5);
* the database credential is a role that bypasses row-level security (see
  deploy/local/initdb/02-backup-role.sh): v1's ``pg_read_all_data`` alone can't
  dump a schema whose tables force RLS. It is this process's alone -- never the
  orchestrator's.
"""

from typing import List

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["BackupNasSettings", "BackupSettings", "DEFAULT_TASK_QUEUE"]

#: Not v1's ``fishsense_backup_queue``: see the module docstring.
DEFAULT_TASK_QUEUE = "fishsense_backup"


class BackupSettings(BaseSettings):
    """What is dumped, as whom, where to, how many are kept, and when."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_BACKUP_")

    database_host: str
    database_port: int = 5432
    database_user: str
    database_password: SecretStr
    #: JSON in the environment: ``'["fishsense_services", "superset"]'``.
    databases: List[str]
    #: The NAS folder dumps land under, as ``{root}/{database}/{time}.dump``.
    nas_root_path: str
    retention_count: int = Field(default=14, gt=0)
    task_queue: str = DEFAULT_TASK_QUEUE
    schedule_id: str = "backup-databases"
    #: v1's: daily at 03:00 UTC.
    schedule_cron: str = "0 3 * * *"


class BackupNasSettings(BaseSettings):
    """The NAS connection (``FISHSENSE_NAS_*``, as the orchestrator names it).
    The backup writes, so it has its own client; the orchestrator's reads."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_NAS_")

    #: Must include hostname and port, e.g. ``https://nas.example:6021``.
    url: str
    username: str
    password: SecretStr
