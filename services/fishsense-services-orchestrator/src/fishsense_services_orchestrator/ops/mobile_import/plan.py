"""Which captures an FSM export holds: pure, from what the scan read.

New in v2 (2026-10-21). The phones' app, from before sync, kept everything in
one database per phone. Each day's folder holds that day's photos and a copy
of the whole database, so the same row appears in every folder (CCFRP summer
2025: the iPad's in ED-00 and ED-04, and again in ED-05 and ED-06).

The rules, in order:
* a capture is a row whose photo is in a folder that holds it; copies of the
  row elsewhere are the same capture. A row whose photo no folder holds is
  left out (``no_photo``): there is nothing to look at.
* it must be taken within the window, by its local date (``outside_window``);
* its depth and confidence must be the sizes the row says, float32 and uint8
  (``depth_size_mismatch``): the server would otherwise misread them.

A capture's id comes from the dataset, the phone and its time, so the same
capture always gets the same id, and a second import finds it already there.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

__all__ = ["Folder", "FolderRow", "PlannedCapture", "fingerprint", "plan"]


@dataclass(frozen=True)
class FolderRow:
    row_id: int
    timestamp: int
    photo: str
    depth_width: int
    depth_height: int
    depth_bytes: int
    confidence_width: int
    confidence_height: int
    confidence_bytes: int


@dataclass(frozen=True)
class Folder:
    #: ``<dataset>/ED-xx/<phone>/FSM``.
    path: str
    phone: str
    #: Photo name -> size in bytes.
    photos: dict[str, int]
    rows: list[FolderRow]
    #: The folder's e4edm ``metadata.json``.
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PlannedCapture:
    client_capture_uuid: uuid.UUID
    phone: str
    folder: str
    row_id: int
    timestamp: int
    photo: str
    photo_size: int
    depth_width: int
    depth_height: int
    confidence_width: int
    confidence_height: int
    metadata: dict

    @property
    def captured_at(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, timezone.utc)

    @property
    def photo_path(self) -> str:
        return f"{self.folder}/{self.photo}"

    @property
    def database(self) -> str:
        return f"{self.folder}/database.sqlite"


def capture_id(dataset: str, phone: str, timestamp: int) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"fsm-import:{dataset}:{phone}:{timestamp}")


def plan(
    folders: list[Folder],
    *,
    dataset: str,
    captured_from,
    captured_to,
    time_zone: str,
) -> tuple[list[PlannedCapture], Counter]:
    zone = ZoneInfo(time_zone)
    excluded: Counter = Counter()
    by_capture: dict[tuple[str, int], list[tuple[Folder, FolderRow]]] = {}
    for folder in sorted(folders, key=lambda f: f.path):
        for row in folder.rows:
            by_capture.setdefault((folder.phone, row.timestamp), []).append(
                (folder, row)
            )
    planned = []
    for (phone, timestamp), copies in sorted(by_capture.items()):
        held = [(f, r) for f, r in copies if r.photo in f.photos]
        if not held:
            excluded["no_photo"] += 1
            continue
        folder, row = held[0]
        day = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(zone).date()
        if not captured_from <= day <= captured_to:
            excluded["outside_window"] += 1
            continue
        if (
            row.depth_bytes != row.depth_width * row.depth_height * 4
            or row.confidence_bytes != row.confidence_width * row.confidence_height
        ):
            excluded["depth_size_mismatch"] += 1
            continue
        planned.append(
            PlannedCapture(
                client_capture_uuid=capture_id(dataset, phone, timestamp),
                phone=phone,
                folder=folder.path,
                row_id=row.row_id,
                timestamp=timestamp,
                photo=row.photo,
                photo_size=folder.photos[row.photo],
                depth_width=row.depth_width,
                depth_height=row.depth_height,
                confidence_width=row.confidence_width,
                confidence_height=row.confidence_height,
                metadata=folder.metadata,
            )
        )
    return planned, excluded


def fingerprint(planned: list[PlannedCapture]) -> str:
    """The plan an operator reviews: every capture, where it is, and its size."""
    body = [
        [str(p.client_capture_uuid), p.folder, p.row_id, p.photo, p.photo_size]
        for p in planned
    ]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def local_day(timestamp: int, time_zone: str) -> str:
    when = datetime.fromtimestamp(timestamp, timezone.utc)
    return when.astimezone(ZoneInfo(time_zone)).date().isoformat()


def ed_folder(folder: str, dataset: str) -> str:
    """``ED-04/iPhone/FSM`` from the full path."""
    return posixpath.relpath(folder, dataset)
