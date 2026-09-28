"""A subprocess wrapper around `pg_dump`.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-backup-worker/
src/fishsense_backup_worker/pg_dump.py.

The command builder is separate from the runner so it can be tested without the
binary, and so the password-via-env contract is pinned (a password in argv
would show in `ps`). The `pg_dump` must be at least the server's major version
(the backup image installs the matching client).
"""

import logging
import subprocess
from typing import Dict, List, Tuple

__all__ = ["build_pg_dump_command", "run_pg_dump"]

_log = logging.getLogger(__name__)


def build_pg_dump_command(
    *,
    db_name: str,
    host: str,
    port: int,
    username: str,
    password: str,
    output_path: str,
) -> Tuple[List[str], Dict[str, str]]:
    """(argv, env) for the pg_dump invocation.

    `-Fc` (custom): what `pg_restore` expects, and v1's convention. The password
    goes in PGPASSWORD, NOT argv; nothing else goes in the env, so a stray
    PGHOST or PGDATABASE can't change what is dumped.
    """
    cmd = [
        "pg_dump",
        "-Fc",
        "-h",
        host,
        "-p",
        str(port),
        "-U",
        username,
        "-d",
        db_name,
        "-f",
        output_path,
    ]
    env = {"PGPASSWORD": password}
    return cmd, env


def run_pg_dump(
    *,
    db_name: str,
    host: str,
    port: int,
    username: str,
    password: str,
    output_path: str,
    timeout_s: float = 3600.0,
) -> None:
    """Run pg_dump. Raises CalledProcessError on a non-zero exit, with the
    captured stderr, so the reason (auth, a missing role, a network error)
    reaches the failure Temporal records. A partial dump is left in place for a
    postmortem; the caller's temporary directory removes it."""
    cmd, env = build_pg_dump_command(
        db_name=db_name,
        host=host,
        port=port,
        username=username,
        password=password,
        output_path=output_path,
    )
    _log.info(
        "pg_dump start db=%s host=%s:%d user=%s out=%s",
        db_name,
        host,
        port,
        username,
        output_path,
    )
    result = subprocess.run(
        cmd,
        env=env,
        timeout=timeout_s,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        _log.error(
            "pg_dump failed db=%s rc=%d stderr=%s",
            db_name,
            result.returncode,
            result.stderr,
        )
        raise subprocess.CalledProcessError(
            returncode=result.returncode,
            cmd=cmd,
            output=result.stdout,
            stderr=result.stderr,
        )
    _log.info("pg_dump done db=%s out=%s", db_name, output_path)
