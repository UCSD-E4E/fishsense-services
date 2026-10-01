"""The backup's pure parts: dump filenames, retention, the pg_dump command.

Ported from fishsense-lite@77e8f8e5 services/fishsense-backup-worker/tests/
test_backup_naming_and_pruning.py and test_pg_dump_command.py, unchanged in
substance: the naming must sort lexicographically as it sorts in time (so a
sort and a slice find the newest N) and be unambiguous about the zone (always
UTC); pruning ignores foreign files and refuses a non-positive `keep`; the
dump is `-Fc` (what `pg_restore` expects), and the password goes in
PGPASSWORD, never argv (argv is visible in `ps`).
"""

import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from subprocess import CompletedProcess

import pytest

from fishsense_services_orchestrator.ops.backup.naming import (
    backup_filename,
    filenames_to_prune,
)
from fishsense_services_orchestrator.ops.backup.pg_dump import (
    build_pg_dump_command,
    run_pg_dump,
)

# -- backup_filename --------------------------------------------------------------


def test_backup_filename_uses_iso_utc_timestamp_with_dump_extension():
    moment = datetime(2026, 4, 28, 3, 0, 0, tzinfo=timezone.utc)
    assert backup_filename(moment) == "2026-04-28T03-00-00Z.dump"


def test_backup_filename_normalizes_aware_datetime_to_utc():
    """Pacific 21:00 -> UTC 04:00 (during PDT)."""
    pdt = timezone(timedelta(hours=-7))
    moment = datetime(2026, 4, 27, 21, 0, 0, tzinfo=pdt)
    assert backup_filename(moment) == "2026-04-28T04-00-00Z.dump"


def test_backup_filename_rejects_naive_datetime():
    """Naive is ambiguous about the zone: refuse it, so a UTC/local mix can't
    reach the NAS listing."""
    with pytest.raises(ValueError):
        backup_filename(datetime(2026, 4, 28, 3, 0, 0))


def test_backup_filenames_sort_chronologically_as_strings():
    """The property pruning depends on: lexicographic sort = time sort."""
    a = backup_filename(datetime(2026, 4, 28, 3, 0, tzinfo=timezone.utc))
    b = backup_filename(datetime(2026, 4, 29, 3, 0, tzinfo=timezone.utc))
    c = backup_filename(datetime(2026, 5, 1, 3, 0, tzinfo=timezone.utc))
    assert sorted([c, a, b]) == [a, b, c]


# -- filenames_to_prune -----------------------------------------------------------


def _names_for_days(n: int):
    base = datetime(2026, 4, 1, 3, 0, tzinfo=timezone.utc)
    return [backup_filename(base + timedelta(days=i)) for i in range(n)]


def test_filenames_to_prune_returns_empty_when_under_limit():
    assert filenames_to_prune(_names_for_days(5), keep=14) == []


def test_filenames_to_prune_returns_empty_at_exact_limit():
    assert filenames_to_prune(_names_for_days(14), keep=14) == []


def test_filenames_to_prune_keeps_most_recent_n_drops_the_rest():
    files = _names_for_days(20)
    to_prune = filenames_to_prune(files, keep=14)
    assert to_prune == files[:6]
    assert [f for f in files if f not in to_prune] == files[-14:]


def test_filenames_to_prune_handles_unsorted_input():
    """The NAS listing isn't guaranteed sorted."""
    files = _names_for_days(20)
    assert sorted(filenames_to_prune(list(reversed(files)), keep=14)) == files[:6]


def test_filenames_to_prune_ignores_unknown_filenames():
    """Stray uploads, a README, a crashed run's partial upload: not candidates.
    Leave them for a human."""
    files = _names_for_days(20) + ["README.md", "junk.txt"]
    to_prune = filenames_to_prune(files, keep=14)
    assert "README.md" not in to_prune
    assert "junk.txt" not in to_prune
    assert len(to_prune) == 6


def test_filenames_to_prune_rejects_zero_or_negative_keep():
    """keep=0 would delete everything on the next run -- almost certainly a
    config typo. Refuse loudly."""
    with pytest.raises(ValueError):
        filenames_to_prune(_names_for_days(5), keep=0)
    with pytest.raises(ValueError):
        filenames_to_prune(_names_for_days(5), keep=-1)


# -- the pg_dump command ----------------------------------------------------------

#: Made up per run, so no credential-shaped literal sits in the source for a
#: secret scanner to flag (GitGuardian did, on a fixed fake one).
PASSWORD = secrets.token_hex(8)


def _cmd(**overrides):
    fields = {
        "db_name": "fishsense",
        "host": "postgres",
        "port": 5432,
        "username": "backup_user",
        "password": PASSWORD,
        "output_path": "/tmp/out.dump",
        **overrides,
    }
    return build_pg_dump_command(**fields)


def test_uses_custom_format():
    cmd, _env = _cmd()
    assert "-Fc" in cmd


def test_passes_db_name_via_dash_d():
    cmd, _ = _cmd(db_name="superset")
    assert cmd[cmd.index("-d") + 1] == "superset"


def test_passes_connection_args():
    cmd, _ = _cmd(host="postgres.internal", port=5433)
    assert cmd[cmd.index("-h") + 1] == "postgres.internal"
    assert cmd[cmd.index("-p") + 1] == "5433"
    assert cmd[cmd.index("-U") + 1] == "backup_user"


def test_password_goes_in_env_not_argv():
    """argv is visible in `ps`."""
    other = secrets.token_hex(8)
    cmd, env = _cmd(password=other)
    assert other not in cmd
    assert env.get("PGPASSWORD") == other


def test_writes_to_specified_output_path():
    cmd, _ = _cmd(output_path="/var/backups/fishsense.dump")
    assert cmd[cmd.index("-f") + 1] == "/var/backups/fishsense.dump"


def test_first_arg_is_pg_dump_executable():
    cmd, _ = _cmd()
    assert cmd[0] == "pg_dump"


def test_env_does_not_set_other_pg_vars():
    """Connection args are explicit; a stray PGHOST or PGDATABASE leaking in
    from the parent could change what is dumped."""
    _, env = _cmd()
    assert set(env) == {"PGPASSWORD"}


def test_failure_surfaces_stderr_in_exception(monkeypatch, tmp_path):
    """A non-zero exit raises with the captured stderr, so Temporal's failure
    carries the real reason (auth, a missing role) and not just the code."""

    def fake_run(cmd, **kwargs):
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        return CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr='pg_dump: error: connection to server at "postgres" '
            'failed: FATAL:  password authentication failed for user "backup"\n',
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        run_pg_dump(
            db_name="fishsense",
            host="postgres",
            port=5432,
            username="backup",
            password=secrets.token_hex(8),
            output_path=str(tmp_path / "out.dump"),
        )

    assert excinfo.value.returncode == 1
    assert "password authentication failed" in (excinfo.value.stderr or "")
