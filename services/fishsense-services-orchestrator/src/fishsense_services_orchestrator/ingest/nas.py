"""Ported verbatim from fishsense-lite@a8b2c3bc
services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/nas.py.

FileStation-backed NAS client for the api-worker.

Backed by the `synology-filestation` Rust client (PyO3 wheel from
`UCSD-E4E/synology-filestation`). Replaces the previous `synology-api`
PyPI package — see [CLAUDE.md] / commit log for the 2026-05-07 stage 2
incident that drove the swap. Two behavioral wins from the new client
the api-worker explicitly relies on:

  * **DSM JSON-error responses raise typed exceptions.** `synology-api`
    streamed JSON-error response bodies to disk and returned a tuple
    instead of raising, which let corrupt JSON propagate downstream as
    `.ORF` content. The new `Client` raises `SidNotFound` (DSM 119),
    `NoSuchFile`, etc. on the same response shapes.
  * **Atomic `download_to` writes.** `Client.download_to` writes to
    `<local>.part` and renames on success. A failed download leaves no
    partial file at the destination, so the staging activity can't
    accidentally read corrupt bytes back via `read_bytes`.

Operations:
  * `download_to(src_path, dest_dir)` — Phase 3a staging-in.
  * `upload(dest_dir, src_file_path)` — Phase 3b archive.
  * `exists(file_path)` — idempotency HEAD-equivalent for archive.
  * `list_dir(folder_path)` — enumerate a dive folder (ingest).
  * `download_range(file_path, offset, length)` — ranged read (ingest).

The last two exist for ingest, which has to discover what is in a dive folder
and read each frame's EXIF header. Both are thin mappings onto methods the
`synology-filestation` wheel already provides, so neither adds a dependency,
and both are reads — the api-worker's NAS access stays read-only by policy.

The wrapper class preserves its previous external shape so call sites
in `stage_raw_bytes_for_dive_activity`, `archive_processed_jpegs_to_nas_activity`,
and `cleanup_raw_bytes_for_dive_activity` are unchanged.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List
from urllib.parse import urlparse

from synology_filestation import (
    AlreadyExists,
    Client,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NasEntry:
    """One child of a NAS folder.

    Deliberately minimal: ingest only needs to tell files from directories,
    match `.ORF` by name, and report sizes. Everything else DSM returns is
    passed over rather than modelled.
    """

    path: str
    name: str
    is_dir: bool
    size: int

    @classmethod
    def from_dsm(cls, entry: dict) -> "NasEntry":
        """Build from a DSM `list_dir` row.

        Optional fields are defaulted rather than required — DSM omits `size`
        on some entries, and a KeyError here would fail an entire dive over one
        odd row.
        """
        path = entry.get("path") or ""
        return cls(
            path=path,
            name=entry.get("name") or os.path.basename(path.rstrip("/")),
            is_dir=bool(entry.get("isdir", False)),
            size=int(entry.get("size") or 0),
        )


class NasClient:
    """FileStation-backed client for the api-worker.

    `nas_url` is parsed for hostname + port; `username`/`password` are
    the corresponding NAS credentials. Same parse contract the previous
    synology-api implementation used.
    """

    def __init__(self, *, nas_url: str, username: str, password: str):
        parsed = urlparse(nas_url)
        if not parsed.hostname or not parsed.port:
            raise ValueError(f"NAS url must include hostname + port; got {nas_url!r}")
        # `auto_relogin=True` (default) makes the client transparently
        # re-authenticate on SID-expired (DSM code 119) and retry the
        # operation once. That's the property that fixes the 30-min
        # idle-timeout corruption pattern: every long-running staging
        # activity used to silently produce JSON-as-`.ORF` bytes once
        # the cached SID went stale.
        self._fs = Client.login(
            parsed.hostname,
            parsed.port,
            username,
            password,
            https=True,
        )

    def download_to(self, *, src_path: str, dest_dir: str) -> None:
        """Download the NAS file at `src_path` into local `dest_dir`.

        The new client takes a full local *file* path; this wrapper
        keeps the previous "drop into a directory" call shape so
        `stage_raw_bytes_for_dive_activity` doesn't change. The
        resulting file lands at `{dest_dir}/{basename(src_path)}`.

        Atomic-rename semantics from the underlying `Client.download_to`
        mean the destination either contains the complete NAS file or
        doesn't exist — never a partial body or JSON-error stub.
        """
        _log.info("nas download start src=%s dest_dir=%s", src_path, dest_dir)
        local_path = os.path.join(dest_dir, os.path.basename(src_path))
        self._fs.download_to(src_path, local_path)
        _log.info("nas download done src=%s", src_path)

    def upload(self, *, dest_dir: str, src_file_path: str) -> None:
        """Upload `src_file_path` into NAS folder `dest_dir`.

        Creates `dest_dir` (idempotently) before the upload — the new
        client honors `_create_parents=True` in `upload`, but historical
        prod data has at least one path layout where the parent of the
        target dir doesn't exist, so the explicit `_ensure_dir` step
        stays as belt-and-suspenders. Existing files at the same name
        are overwritten.
        """
        _log.info("nas upload start dest=%s src=%s", dest_dir, src_file_path)
        self._ensure_dir(dest_dir)
        self._fs.upload(src_file_path, dest_dir, overwrite=True)
        _log.info("nas upload done dest=%s", dest_dir)

    def list_dir(self, *, folder_path: str) -> List["NasEntry"]:
        """Enumerate `folder_path`, one `NasEntry` per child.

        Ingest needs to know what is in a dive folder before it can stage
        anything. The underlying client already has `list_dir`; this maps its
        loosely-typed dicts onto a shape the activities can rely on, tolerating
        the optional fields DSM sometimes omits (a missing `size` must not fail
        the whole dive).
        """
        raw = self._fs.list_dir(folder_path)
        return [NasEntry.from_dsm(entry) for entry in raw]

    def download_range(
        self, *, file_path: str, offset: int = 0, length: int = 0
    ) -> bytes:
        """Read `length` bytes of `file_path` starting at `offset`.

        `length=0` means "to the end", matching the underlying client. The
        point is the ingest dry run: EXIF lives in the first megabyte, so a
        preflight over a 500-image dive moves ~0.5 GB instead of ~7.5 GB across
        FileStation's fragile shared download backend.
        """
        return self._fs.download(file_path, offset=offset, length=length)

    def exists(self, *, file_path: str) -> bool:
        """Return True if `file_path` exists on the NAS, False otherwise.

        Used by the archive activity to skip already-uploaded JPEGs on
        retried runs without re-paying the local read + upload.
        """
        return self._fs.exists(file_path)

    def _ensure_dir(self, dest_dir: str) -> None:
        """Create `dest_dir` on the NAS, treating 'already exists' as
        success."""
        parent, _, name = dest_dir.rstrip("/").rpartition("/")
        if not parent or not name:
            return
        try:
            self._fs.create_folder(parent, name, _force_parent=True)
        except AlreadyExists:
            return


# Backwards-compatible alias for Phase 3a callers that imported the
# narrow class name. New code should use `NasClient` directly.
NasDownloadClient = NasClient
