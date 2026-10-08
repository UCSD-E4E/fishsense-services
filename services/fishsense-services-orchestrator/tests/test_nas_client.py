# pylint: disable=protected-access
"""Ported from fishsense-lite@a8b2c3bc
services/fishsense-api-workflow-worker/tests/test_nas_client.py -- only the import
path changed.

Unit tests for the api-worker's NasClient.

Pins the 2026-05-07 stage 2 incident: DSM returned 200 OK with a
JSON-error body after the staging activity's idle session timed out
(~30 min default DSM SID TTL). The previous synology-api 0.8.x
`get_file(mode='download')` implementation streamed the JSON-error
body to disk and returned a tuple instead of raising; the surrounding
wrapper only caught `FileStationError` exceptions, so the corruption
slipped through silently. Callers (`stage_raw_bytes_for_dive_activity`)
read the JSON bytes via `read_bytes()` and uploaded them to the
file-exchange as `.ORF` content — every subsequent stage-2
`preprocess_species_image` activity received those JSON bytes, failed
`rawpy.imread` with `LibRawIOError: Input/output error`, and Temporal
retried 60+ times before the workflow timed out.

The migration to `synology-filestation` (`Client`) eliminates the
underlying-library footgun: it raises typed exceptions on JSON errors
and writes downloads atomically. The test below pins the *behavioral
contract* on `NasClient.download_to` so any future implementation
swap (or accidental regression) re-trips the same alarm before
corrupt content can reach the file-exchange.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

import pytest
from synology_filestation import SidNotFound


def test_external_shape_preserved_for_activity_call_sites(monkeypatch):
    """Contract test for the public method shape of `NasClient`.

    `stage_raw_bytes_for_dive_activity`, `stage_slate_pdf_activity`,
    `archive_processed_jpegs_to_nas_activity`, and
    `cleanup_raw_bytes_for_dive_activity` all call into NasClient
    using these exact keyword arguments. A future refactor that
    renames a method or drops a kwarg can't be caught at import time
    (call sites use `getattr` via `asyncio.to_thread` partials), so
    pin the shape here.

    `NasDownloadClient` must remain an alias for `NasClient` —
    several activities import the narrow alias for documentation
    intent.
    """
    from fishsense_services_orchestrator.ingest import nas as sut

    assert sut.NasDownloadClient is sut.NasClient

    monkeypatch.setattr(sut.Client, "login", lambda *a, **kw: MagicMock())
    client = sut.NasClient(
        nas_url="https://nas.example.com:6021",
        username="u",
        password="p",
    )

    # All three call shapes use keyword args. Asserting the calls don't
    # raise TypeError is sufficient — we don't care what the underlying
    # client does, only that our wrapper exposes these names.
    client.download_to(src_path="/foo", dest_dir="/tmp")
    client.upload(dest_dir="/foo", src_file_path="/tmp/bar")
    client.exists(file_path="/foo")


def test_download_to_raises_when_underlying_client_signals_failure(
    monkeypatch, tmp_path
):
    """Behavioral contract regression for the 2026-05-07 incident.

    When the underlying NAS client signals a download failure (today:
    by raising one of the `synology_filestation` typed exceptions on a
    DSM JSON-error response), `NasClient.download_to` MUST surface
    that failure to its caller AND MUST NOT leave any partial /
    JSON-error content visible at the destination path. Previously
    the wrapper silently swallowed tuple-returns from synology-api,
    leaving DSM JSON-error bodies on disk for the activity to read
    back as `.ORF` content.
    """
    from fishsense_services_orchestrator.ingest import nas as sut

    src_path = "/share/data/2024.06.20.REEF/img.ORF"

    # Mock the boundary: NasClient delegates to synology_filestation's
    # Client. We simulate the new client's correct failure mode —
    # `download_to` raises a `SidNotFound` and (because of its atomic
    # `<local>.part`+rename semantics) leaves no file at the
    # destination.
    fake_fs = MagicMock(name="synology_filestation.Client")
    fake_fs.download_to.side_effect = SidNotFound("session expired")
    monkeypatch.setattr(sut.Client, "login", lambda *a, **kw: fake_fs)

    client = sut.NasClient(
        nas_url="https://nas.example.com:6021",
        username="u",
        password="p",
    )

    dest_dir = tmp_path / "stage"
    dest_dir.mkdir()

    # Property 1: download_to surfaces the failure to the caller.
    with pytest.raises(Exception):
        client.download_to(src_path=src_path, dest_dir=str(dest_dir))

    # Property 2: the destination doesn't contain JSON masquerading as
    # raw image bytes. (Atomic-rename means it should be absent
    # entirely; the assertion is broader to allow for a future client
    # whose semantic is "leave a valid file or none at all.")
    leftover = dest_dir / pathlib.Path(src_path).name
    if leftover.exists():
        contents = leftover.read_bytes()
        assert b'"success"' not in contents and b'"error"' not in contents, (
            "download_to left a DSM-shaped JSON-error body at the "
            "destination; the caller will read those bytes via "
            "read_bytes() and upload them to the file-exchange as `.ORF`, "
            "reproducing the 2026-05-07 stage 2 corruption."
        )


# ── ingest additions: directory listing + ranged reads ────────────────


def _client_with(monkeypatch, fake_fs):
    from fishsense_services_orchestrator.ingest import nas as sut

    monkeypatch.setattr(sut.Client, "login", lambda *a, **kw: fake_fs)
    return sut.NasClient(
        nas_url="https://nas.example.com:6021", username="u", password="p"
    )


def test_list_dir_returns_typed_entries(monkeypatch):
    """Ingest has to enumerate a dive folder, which `NasClient` could not do.

    The underlying `synology-filestation` wheel already exposes `list_dir`, so
    this is a thin mapping rather than a new capability — no new dependency,
    and NAS access stays read-only.
    """
    fake_fs = MagicMock()
    fake_fs.list_dir.return_value = [
        {"path": "/d/P8210001.ORF", "isdir": False, "size": 15_232_982},
        {"path": "/d/sub", "isdir": True, "size": 0},
    ]
    client = _client_with(monkeypatch, fake_fs)

    entries = client.list_dir(folder_path="/d")

    fake_fs.list_dir.assert_called_once_with("/d")
    assert [e.name for e in entries] == ["P8210001.ORF", "sub"]
    assert [e.is_dir for e in entries] == [False, True]
    assert entries[0].size == 15_232_982
    assert entries[0].path == "/d/P8210001.ORF"


def test_list_dir_tolerates_entries_missing_optional_fields(monkeypatch):
    """DSM omits `size` on some entries. A KeyError here would fail the whole
    dive rather than one file."""
    fake_fs = MagicMock()
    fake_fs.list_dir.return_value = [{"path": "/d/x.ORF"}]
    client = _client_with(monkeypatch, fake_fs)

    entry = client.list_dir(folder_path="/d")[0]

    assert entry.name == "x.ORF"
    assert entry.is_dir is False
    assert entry.size == 0


def test_download_range_requests_only_the_asked_for_bytes(monkeypatch):
    """The dry run reads EXIF from the first megabyte of each frame rather than
    pulling ~15 MB. On a 500-image dive that is ~0.5 GB instead of ~7.5 GB over
    FileStation's fragile download backend."""
    fake_fs = MagicMock()
    fake_fs.download.return_value = b"IIRO" + b"\x00" * 100
    client = _client_with(monkeypatch, fake_fs)

    data = client.download_range(file_path="/d/x.ORF", offset=0, length=1024)

    fake_fs.download.assert_called_once_with("/d/x.ORF", offset=0, length=1024)
    assert data.startswith(b"IIRO")


def test_download_range_defaults_to_the_whole_file(monkeypatch):
    """`length=0` is the underlying client's "rest of the file" sentinel."""
    fake_fs = MagicMock()
    fake_fs.download.return_value = b""
    client = _client_with(monkeypatch, fake_fs)

    client.download_range(file_path="/d/x.ORF")

    fake_fs.download.assert_called_once_with("/d/x.ORF", offset=0, length=0)


def test_the_new_methods_are_read_only(monkeypatch):
    """NAS access from the api-worker is read-only by policy. Neither addition
    may reach a mutating call on the underlying client."""
    fake_fs = MagicMock()
    fake_fs.list_dir.return_value = []
    fake_fs.download.return_value = b""
    client = _client_with(monkeypatch, fake_fs)

    client.list_dir(folder_path="/d")
    client.download_range(file_path="/d/x.ORF")

    fake_fs.delete.assert_not_called()
    fake_fs.upload.assert_not_called()
    fake_fs.upload_bytes.assert_not_called()
    fake_fs.create_folder.assert_not_called()


# ── the transport (v2): SMB, or a loud fallback to FileStation ────────


def _fs(transport="smb", reason=None, detail=None):
    fake_fs = MagicMock()
    fake_fs.transport = transport
    fake_fs.transport_reason = reason
    fake_fs.transport_detail = detail
    return fake_fs


def test_the_client_says_which_transport_it_got(monkeypatch):
    client = _client_with(monkeypatch, _fs("smb"))
    assert client.transport == "smb"


def test_falling_back_to_filestation_is_a_warning_with_its_reason(monkeypatch, caplog):
    """FileStation alone broke the NAS under v1, and v2 ran on it silently
    for weeks while every SMB probe failed (2026-10-07). A fallback is never
    quiet."""
    with caplog.at_level("INFO"):
        _client_with(monkeypatch, _fs("http", "auth_failed", "logon failure"))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "http" in warnings[0].getMessage()
    assert "auth_failed" in warnings[0].getMessage()
    assert "logon failure" in warnings[0].getMessage()


def test_smb_is_logged_at_info(monkeypatch, caplog):
    with caplog.at_level("INFO"):
        _client_with(monkeypatch, _fs("smb"))
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("smb" in r.getMessage() for r in caplog.records)


def test_md5_is_computed_by_the_nas(monkeypatch):
    fake_fs = _fs()
    fake_fs.md5.return_value = "9d7018f9585f7bc319ea845d9621b73e"
    client = _client_with(monkeypatch, fake_fs)

    assert client.md5(file_path="/d/x.ORF") == "9d7018f9585f7bc319ea845d9621b73e"
    fake_fs.md5.assert_called_once_with("/d/x.ORF")
    fake_fs.download.assert_not_called()
    fake_fs.download_to.assert_not_called()


def test_the_backup_client_warns_on_a_fallback_too(monkeypatch, caplog):
    from fishsense_services_orchestrator.ops.backup import nas as backup

    monkeypatch.setattr(
        backup.Client, "login", lambda *a, **kw: _fs("http", "cooldown", None)
    )
    with caplog.at_level("INFO"):
        backup.NasBackupClient(
            nas_url="https://nas.example.com:6021", username="u", password="p"
        )
    assert [r for r in caplog.records if r.levelname == "WARNING"]
