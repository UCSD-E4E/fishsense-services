"""The backup's I/O: the NAS client, the dump-and-upload, and the prune.

Ported from fishsense-lite@77e8f8e5 services/fishsense-backup-worker/tests/
test_nas_backup_client.py, test_pg_dump_database_activity.py and
test_prune_database_backups_activity.py. v1's rules, kept:

* the NAS client's surface (`upload`, `list_filenames`, `delete`) propagates
  every underlying failure -- the 2026-05-07 stage-2 incident was silent
  error-swallowing in the old NAS library -- and treats "folder already
  exists" as success;
* **each dump gets its own temporary directory.** On 2026-05-03 three
  concurrent per-database dumps resolved to the same `/tmp/<timestamp>.dump`,
  and the fastest one's cleanup deleted the file under the others' uploads;
* the prune lists `<root>/<db>` (never the root: that would prune across
  databases), deletes exactly what the retention helper names, by full path,
  and nothing when it names nothing. The nightly backup *is* the rollback
  mechanism, and this is the only code that deletes backups.

v2 changes: settings are typed (``FISHSENSE_BACKUP_*`` and the NAS's
``FISHSENSE_NAS_*``) and handed to the activities, instead of a global
Dynaconf object seeded from the environment.
"""

from __future__ import annotations

import os
import threading
from typing import List
from unittest.mock import MagicMock

import pytest
from synology_filestation import AlreadyExists, NoSuchFile, SidNotFound
from temporalio.testing import ActivityEnvironment

from fishsense_services_orchestrator.ops.backup import activities as sut
from fishsense_services_orchestrator.ops.backup import nas as nas_mod
from fishsense_services_orchestrator.ops.backup.settings import (
    BackupNasSettings,
    BackupSettings,
)
from fishsense_services_orchestrator.ops.backup.workflow import (
    PgDumpDatabaseInput,
    PruneDatabaseBackupsInput,
)


def _settings() -> BackupSettings:
    return BackupSettings(
        database_host="postgres",
        database_user="fishsense_backup",
        database_password="secret",
        databases=["fishsense_v2", "superset"],
        nas_root_path="/backups",
    )


def _nas_settings() -> BackupNasSettings:
    return BackupNasSettings(
        url="https://nas.example.test:6021", username="u", password="p"
    )


def _activities(nas) -> sut.BackupActivities:
    return sut.BackupActivities(
        settings=_settings(),
        nas_settings=_nas_settings(),
        nas_client_factory=lambda: nas,
    )


# -- the NAS client ---------------------------------------------------------------


def _client(monkeypatch, fake):
    monkeypatch.setattr(nas_mod.Client, "login", lambda *a, **kw: fake)
    return nas_mod.NasBackupClient(
        nas_url="https://nas.example.com:6021", username="u", password="p"
    )


def test_external_shape_preserved_for_activity_call_sites(monkeypatch):
    """The activities call these names with these keywords, through
    `asyncio.to_thread`, where a rename can't be caught at import time."""
    fake = MagicMock(name="synology_filestation.Client")
    fake.list_dir.return_value = []
    client = _client(monkeypatch, fake)

    client.upload(dest_dir="/foo/bar", src_file_path="/tmp/x.dump")
    client.list_filenames(folder_path="/foo/bar")
    client.delete(file_path="/foo/bar/old.dump")


def test_upload_propagates_underlying_failure(monkeypatch):
    fake = MagicMock(name="synology_filestation.Client")
    fake.upload.side_effect = SidNotFound("session expired")
    client = _client(monkeypatch, fake)

    with pytest.raises(SidNotFound):
        client.upload(dest_dir="/foo/bar", src_file_path="/tmp/x.dump")


def test_delete_propagates_underlying_failure(monkeypatch):
    """Pruning relies on a failed delete surfacing, so retention can't
    silently no-op."""
    fake = MagicMock(name="synology_filestation.Client")
    fake.delete.side_effect = NoSuchFile("file not found")
    client = _client(monkeypatch, fake)

    with pytest.raises(NoSuchFile):
        client.delete(file_path="/foo/bar/missing.dump")


def test_ensure_dir_treats_already_exists_as_success(monkeypatch):
    """The folder already existing is the steady state."""
    fake = MagicMock(name="synology_filestation.Client")
    fake.create_folder.side_effect = AlreadyExists("folder exists")
    client = _client(monkeypatch, fake)

    client.upload(dest_dir="/foo/bar", src_file_path="/tmp/x.dump")
    fake.upload.assert_called_once()


def test_list_filenames_returns_basenames(monkeypatch):
    """Retention string-matches on filenames, not paths."""
    fake = MagicMock(name="synology_filestation.Client")
    fake.list_dir.return_value = [
        {"path": "/backups/fishsense/2026-05-01.dump", "isdir": False, "size": 1},
        {"path": "/backups/fishsense/2026-05-02.dump", "isdir": False, "size": 1},
        {"path": "/backups/fishsense/old", "isdir": True, "size": 0},
    ]
    client = _client(monkeypatch, fake)

    assert client.list_filenames(folder_path="/backups/fishsense") == [
        "2026-05-01.dump",
        "2026-05-02.dump",
        "old",
    ]


def test_a_nas_url_without_a_port_is_refused():
    with pytest.raises(ValueError, match="hostname"):
        nas_mod.NasBackupClient(nas_url="https://nas.example.com", username="u", password="p")  # fmt: skip


# -- dump and upload --------------------------------------------------------------


class _UploadingNas:
    """Checks, at upload time, that the dump is really there."""

    def __init__(self):
        self.uploads: List[tuple[str, str]] = []
        self._lock = threading.Lock()

    def upload(self, *, dest_dir, src_file_path):
        assert os.path.exists(
            src_file_path
        ), f"upload of a missing {src_file_path!r}: the tempdir lifecycle is wrong"
        with self._lock:
            self.uploads.append((dest_dir, src_file_path))


def test_each_invocation_uses_an_isolated_tempdir(monkeypatch):
    """The tempdir carries the database's name, so an operator can attribute a
    leftover one (`ls /tmp`) if pg_dump ever wedges."""
    captured: List[dict] = []

    def fake_pg_dump(**kwargs):
        with open(kwargs["output_path"], "wb") as fh:
            fh.write(b"")
        captured.append(kwargs)

    monkeypatch.setattr(sut, "run_pg_dump", fake_pg_dump)
    nas = _UploadingNas()

    _activities(nas)._dump_and_upload(db_name="fishsense_v2", nas_root_path="/backups")

    (call,) = captured
    parent = os.path.basename(os.path.dirname(call["output_path"]))
    assert parent.startswith("backup-fishsense_v2-")
    assert nas.uploads == [("/backups/fishsense_v2", call["output_path"])]


def test_the_dump_connects_as_the_configured_backup_role(monkeypatch):
    captured: List[dict] = []

    def fake_pg_dump(**kwargs):
        open(kwargs["output_path"], "wb").close()  # pylint: disable=consider-using-with
        captured.append(kwargs)

    monkeypatch.setattr(sut, "run_pg_dump", fake_pg_dump)

    _activities(_UploadingNas())._dump_and_upload(
        db_name="superset", nas_root_path="/backups/"
    )

    (call,) = captured
    assert (call["db_name"], call["host"], call["port"]) == ("superset", "postgres", 5432)  # fmt: skip
    assert (call["username"], call["password"]) == ("fishsense_backup", "secret")
    assert os.path.basename(call["output_path"]).endswith("Z.dump")


def test_concurrent_calls_for_three_dbs_all_succeed(monkeypatch):
    """The 2026-05-03 regression: three dumps sitting between pg_dump and the
    upload at once -- the window the old shared path raced on -- must each
    upload their own file."""
    barrier = threading.Barrier(3)

    def fake_pg_dump(*, output_path: str, **_kwargs):
        with open(output_path, "wb") as fh:
            fh.write(b"\xff\xff")
        barrier.wait(timeout=5.0)

    monkeypatch.setattr(sut, "run_pg_dump", fake_pg_dump)
    nas = _UploadingNas()
    activities = _activities(nas)

    threads = [
        threading.Thread(
            target=activities._dump_and_upload,  # pylint: disable=protected-access
            kwargs={"db_name": db, "nas_root_path": "/backups"},
        )
        for db in ("fishsense_v2", "superset", "fishsense")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert all(not t.is_alive() for t in threads), "thread deadlocked"
    paths = [p for _, p in nas.uploads]
    assert len(paths) == 3
    assert len(set(paths)) == 3
    assert len({os.path.dirname(p) for p in paths}) == 3


async def test_the_dump_activity_accepts_its_payload(monkeypatch):
    seen: List[str] = []
    monkeypatch.setattr(
        sut.BackupActivities,
        "_dump_and_upload",
        lambda self, *, db_name, nas_root_path: seen.append(f"{nas_root_path}:{db_name}"),
    )  # fmt: skip

    await ActivityEnvironment().run(
        _activities(None).pg_dump_database,
        PgDumpDatabaseInput(db_name="fishsense_v2", nas_root_path="/backups"),
    )

    assert seen == ["/backups:fishsense_v2"]


# -- prune ------------------------------------------------------------------------


@pytest.fixture
def nas():
    client = MagicMock()
    client.list_filenames.return_value = []
    return client


def _prune(nas, **kwargs):
    return _activities(nas)._prune(**kwargs)  # pylint: disable=protected-access


def test_lists_the_per_database_subdirectory_not_the_root(nas):
    """Listing the root would return every database's files and prune across
    databases."""
    _prune(nas, db_name="fishsense", nas_root_path="/backups", keep=3)

    nas.list_filenames.assert_called_once_with(folder_path="/backups/fishsense")


def test_a_trailing_slash_on_the_root_does_not_produce_a_double_slash(nas):
    """`//` 404s on FileStation."""
    _prune(nas, db_name="fishsense", nas_root_path="/backups/", keep=3)

    nas.list_filenames.assert_called_once_with(folder_path="/backups/fishsense")


def test_deletes_each_pruned_file_by_full_path(nas, monkeypatch):
    nas.list_filenames.return_value = ["a.dump", "b.dump", "c.dump"]
    monkeypatch.setattr(sut, "filenames_to_prune", lambda _files, keep: ["a.dump"])

    pruned = _prune(nas, db_name="superset", nas_root_path="/backups", keep=2)

    nas.delete.assert_called_once_with(file_path="/backups/superset/a.dump")
    assert pruned == ["a.dump"]


def test_deletes_nothing_when_retention_says_nothing_to_prune(nas, monkeypatch):
    """The steady state on a fresh install."""
    nas.list_filenames.return_value = ["a.dump", "b.dump"]
    monkeypatch.setattr(sut, "filenames_to_prune", lambda _files, keep: [])

    assert _prune(nas, db_name="fishsense", nas_root_path="/backups", keep=5) == []
    nas.delete.assert_not_called()


def test_deletes_only_what_the_retention_helper_returned(nas, monkeypatch):
    nas.list_filenames.return_value = [f"{i}.dump" for i in range(10)]
    monkeypatch.setattr(
        sut, "filenames_to_prune", lambda _files, keep: ["0.dump", "1.dump"]
    )

    _prune(nas, db_name="fishsense", nas_root_path="/backups", keep=8)

    deleted = [c.kwargs["file_path"] for c in nas.delete.call_args_list]
    assert deleted == ["/backups/fishsense/0.dump", "/backups/fishsense/1.dump"]


def test_passes_keep_through_to_the_retention_helper(nas, monkeypatch):
    seen: List[int] = []

    def _spy(files, keep):  # pylint: disable=unused-argument
        seen.append(keep)
        return []

    monkeypatch.setattr(sut, "filenames_to_prune", _spy)

    _prune(nas, db_name="fishsense", nas_root_path="/backups", keep=14)

    assert seen == [14]


def test_only_the_listed_files_are_considered(nas, monkeypatch):
    nas.list_filenames.return_value = ["x.dump", "y.dump"]
    seen: List[List[str]] = []

    def _spy(files, keep):  # pylint: disable=unused-argument
        seen.append(list(files))
        return []

    monkeypatch.setattr(sut, "filenames_to_prune", _spy)

    _prune(nas, db_name="fishsense", nas_root_path="/backups", keep=3)

    assert seen == [["x.dump", "y.dump"]]


async def test_the_prune_activity_accepts_an_already_typed_payload(nas):
    await ActivityEnvironment().run(
        _activities(nas).prune_database_backups,
        PruneDatabaseBackupsInput(db_name="fishsense", nas_root_path="/backups", keep=3),
    )  # fmt: skip

    nas.list_filenames.assert_called_once_with(folder_path="/backups/fishsense")


async def test_the_prune_activity_validates_a_dict_payload(nas):
    """Whatever the data converter produced: a plain dict is coerced, not
    attribute-errored."""
    await ActivityEnvironment().run(
        _activities(nas).prune_database_backups,
        {"db_name": "superset", "nas_root_path": "/backups", "keep": 4},
    )

    nas.list_filenames.assert_called_once_with(folder_path="/backups/superset")


# -- the heartbeat pump -----------------------------------------------------------


async def test_the_pump_heartbeats_while_the_blocking_work_runs():
    """pg_dump and the NAS calls block in a thread, so the activity can't
    heartbeat inline; the pump does, on a ticker, and stops with the block."""
    import asyncio  # pylint: disable=import-outside-toplevel

    from fishsense_services_orchestrator.ops.backup.heartbeat import heartbeat_pump

    beats: list = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *d: beats.append(d)

    async def body():
        async with heartbeat_pump(interval_seconds=0.01):
            await asyncio.sleep(0.1)
        settled = len(beats)
        await asyncio.sleep(0.05)
        return settled

    settled = await env.run(body)

    assert settled >= 3
    assert len(beats) == settled, "the pump outlived its block"
