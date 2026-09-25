"""The two conventions every raw frame is read under, and NAS plumbing.

Ported from fishsense-lite@a8b2c3bc activities/nas_frames.py. The conventions
are unchanged -- they are the ones that break *silently* if they drift:

* the checksum is ``md5`` of the whole file (v1 re-verified it against 1,619
  production frames with zero disagreements), and duplicate detection depends
  on it matching exactly;
* the timestamp is EXIF tag 0x0132, **naive, stamped UTC, offset not applied**,
  matching ~131k existing rows that stage-1 clustering does arithmetic on.

v2 change: NAS settings are explicit and typed (``FISHSENSE_NAS_*``) and passed
in, instead of a global Dynaconf object read at call time.
"""

import hashlib
import secrets
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from fishsense_services_orchestrator.ingest import nas
from fishsense_services_orchestrator.ingest.nas_frames import (
    NasSettings,
    build_nas_client,
    file_checksum,
    read_taken_datetime,
    resolve_nas_path,
)

from ._tiff_builder import build_orf

# Generated per run, never written in the source: a password-shaped literal
# trips secret scanners even when it is fake.
NAS_PASSWORD = secrets.token_hex(8)

SETTINGS = {
    "FISHSENSE_NAS_URL": "https://nas.example.test:6021",
    "FISHSENSE_NAS_USERNAME": "svc",
    "FISHSENSE_NAS_PASSWORD": NAS_PASSWORD,
    "FISHSENSE_NAS_RAW_ROOT_PATH": "/fishsense_data/",
}


@pytest.fixture
def settings(monkeypatch) -> NasSettings:
    for name, value in SETTINGS.items():
        monkeypatch.setenv(name, value)
    return NasSettings()


# --- the conventions of record --------------------------------------------------


def test_the_checksum_is_md5_of_the_whole_file(tmp_path):
    frame = tmp_path / "P1.ORF"
    content = build_orf() + b"\x00" * 100_000  # spans many hash chunks
    frame.write_bytes(content)

    assert file_checksum(frame) == hashlib.md5(content).hexdigest()


def test_the_timestamp_is_tag_0x0132_stamped_utc_with_no_offset_applied(tmp_path):
    frame = tmp_path / "P1.ORF"
    frame.write_bytes(
        build_orf(
            date_time="2025:03:06 17:00:15",
            date_time_original="2025:03:06 09:00:00",
            offset_time="-08:00",
        )
    )

    assert read_taken_datetime(frame) == datetime(2025, 3, 6, 17, 0, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    "date_time", [pytest.param(None, id="absent"), pytest.param("garbage", id="bad")]
)
def test_no_usable_timestamp_is_none_never_a_default(tmp_path, date_time):
    """Stage-1 clustering can't tell a fabricated timestamp from a real one."""
    frame = tmp_path / "P1.ORF"
    frame.write_bytes(build_orf(date_time=date_time, date_time_original=None))

    assert read_taken_datetime(frame) is None


# --- NAS plumbing (v2: explicit settings) ---------------------------------------


def test_a_relative_path_is_prefixed_with_the_raw_root(settings):
    assert (
        resolve_nas_path("2024 REEF/d1/P1.ORF", settings)
        == "/fishsense_data/2024 REEF/d1/P1.ORF"
    )


def test_an_absolute_path_is_not_double_prefixed(settings):
    """A hand-corrected absolute path passes through; FileStation reports an
    unresolved path as a 502, which looks transient, so this matters."""
    assert resolve_nas_path("/elsewhere/P1.ORF", settings) == "/elsewhere/P1.ORF"


def test_the_client_is_built_from_the_settings(settings, monkeypatch):
    login = MagicMock()
    monkeypatch.setattr(nas.Client, "login", login)

    build_nas_client(settings)

    login.assert_called_once_with(
        "nas.example.test", 6021, "svc", NAS_PASSWORD, https=True
    )


def test_the_nas_password_never_appears_in_the_settings_repr(settings):
    assert NAS_PASSWORD not in repr(settings)
