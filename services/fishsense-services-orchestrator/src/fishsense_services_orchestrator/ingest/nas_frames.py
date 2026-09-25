"""Shared NAS-frame plumbing: client construction, path resolution, and the two
conventions every raw frame is read under.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/nas_frames.py. The conventions are
unchanged. v2 change: NAS settings are explicit and typed (``NasSettings``,
``FISHSENSE_NAS_*``) and passed in, instead of a global Dynaconf object -- so no
module reads config on import, and tests need no global state.

The two conventions break *silently* if they drift, which is why each is
defined once:

  * A checksum computed differently from `spider/backend.py:67` does not error.
    Duplicate detection simply reports **zero overlap**, every re-ingested frame
    lands canonical, and the canonical-only pipeline gating has nothing to gate
    on.
  * A timestamp read from a different tag, or with the camera's offset applied,
    does not error either. It silently disagrees with ~131k existing rows and
    corrupts stage-1 clustering, which is pure timestamp arithmetic.

v1 re-verified the checksum against the live corpus on 2026-08-17: 1,619 frames
across all 272 canonical dives, zero disagreements -- the convention of record,
not an inference from source.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fishsense_services_orchestrator.ingest.exif import read_exif
from fishsense_services_orchestrator.ingest.nas import NasClient

# Matches `spider/backend.py:67` exactly. The chunking is not scoping -- it is
# byte-identical to hashing the whole buffer -- but it keeps a ~15 MB buffer per
# frame off the heap, which is why spider did it and why we do.
HASH_CHUNK_BYTES = 8192

# EXIF sits at the front of an ORF; no need to re-read ~15 MB to reach it.
EXIF_HEADER_BYTES = 1024 * 1024

__all__ = [
    "EXIF_HEADER_BYTES",
    "HASH_CHUNK_BYTES",
    "NasSettings",
    "build_nas_client",
    "file_checksum",
    "parse_taken_datetime",
    "read_taken_datetime",
    "resolve_nas_path",
]


class NasSettings(BaseSettings):
    """The e4e NAS (Synology FileStation), from ``FISHSENSE_NAS_*``."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_NAS_")

    #: Must include hostname and port, e.g. ``https://nas.example:6021``.
    url: str
    username: str
    password: SecretStr
    #: The share root that relative frame paths are resolved under.
    raw_root_path: str


def build_nas_client(settings: NasSettings) -> NasClient:
    """The orchestrator's NAS client -- read-only use, by policy."""
    return NasClient(
        nas_url=settings.url,
        username=settings.username,
        password=settings.password.get_secret_value(),
    )


def resolve_nas_path(relative_path: str, settings: NasSettings) -> str:
    """Prepend the NAS raw root to a share-relative path.

    The stored convention is share-relative; FileStation needs absolute. Worth
    getting right because FileStation surfaces an unresolved path as a **502**,
    not a 404, so the failure looks transient. An already-absolute path passes
    through, so a hand-corrected row is not double-prefixed.
    """
    if relative_path.startswith("/"):
        return relative_path
    root = settings.raw_root_path.rstrip("/")
    return f"{root}/{relative_path.lstrip('/')}"


def file_checksum(path: Path) -> str:
    """`md5` of the whole file, streamed. The convention of record."""
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for blob in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(blob)
    return digest.hexdigest()


def read_taken_datetime(path: Path) -> datetime | None:
    """Naive EXIF tag 0x0132 stamped UTC, or None if unreadable.

    The camera's recorded offset is deliberately **not** applied: matching the
    ~131k existing rows matters more than being right, because one consistent
    offset is recoverable later and two conventions mixed in one column are not.

    None means "no usable timestamp" and callers must treat it as a rejection
    rather than substituting a default -- stage-1 clustering cannot tell a
    fabricated timestamp from a real one.
    """
    with open(path, "rb") as handle:
        header = handle.read(EXIF_HEADER_BYTES)
    return parse_taken_datetime(read_exif(header).date_time)


def parse_taken_datetime(raw: str | None) -> datetime | None:
    """EXIF `"YYYY:MM:DD HH:MM:SS"` -> aware UTC, offset not applied (see
    `read_taken_datetime`); None when absent or malformed."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
