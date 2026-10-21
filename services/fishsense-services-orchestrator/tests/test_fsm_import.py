"""Importing FishSense Mobile exports from the NAS (the owner, 2026-10-21:
CCFRP's summer 2025 data into ccfrp-ucsd).

The phones ran an app from before sync. Each day's folder (``ED-xx/<phone>/FSM``)
holds that day's photos and a copy of the phone's whole database: a ``photos``
table with the photo's name, its time, and the LiDAR depth and confidence. What
is pinned here:

* a capture is a database row whose photo is in its folder; the copies of the
  same row in other folders are one capture, not several;
* only captures taken within the window, by local date, are planned;
* a phone needs its intrinsics, given with their source, or nothing is planned;
* a dry run changes nothing and gives the plan's fingerprint; apply imports only
  that plan, refusing one that has changed since;
* each capture's photo, depth and confidence go into the tenant's bucket under
  their SHA-256, and are committed as an import that says where it came from;
* a capture already imported is skipped, so a retried apply is safe.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import date

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_orchestrator.ingest.nas import NasEntry
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings
from fishsense_services_orchestrator.ops.mobile_import.activities import (
    FsmImportActivities,
)
from fishsense_services_orchestrator.ops.mobile_import.contracts import (
    FsmImportRequest,
)

DATASET = "/fishsense_data/CCFRP/2025.06.27.FishSense.SanDiego"
TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
IPAD_K = [1604.2147, 0.0, 956.5816, 0.0, 1604.2147, 717.7617, 0.0, 0.0, 1.0]
IPHONE_K = [1375.0719, 0.0, 968.6433, 0.0, 1375.0719, 723.04926, 0.0, 0.0, 1.0]
#: 2025-06-27 09:30 PDT, and 2025-07-01 15:20 PDT; 2025-05-30 is before the window.
JUNE_27, JULY_1, MAY_30 = 1751041800, 1751408400, 1748643122
DEPTH = (4, 3)


def _database(rows) -> bytes:
    """A phone's v0 database: (id, timestamp, rgb_path, depth bytes) rows."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "database.sqlite")
        con = sqlite3.connect(path)
        con.execute("""
            CREATE TABLE photos (id INTEGER PRIMARY KEY, utc_unix_timestamp INTEGER,
                rgb_path TEXT, depth_bytes BLOB, depth_width INTEGER,
                depth_height INTEGER, confidence_bytes BLOB, confidence_width INTEGER,
                confidence_height INTGER)
            """)
        w, h = DEPTH
        for row_id, ts, depth, *confidence in rows:
            confidence = confidence[0] if confidence else bytes([2]) * (w * h)
            con.execute(
                "INSERT INTO photos VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row_id, ts, f"rgb_{ts}.jpg", depth, w, h, confidence, w, h),
            )
        con.commit()
        con.close()
        with open(path, "rb") as f:
            return f.read()


def _depth(seed: int) -> bytes:
    w, h = DEPTH
    return bytes([seed]) * (w * h * 4)


def _metadata(site: str, when: str, phone: str) -> bytes:
    return json.dumps(
        {
            "timestamp": when,
            "device": phone,
            "notes": "",
            "properties": {},
            "country": "USA",
            "region": "San Diego",
            "site": site,
            "mission": f"{phone}/FSM",
        }
    ).encode()


def _dataset(*, ipad_rows=None, iphone_rows=None) -> dict[str, bytes]:
    """Two day folders sharing each phone's database, as on the NAS."""
    ipad = _database(
        ipad_rows
        or [(1, JUNE_27, _depth(1)), (2, JULY_1, _depth(2)), (3, MAY_30, _depth(3))]
    )
    iphone = _database(iphone_rows or [(1, JULY_1 + 60, _depth(4))])
    files = {}
    for ed, site, when, photos in (
        ("ED-00", "Oceanside SEA Center", "2025-06-27T07:00:00-07:00", {JUNE_27}),
        ("ED-04", "SIO", "2025-07-01T15:00:00-07:00", {JULY_1, JULY_1 + 60}),
    ):
        for phone, db in (("iPad", ipad), ("iPhone", iphone)):
            folder = f"{DATASET}/{ed}/{phone}/FSM"
            files[f"{folder}/database.sqlite"] = db
            files[f"{folder}/metadata.json"] = _metadata(site, when, phone)
            for ts in photos:
                files[f"{folder}/rgb_{ts}.jpg"] = b"\xff\xd8jpeg" + str(ts).encode()
    files[f"{DATASET}/ED-00/iPad/Fishtechy/metadata.json"] = b"{}"
    return files


class FakeNas:
    def __init__(self, files: dict[str, bytes]):
        self.files = files

    def list_dir(self, *, folder_path):
        prefix = folder_path.rstrip("/") + "/"
        children: dict[str, tuple[bool, int]] = {}
        for path, body in self.files.items():
            if path.startswith(prefix):
                head, _, rest = path[len(prefix) :].partition("/")
                children[head] = (bool(rest), 0 if rest else len(body))
        return [
            NasEntry(path=prefix + name, name=name, is_dir=is_dir, size=size)
            for name, (is_dir, size) in sorted(children.items())
        ]

    def download_range(self, *, file_path, offset=0, length=0):
        body = self.files[file_path]
        return body[offset : offset + length] if length else body[offset:]


class FakeCatalog:
    def __init__(self, already=()):
        self.imports = []
        self.already = set(already)

    async def resolve_tenant(self, slug):
        return TENANT if slug == "ccfrp-ucsd" else None

    async def tenant_slug(self, tenant_id):
        return "ccfrp-ucsd"

    async def imported(self, tenant_id, uuids):
        known = self.already | {c.client_capture_uuid for c in self.imports}
        return {u for u in uuids if u in known}

    async def import_capture(self, tenant_id, capture):
        assert tenant_id == TENANT
        self.imports.append(capture)


class FakeObjects:
    bucket = "fishsense-tenant-ccfrp-ucsd"

    def presign_put(self, key, *, sha256, size, content_type):
        from fishsense_services_api.tenant_stores import PresignedUpload

        return PresignedUpload(
            url=f"https://s3.test/{self.bucket}/{key}",
            headers={
                "x-amz-checksum-sha256": sha256,
                "Content-Length": str(size),
                "Content-Type": content_type,
            },
        )


class FakeStores:
    def for_tenant(self, slug):
        assert slug == "ccfrp-ucsd"
        return FakeObjects()


def _request(**changes) -> FsmImportRequest:
    fields = dict(
        dataset=DATASET,
        tenant="ccfrp-ucsd",
        captured_from=date(2025, 6, 20),
        captured_to=date(2025, 9, 22),
        time_zone="America/Los_Angeles",
        intrinsics={"iPad": IPAD_K, "iPhone": IPHONE_K},
        intrinsics_source="fishsense-mobile-oceans-2025@e769bdd 01_process.ipynb cell 4",
    )
    fields.update(changes)
    return FsmImportRequest(**fields)


def _activities(files=None, catalog=None):
    nas = FakeNas(files or _dataset())
    puts = []
    acts = FsmImportActivities(
        catalog=catalog or FakeCatalog(),
        nas_settings=NasSettings(
            url="https://nas.test:6021",
            username="svc",
            password="x",
            raw_root_path="/fishsense_data",
        ),
        stores=FakeStores(),
        nas_client_factory=lambda: nas,
        put=lambda url, body, headers: puts.append((url, body, headers)),
    )
    return acts, puts


async def _plan(acts, request=None):
    return await ActivityEnvironment().run(acts.plan_fsm_import, request or _request())


async def test_a_dry_run_plans_each_photo_once_and_changes_nothing():
    catalog = FakeCatalog()
    acts, puts = _activities(catalog=catalog)

    report = await _plan(acts)

    # iPad: June 27 and July 1 have photos; May 30 is outside the window and
    # has none. iPhone: July 1. The database copies in the other folder add
    # nothing.
    assert report.planned == 3
    assert report.by_day == {"2025-06-27": 1, "2025-07-01": 2}
    assert report.by_phone == {"iPad": 2, "iPhone": 1}
    assert report.excluded["no_photo"] == 1
    assert not report.applied and report.plan_sha256
    assert (catalog.imports, puts) == ([], [])


async def test_the_window_is_by_local_date():
    acts, _ = _activities()

    report = await _plan(acts, _request(captured_to=date(2025, 6, 30)))

    assert report.by_day == {"2025-06-27": 1}
    assert report.excluded["outside_window"] == 2


async def test_a_phone_without_intrinsics_plans_nothing():
    acts, _ = _activities()

    with pytest.raises(ApplicationError, match="iPhone") as raised:
        await _plan(acts, _request(intrinsics={"iPad": IPAD_K}))
    assert raised.value.non_retryable


async def test_a_depth_that_does_not_fit_its_size_is_left_out():
    files = _dataset(ipad_rows=[(1, JUNE_27, b"short"), (2, JULY_1, _depth(2))])
    acts, _ = _activities(files)

    report = await _plan(acts)

    assert report.excluded["depth_size_mismatch"] == 1
    assert report.by_phone == {"iPad": 1, "iPhone": 1}


async def test_a_confidence_that_does_not_fit_its_size_is_left_out():
    files = _dataset(ipad_rows=[(1, JUNE_27, _depth(1), b"x"), (2, JULY_1, _depth(2))])
    acts, _ = _activities(files)

    report = await _plan(acts)

    assert report.excluded["depth_size_mismatch"] == 1


async def test_apply_needs_the_reviewed_plan():
    acts, _ = _activities()

    with pytest.raises(ApplicationError, match="plan") as raised:
        await ActivityEnvironment().run(acts.import_fsm_captures, _request(), "0" * 64)
    assert raised.value.non_retryable


async def test_apply_uploads_each_file_by_its_checksum_and_commits_an_import():
    catalog = FakeCatalog()
    acts, puts = _activities(catalog=catalog)
    plan = await _plan(acts)

    report = await ActivityEnvironment().run(
        acts.import_fsm_captures, _request(), plan.plan_sha256
    )

    assert (report.applied, report.imported) == (True, 3)
    assert len(puts) == 9
    for url, body, headers in puts:
        assert url.startswith(
            "https://s3.test/fishsense-tenant-ccfrp-ucsd/mobile/captures/"
        )
        assert headers["Content-Length"] == str(len(body))
        import base64

        assert (
            headers["x-amz-checksum-sha256"]
            == base64.b64encode(hashlib.sha256(body).digest()).decode()
        )
    ipad = next(c for c in catalog.imports if c.captured_at.timestamp() == JUNE_27)
    assert ipad.uploaded_by is None
    assert ipad.imported_from == f"{DATASET}/ED-00/iPad/FSM/database.sqlite#photos/1"
    assert ipad.intrinsics == tuple(IPAD_K)
    assert ipad.place_name == "Oceanside SEA Center"
    assert ipad.device_model == "iPad"
    assert (ipad.depth_width, ipad.depth_height, ipad.depth_row_bytes) == (4, 3, 16)
    assert {f.role for f in ipad.files} == {"rgb_jpeg", "depth_f32", "confidence_u8"}
    assert ipad.manifest["time_zone"] == "America/Los_Angeles"
    assert ipad.manifest["import"]["intrinsics_source"].startswith(
        "fishsense-mobile-oceans-2025@e769bdd"
    )
    assert (
        ipad.manifest["import"]["photo"]
        == f"{DATASET}/ED-00/iPad/FSM/rgb_{JUNE_27}.jpg"
    )
    iphone = next(c for c in catalog.imports if c.device_model == "iPhone")
    assert iphone.intrinsics == tuple(IPHONE_K)
    assert iphone.device_install_id != ipad.device_install_id


async def test_the_same_capture_gets_the_same_id_every_time():
    first, _ = _activities()
    second, _ = _activities()
    a = await _plan(first)
    b = await _plan(second)

    assert a.plan_sha256 == b.plan_sha256


async def test_a_capture_already_imported_is_skipped():
    acts, _ = _activities()
    plan = await _plan(acts)
    done = FakeCatalog()
    acts, puts = _activities(catalog=done)
    await ActivityEnvironment().run(
        acts.import_fsm_captures, _request(), plan.plan_sha256
    )
    again, puts_again = _activities(catalog=done)

    report = await ActivityEnvironment().run(
        again.import_fsm_captures, _request(), plan.plan_sha256
    )

    assert (report.imported, report.already_imported) == (0, 3)
    assert puts_again == []
