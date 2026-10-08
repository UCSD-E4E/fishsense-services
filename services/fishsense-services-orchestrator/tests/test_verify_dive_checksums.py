"""`verify_dive_checksums` -- does the migrated data mean what we think it means?

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_verify_dive_checksums_activity.py.

Every ingest convention was recovered by reading the retired spider crawler's
source: checksum is `md5` of the whole file (`backend.py:67`), and
`taken_datetime` is naive EXIF tag 0x0132 stamped UTC (`backend.py:20`). That
derivation proves what *spider wrote*; this activity compares the stored values
with the bytes still on the NAS. It fails in the dangerous way if the
conventions drift: duplicate detection silently reports **zero overlap**.

It is **read-only**, and a mismatch is a *finding*, not a failure.

v2 changes, each pinned here:

* the dive is named by its ``number`` (v1's id for a migrated dive) and found in
  whichever served tenant holds it; the frames come from the checksum catalog
  (tested on Postgres in fishsense-services-api tests/test_checksum_store.py),
  and a finding names the capture's number (v1's image id);
* **a retry resumes** from its last heartbeat. v1 heartbeated the index "so a
  retry resumes" but never read it back, so every retry re-downloaded the whole
  sample -- whole files, ~15 MB each;
* **a dive that doesn't exist is an error**, not an empty report. v1's SDK
  returned no rows for an unknown dive id, so a typo read as a clean,
  zero-frame verification;
* a capture recorded with ``sha256`` is hashed with sha256. v2 records the
  algorithm (v1's column was md5 by convention), and hashing it with md5 would
  report every such row as a mismatch.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from synology_filestation import DSMError, NoSuchFile
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.checksum_store import VerifyCapture
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings
from fishsense_services_orchestrator.ops.checksums import activities as sut
from fishsense_services_orchestrator.ops.checksums.contracts import (
    VerifyChecksumsReport,
)

from ._tiff_builder import build_orf

ROOT = "/fishsense_data/REEF/data"
DIVE_FOLDER = "2024.06.20.REEF/082929_FishModels_FSL07"
LAB = uuid.UUID("7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11")
REEF = uuid.UUID("0d0d0d0d-3f58-4d8e-9a3c-0c5f1f7f2d11")
DIVE = uuid.UUID("11111111-2222-4333-8444-555555555555")
TAKEN = datetime(2024, 8, 21, 8, 56, 51, tzinfo=timezone.utc)


def _orf(date_time="2024:08:21 08:56:51") -> bytes:
    """A synthetic ORF, padded past one hash chunk so the streaming read is
    exercised rather than incidentally fitting in a single buffer."""
    return build_orf(date_time=date_time, serial_number="BJ6C67989") + b"\0" * 40_000


def _capture(number, name, data, *, checksum=None, taken=None, algorithm="md5"):
    """A capture row, defaulting to values that agree with `data` so a test
    only has to state the disagreement it cares about."""
    if checksum is None:
        checksum = hashlib.new(algorithm, data).hexdigest()
    return VerifyCapture(
        number=number,
        source_path=f"{DIVE_FOLDER}/{name}",
        checksum=checksum,
        checksum_algorithm=algorithm,
        captured_at=TAKEN if taken is None else taken,
    )


class FakeCatalog:
    """The checksum catalog, answered from memory: `dives` maps
    (tenant, number) -> dive id, and every dive holds `captures`."""

    def __init__(self, captures, *, dives=None, tenants=(LAB,)):
        self.captures = list(captures)
        self.dives = {(LAB, 412): DIVE} if dives is None else dives
        self.tenants = list(tenants)
        self.asked: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def member_tenants(self):
        return self.tenants

    async def dive_by_number(self, tenant_id, number):
        return self.dives.get((tenant_id, number))

    async def captures_to_verify(self, tenant_id, dive_id):
        self.asked.append((tenant_id, dive_id))
        return self.captures

    async def canonical_dive_numbers(self, tenant_id):
        return sorted(n for (t, n) in self.dives if t == tenant_id)


class FakeNas:
    """A NAS that materialises real files, keyed by basename, and records what
    was downloaded. Real files, because the activity hashes from disk in
    chunks, as spider did."""

    def __init__(self, contents: dict[str, bytes]):
        self.contents = contents
        self.downloaded: list[str] = []

    def download_to(self, *, src_path: str, dest_dir: str) -> None:
        self.downloaded.append(src_path)
        name = src_path.rsplit("/", 1)[-1]
        if name not in self.contents:
            raise NoSuchFile("no such file or folder")
        Path(dest_dir, name).write_bytes(self.contents[name])


def _settings() -> NasSettings:
    return NasSettings(
        url="https://nas.example.test:6021",
        username="svc",
        password="unused",
        raw_root_path=ROOT,
    )


def _activities(catalog, nas):
    return sut.ChecksumActivities(
        catalog=catalog, nas_settings=_settings(), nas_client_factory=lambda: nas
    )


async def _run(captures, contents, *, limit=None, env=None, catalog=None, nas=None):
    catalog = catalog or FakeCatalog(captures)
    nas = nas or FakeNas(contents)
    return await (env or ActivityEnvironment()).run(
        _activities(catalog, nas).verify_dive_checksums, 412, limit
    )


# -- the question being asked ---------------------------------------------------


async def test_a_row_whose_stored_checksum_matches_the_file_is_counted_matched():
    data = _orf()
    report = await _run([_capture(1, "PA010001.ORF", data)], {"PA010001.ORF": data})

    assert report.dive_number == 412
    assert report.checked == 1
    assert report.checksum_matched == 1
    assert report.mismatches == []


async def test_a_checksum_mismatch_reports_both_values_and_the_path():
    """The finding has to be actionable. "Some rows disagree" would send
    someone back to the NAS to work out which."""
    data = _orf()
    stored = "0" * 32
    report = await _run(
        [_capture(1, "PA010001.ORF", data, checksum=stored)], {"PA010001.ORF": data}
    )

    assert report.checksum_matched == 0
    (finding,) = report.mismatches
    assert finding.capture_number == 1
    assert finding.path.endswith("PA010001.ORF")
    assert finding.stored == stored
    assert finding.computed == hashlib.md5(data).hexdigest()


async def test_the_checksum_is_md5_of_the_whole_file():
    """Pins the convention recovered from `spider/backend.py:67`. A reader that
    hashed only the header would agree with itself forever and disagree with
    every migrated row."""
    data = _orf()
    report = await _run(
        [_capture(1, "P.ORF", data, checksum=hashlib.md5(data).hexdigest())],
        {"P.ORF": data},
    )
    assert report.checksum_matched == 1

    header_only = hashlib.md5(data[: 1024 * 1024 // 64]).hexdigest()
    report = await _run(
        [_capture(1, "P.ORF", data, checksum=header_only)], {"P.ORF": data}
    )
    assert report.checksum_matched == 0


async def test_a_capture_recorded_with_sha256_is_hashed_with_sha256():
    """v2 records the algorithm; hashing a sha256 row with md5 would report
    every such row as a mismatch."""
    data = _orf()
    report = await _run(
        [_capture(1, "P.ORF", data, algorithm="sha256")], {"P.ORF": data}
    )

    assert report.checksum_matched == 1
    assert report.mismatches == []


async def test_a_timestamp_mismatch_is_reported_separately():
    """A wrong checksum breaks duplicate detection, a wrong timestamp breaks
    stage-1 clustering, so they are tracked apart."""
    data = _orf()
    wrong = datetime(1999, 1, 1, tzinfo=timezone.utc)
    report = await _run(
        [_capture(1, "PA010001.ORF", data, taken=wrong)], {"PA010001.ORF": data}
    )

    assert report.checksum_matched == 1
    (finding,) = report.timestamp_mismatches
    assert finding.stored == wrong.isoformat()
    assert finding.computed == TAKEN.isoformat()


async def test_the_stored_timestamp_convention_is_naive_0x0132_stamped_utc():
    """Agreement, not correctness: the camera's offset is deliberately not
    applied (the ORF here says -08:00)."""
    data = _orf(date_time="2024:08:21 08:56:51")
    report = await _run(
        [_capture(1, "PA010001.ORF", data, taken=TAKEN)], {"PA010001.ORF": data}
    )

    assert report.timestamp_mismatches == []


async def test_a_stored_time_in_another_zone_is_the_same_instant():
    """Postgres hands a timestamptz back in the session's zone; the same
    instant must not read as a mismatch."""
    data = _orf()
    pacific = TAKEN.astimezone(timezone(-timedelta(hours=7)))
    report = await _run([_capture(1, "P.ORF", data, taken=pacific)], {"P.ORF": data})

    assert report.timestamp_mismatches == []


# -- findings, not failures ------------------------------------------------------


async def test_a_file_missing_from_the_nas_is_a_finding_not_a_crash():
    """Unlike staging, where a missing file is a non-retryable failure: here
    "the row exists but the file is gone" is one of the answers."""
    data = _orf()
    report = await _run(
        [_capture(1, "PA010001.ORF", data), _capture(2, "GONE.ORF", data)],
        {"PA010001.ORF": data},
    )

    assert report.checked == 2
    assert report.checksum_matched == 1
    assert [m.path.rsplit("/", 1)[-1] for m in report.missing_on_nas] == ["GONE.ORF"]


async def test_a_nas_outage_propagates_for_temporal_to_retry():
    """A NAS that is down, rather than a file that is absent, is not a finding:
    recording it would make an outage look like missing data."""

    class DownNas(FakeNas):
        def download_to(self, *, src_path, dest_dir):
            raise DSMError("Synology API error 502")

    with pytest.raises(DSMError):
        await _run([_capture(1, "P.ORF", _orf())], {}, nas=DownNas({}))


async def test_a_row_with_no_stored_checksum_is_reported_not_silently_skipped():
    """A blank checksum is itself a migration finding: the column is what
    duplicate detection joins on."""
    data = _orf()
    report = await _run(
        [dataclasses.replace(_capture(1, "P.ORF", data), checksum="")],
        {"P.ORF": data},
    )

    assert report.checked == 1
    assert report.checksum_matched == 0
    assert report.mismatches == []
    (finding,) = report.no_stored_checksum
    assert finding.computed == hashlib.md5(data).hexdigest()


async def test_one_bad_row_does_not_stop_the_rest_being_checked():
    data = _orf()
    report = await _run(
        [
            _capture(1, "A.ORF", data, checksum="0" * 32),
            _capture(2, "B.ORF", data),
            _capture(3, "C.ORF", data),
        ],
        {"A.ORF": data, "B.ORF": data, "C.ORF": data},
    )

    assert report.checked == 3
    assert report.checksum_matched == 2
    assert len(report.mismatches) == 1


async def test_a_frame_with_no_nas_path_is_skipped_uncounted():
    """A frame held only in the object store has nothing on the NAS to compare
    (v1 skipped a row with no path the same way)."""
    data = _orf()
    held = dataclasses.replace(_capture(1, "X.ORF", data), source_path=None)
    report = await _run([held, _capture(2, "B.ORF", data)], {"B.ORF": data})

    assert report.total_in_dive == 2
    assert report.checked == 1


# -- the path, and the dive -------------------------------------------------------


async def test_the_nas_root_is_prepended_to_a_share_relative_path():
    data = _orf()
    nas = FakeNas({"P.ORF": data})
    await _run([_capture(1, "P.ORF", data)], {}, nas=nas)

    assert nas.downloaded == [f"{ROOT}/{DIVE_FOLDER}/P.ORF"]


async def test_the_dive_is_found_in_whichever_served_tenant_holds_it():
    catalog = FakeCatalog([], dives={(REEF, 412): DIVE}, tenants=[LAB, REEF])
    await _run([], {}, catalog=catalog)

    assert catalog.asked == [(REEF, DIVE)]


async def test_a_dive_that_does_not_exist_is_a_non_retryable_error():
    """v1 returned an empty report for an unknown dive id, so a typo read as a
    clean verification of zero frames."""
    with pytest.raises(ApplicationError) as raised:
        await _run([], {}, catalog=FakeCatalog([], dives={}))

    assert raised.value.non_retryable
    assert raised.value.type == "DiveNotFound"


# -- cost control -----------------------------------------------------------------


async def test_a_limit_caps_how_many_frames_are_downloaded():
    """Whole files, ~15 MB each: a sample has to be possible."""
    data = _orf()
    captures = [_capture(i, f"P{i:04d}.ORF", data) for i in range(10)]
    nas = FakeNas({f"P{i:04d}.ORF": data for i in range(10)})

    report = await _run(captures, {}, limit=3, nas=nas)

    assert report.checked == 3
    assert report.total_in_dive == 10
    assert len(nas.downloaded) == 3


async def test_the_progress_is_heartbeated_after_every_frame():
    data = _orf()
    env = ActivityEnvironment()
    beats = []
    env.on_heartbeat = lambda *details: beats.append(details)

    await _run(
        [_capture(1, "A.ORF", data), _capture(2, "B.ORF", data)],
        {"A.ORF": data, "B.ORF": data},
        env=env,
    )

    assert [d[0]["next"] for d in beats] == [0, 1, 2]
    assert beats[-1][0]["report"]["checked"] == 2


async def test_a_retry_resumes_where_the_last_attempt_heartbeated():
    """Whole-file downloads are real bandwidth; a retry must not re-pull what
    the last attempt already checked, and must keep what it found."""
    data = _orf()
    captures = [_capture(i, f"P{i}.ORF", data) for i in range(4)]
    earlier = VerifyChecksumsReport(
        dive_number=412,
        total_in_dive=4,
        checked=2,
        checksum_matched=1,
        mismatches=[{"capture_number": 1, "path": "x", "stored": "0", "computed": "1"}],
    )
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info,
        heartbeat_details=[{"next": 2, "report": earlier.model_dump(mode="json")}],
    )
    nas = FakeNas({f"P{i}.ORF": data for i in range(4)})

    report = await _run(captures, {}, env=env, nas=nas)

    assert nas.downloaded == [
        f"{ROOT}/{DIVE_FOLDER}/P2.ORF",
        f"{ROOT}/{DIVE_FOLDER}/P3.ORF",
    ]
    assert report.checked == 4
    assert report.checksum_matched == 3
    assert len(report.mismatches) == 1


# -- the sweep's selector (v1's select_canonical_dive_ids_activity) ---------------


async def test_the_sweep_takes_every_served_tenants_canonical_dives_in_order():
    catalog = FakeCatalog(
        [], dives={(LAB, 66): DIVE, (REEF, 64): DIVE, (LAB, 11): DIVE},
        tenants=[LAB, REEF],
    )  # fmt: skip

    numbers = await ActivityEnvironment().run(
        _activities(catalog, FakeNas({})).select_canonical_dive_numbers
    )

    assert numbers == [11, 64, 66]


# -- read-only --------------------------------------------------------------------


def test_the_module_imports_no_write_capable_call():
    """Tripwire, mirroring v1's: this runs against production data to answer a
    question, so it must not be able to change the answer."""
    source = inspect.getsource(sut)
    for forbidden in (".upload(", ".delete(", ".post(", ".put(", "upload_bytes",
                      "register_", "record_", "persist_", "apply_"):  # fmt: skip
        assert forbidden not in source, f"verification must stay read-only: {forbidden}"
