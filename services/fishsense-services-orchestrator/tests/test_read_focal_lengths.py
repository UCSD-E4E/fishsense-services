"""The focal-length backfill: each NAS frame's EXIF focal length, read from its
first 4 KB and (with ``apply``) recorded.

New in v2 (2026-10-25). The zoom gate needs every frame's focal length; ingest
records it from now on, and this reads it back for the ~131k frames ingested
before. Pinned here:

* **a dry run unless told otherwise** -- it reads and reports, writes nothing;
* 4 KB a frame, never the whole file (the NAS moves ~1 MB/s);
* the report counts frames by focal length, and names the zoomed ones' count,
  the unreadable and the missing -- every frame in exactly one bucket;
* the write fills an unknown value only (the store's rule), and a frame
  written meanwhile is counted, not overwritten;
* a missing file is a finding; any other NAS error propagates for the retry.
"""

from __future__ import annotations

import uuid

import pytest
from synology_filestation import DSMError, NoSuchFile
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.focal_length_store import (
    FocalLengthFrame,
    FocalLengthMission,
)
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings
from fishsense_services_orchestrator.ops.focal_lengths.activities import (
    FOCAL_HEADER_BYTES,
    FOCAL_RETRY_BYTES,
    FocalLengthActivities,
)
from fishsense_services_orchestrator.ops.focal_lengths.contracts import (
    FocalLengthMissionRef,
)

from ._tiff_builder import build_orf

ROOT = "/fishsense_data/REEF/data"
LAB = uuid.uuid4()
MISSION = uuid.uuid4()
DIVE = "2024.06.20.REEF/062024_Alligator Anchor_FSL04"


class FakeCatalog:
    def __init__(self, frames, *, taken=()):
        self.frames = frames
        self.taken = set(taken)
        self.recorded: dict[uuid.UUID, float] = {}

    async def member_tenants(self):
        return [LAB]

    async def missions_without_focal_lengths(self, tenant_id):
        assert tenant_id == LAB
        return [FocalLengthMission(MISSION, 363, len(self.frames))]

    async def frames_without_focal_length(self, tenant_id, mission_id):
        assert (tenant_id, mission_id) == (LAB, MISSION)
        return list(self.frames)

    async def record_focal_length(self, tenant_id, capture_id, focal_length_mm):
        if capture_id in self.taken:
            return False
        self.recorded[capture_id] = focal_length_mm
        return True


class FakeNas:
    def __init__(self, files, *, error=None):
        self.files = files
        self.error = error
        self.reads: list[tuple[str, int, int]] = []

    def download_range(self, *, file_path, offset=0, length=0):
        self.reads.append((file_path, offset, length))
        if self.error is not None:
            raise self.error
        name = file_path.rsplit("/", 1)[-1]
        if name not in self.files:
            raise NoSuchFile(file_path)
        data = self.files[name]
        return data[offset : offset + length] if length else data[offset:]


def _frames(*names):
    return [FocalLengthFrame(uuid.uuid4(), f"{DIVE}/{n}") for n in names]


def _activities(catalog, nas):
    return FocalLengthActivities(
        catalog=catalog,
        nas_settings=NasSettings(url="https://nas.test:6021", username="u",
                                 password="p", raw_root_path=ROOT),
        nas_client_factory=lambda: nas,
    )  # fmt: skip


async def _read(catalog, nas, apply=False):
    ref = FocalLengthMissionRef(
        tenant_id=str(LAB), mission_id=str(MISSION), mission_number=363, frames=0
    )
    return await ActivityEnvironment().run(
        _activities(catalog, nas).read_mission_focal_lengths, ref, apply
    )


def _orf(focal, *, serial="BJ6C67989"):
    return build_orf(focal_length=focal, serial_number=serial) + b"\0" * 80_000


def _tg7(focal):
    """A TG-7's Exif IFD sits past the first 4 KB (missions 480-487,
    2026-10-25): here, behind a long MakerNote."""
    data = _orf(focal, serial="X" * 6_000)
    from fishsense_services_orchestrator.ingest.exif import read_exif

    assert read_exif(data[:4096]).focal_length_mm is None
    return data


async def test_the_missions_to_read_come_from_every_tenant_served():
    catalog = FakeCatalog(_frames("P1.ORF", "P2.ORF"))

    refs = await ActivityEnvironment().run(
        _activities(catalog, FakeNas({})).focal_length_missions
    )

    assert refs == [
        FocalLengthMissionRef(
            tenant_id=str(LAB), mission_id=str(MISSION), mission_number=363, frames=2
        )
    ]


async def test_a_dry_run_reads_and_counts_and_writes_nothing():
    catalog = FakeCatalog(_frames("P1.ORF", "P2.ORF", "P3.ORF"))
    nas = FakeNas({"P1.ORF": _orf((450, 100)), "P2.ORF": _orf((450, 100)),
                   "P3.ORF": _orf((1462, 100))})  # fmt: skip

    report = await _read(catalog, nas)

    assert report.applied is False
    assert (report.frames, report.zoomed) == (3, 1)
    assert report.focal_lengths == {"4.50": 2, "14.62": 1}
    assert report.recorded == 0
    assert catalog.recorded == {}


async def test_only_the_first_4_kb_of_each_frame_is_read():
    catalog = FakeCatalog(_frames("P1.ORF"))
    nas = FakeNas({"P1.ORF": _orf((450, 100))})

    await _read(catalog, nas)

    assert nas.reads == [(f"{ROOT}/{DIVE}/P1.ORF", 0, FOCAL_HEADER_BYTES)]
    assert FOCAL_HEADER_BYTES == 4096


async def test_a_tg7_frame_is_read_again_further_in():
    """4 KB holds a TG-6's FocalLength but not a TG-7's: a frame with none in
    4 KB is read again to `FOCAL_RETRY_BYTES` before it is unreadable."""
    frames = _frames("P1.ORF")
    catalog = FakeCatalog(frames)
    nas = FakeNas({"P1.ORF": _tg7((450, 100))})

    report = await _read(catalog, nas, apply=True)

    assert report.focal_lengths == {"4.50": 1}
    assert report.unreadable_count == 0
    assert [length for _, _, length in nas.reads] == [4096, FOCAL_RETRY_BYTES]
    assert FOCAL_RETRY_BYTES == 65536
    assert catalog.recorded == {frames[0].id: pytest.approx(4.5)}


async def test_apply_records_each_frames_focal_length():
    frames = _frames("P1.ORF", "P2.ORF")
    catalog = FakeCatalog(frames)
    nas = FakeNas({"P1.ORF": _orf((450, 100)), "P2.ORF": _orf((1800, 100))})

    report = await _read(catalog, nas, apply=True)

    assert report.applied is True
    assert report.recorded == 2
    assert catalog.recorded == {
        frames[0].id: pytest.approx(4.5),
        frames[1].id: pytest.approx(18.0),
    }


async def test_a_frame_recorded_meanwhile_is_counted_not_overwritten():
    frames = _frames("P1.ORF")
    catalog = FakeCatalog(frames, taken=[frames[0].id])

    report = await _read(catalog, FakeNas({"P1.ORF": _orf((450, 100))}), apply=True)

    assert (report.recorded, report.already_known) == (0, 1)


async def test_an_unreadable_or_missing_frame_is_a_finding():
    catalog = FakeCatalog(_frames("P1.ORF", "P2.ORF", "GONE.ORF"))
    nas = FakeNas({"P1.ORF": _orf((450, 100)), "P2.ORF": _orf(None)})

    report = await _read(catalog, nas, apply=True)

    assert report.focal_lengths == {"4.50": 1}
    assert report.unreadable == [f"{DIVE}/P2.ORF"]
    assert report.not_found == [f"{DIVE}/GONE.ORF"]
    assert report.recorded == 1


async def test_a_transient_nas_error_propagates_for_the_retry():
    catalog = FakeCatalog(_frames("P1.ORF"))

    with pytest.raises(DSMError):
        await _read(catalog, FakeNas({}, error=DSMError("Synology API error 502")))


def test_the_module_never_writes_to_the_nas():
    """Read-only by policy: the only NAS call is a ranged read."""
    import inspect

    from fishsense_services_orchestrator.ops.focal_lengths import activities

    source = inspect.getsource(activities)
    for call in ("upload", "delete", "rename", "move", "create_folder"):
        assert f".{call}(" not in source
