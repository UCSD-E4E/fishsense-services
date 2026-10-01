"""The backup's NAS client: upload a dump, list a database's dumps, delete one.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-backup-worker/
src/fishsense_backup_worker/nas.py.

Backed by the `synology-filestation` client, which raises typed errors on DSM
JSON-error responses (the old `synology-api` returned them as if they were
file bodies: the 2026-05-07 stage-2 incident) and writes atomically. This is
the one NAS client in the repo that writes, and only the backup process builds
it: the orchestrator's own NAS access stays read-only by policy.

Sync only -- call it through `asyncio.to_thread`.
"""

from __future__ import annotations

import logging
import os
from typing import List
from urllib.parse import urlparse

from synology_filestation import AlreadyExists, Client

__all__ = ["NasBackupClient"]

_log = logging.getLogger(__name__)


class NasBackupClient:
    """Upload, list and delete, for the backup workflow's narrow needs.

    `nas_url` must carry a hostname and a port.
    """

    def __init__(self, *, nas_url: str, username: str, password: str):
        parsed = urlparse(nas_url)
        if not parsed.hostname or not parsed.port:
            raise ValueError(f"NAS url must include hostname + port; got {nas_url!r}")
        # `auto_relogin` (the default) re-authenticates on an expired session
        # (DSM 119) and retries once: the property that fixed the 30-minute
        # idle-timeout failures of 2026-05-03 and 2026-05-07.
        self._fs = Client.login(
            parsed.hostname,
            parsed.port,
            username,
            password,
            https=True,
        )

    def upload(self, *, dest_dir: str, src_file_path: str) -> None:
        """Upload `src_file_path` into `dest_dir`, creating the folder first:
        the client creates parents, but a historical backup layout lacked one,
        so the explicit step stays. An existing file of the same name is
        overwritten."""
        _log.info("nas upload start dest=%s src=%s", dest_dir, src_file_path)
        self._ensure_dir(dest_dir)
        self._fs.upload(src_file_path, dest_dir, overwrite=True)
        _log.info("nas upload done dest=%s", dest_dir)

    def _ensure_dir(self, dest_dir: str) -> None:
        """Create `dest_dir`; "already exists" is success."""
        parent, _, name = dest_dir.rstrip("/").rpartition("/")
        if not parent or not name:
            return
        try:
            self._fs.create_folder(parent, name, _force_parent=True)
        except AlreadyExists:
            return

    def list_filenames(self, *, folder_path: str) -> List[str]:
        """The bare filenames in `folder_path`: retention matches on names."""
        names = []
        for entry in self._fs.list_dir(folder_path):
            full_path = entry.get("path") if isinstance(entry, dict) else None
            if not full_path:
                continue
            names.append(os.path.basename(full_path))
        return names

    def delete(self, *, file_path: str) -> None:
        """Delete one file at an absolute NAS path."""
        _log.info("nas delete %s", file_path)
        self._fs.delete(file_path)
