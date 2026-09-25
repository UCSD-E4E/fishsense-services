"""Unit tests for preflight -- the gate ingest has to pass.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/tests/
test_preflight_ingest_activity.py. Test names, bodies and reasons are v1's;
the harness changed (a fake **catalog** stands in for v1's fake API client, and
activities are methods of `IngestActivities`), and v2 adaptations are marked:
the camera override is a device serial; the calibration source is a dive's path
and must exist in the tenant; a named slate template must exist; and the
tenant must resolve for the orchestrator (it is a member).

Preflight writes nothing. Its whole job is to decide whether the folder can
become a dive, and to say so completely: **every problem at once, never
first-wins.** An operator submitting a folder from a boat has one round trip
worth of attention, and "fix this, resubmit, discover the next thing" spends it
badly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

from temporalio.testing import ActivityEnvironment

from fishsense_services_orchestrator.ingest.activities import (
    EXIF_HEADER_BYTES,
    DiveFolderListing,
    IngestActivities,
    stored_path,
)
from fishsense_services_orchestrator.ingest.catalog import ResolvedDevice
from fishsense_services_orchestrator.ingest.contracts import (
    IngestDiveRequest,
    SubfolderReport,
)
from fishsense_services_orchestrator.ingest.nas import NasEntry
from fishsense_services_orchestrator.ingest.nas_frames import (
    NasSettings,
    resolve_nas_path,
)

from ._tiff_builder import build_orf

SERIAL = "BJ6C67989"
ROOT = "/fishsense_data/REEF/data"
FOLDER = f"{ROOT}/2024.06.20.REEF/082929_FishModels_FSL07"
TENANT = uuid.uuid4()
DEVICE = uuid.uuid4()
SOURCE_DIVE = uuid.uuid4()
SLATE = uuid.uuid4()


def _settings(root: str = ROOT) -> NasSettings:
    return NasSettings(url="https://nas.test:6021", username="u", password="p",
                       raw_root_path=root)  # fmt: skip


class FakeCatalog:
    """What preflight asks the database, answered from memory."""

    def __init__(self, devices=None, dives=None, slates=None, tenants=None):
        default = {SERIAL: ResolvedDevice(DEVICE, "FSL-07", True)}
        self.devices = default if devices is None else devices
        self.dives = dives or {}  # path -> id
        self.slates = {"H-Slate": SLATE} if slates is None else slates
        self.tenants = {"lab": TENANT} if tenants is None else tenants

    async def resolve_tenant(self, slug):
        return self.tenants.get(slug)

    async def resolve_device(self, tenant_id, serial):
        return self.devices.get(serial)

    async def dive_by_path(self, tenant_id, path):
        return self.dives.get(path)

    async def dives_with_leaf(self, tenant_id, leaf):
        return [(i, p) for p, i in self.dives.items() if p.rsplit("/", 1)[-1] == leaf]

    async def slate_template(self, name):
        return self.slates.get(name)


def _entry(name: str, size: int = 15_000_000, folder: str = FOLDER):
    return NasEntry(path=f"{folder}/{name}", name=name, is_dir=False, size=size)


def _listing(names=("PA010001.ORF", "PA010002.ORF"), subfolders=()):
    return DiveFolderListing(
        folder_path=FOLDER,
        files=[_entry(n) for n in names],
        subfolders=list(subfolders),
    )


def _request(**kwargs):
    kwargs.setdefault("tenant", "lab")
    kwargs.setdefault("dive_path", "2024.06.20.REEF/082929_FishModels_FSL07")
    kwargs.setdefault("self_calibrates", True)
    return IngestDiveRequest(**kwargs)


def _nas(headers=None):
    default = build_orf(
        date_time="2024:08:21 08:56:51", serial_number=SERIAL, artist="FSL-07"
    )
    headers = headers or {}
    nas = MagicMock()
    nas.download_range.side_effect = lambda *, file_path, offset=0, length=0: (
        headers.get(file_path.rsplit("/", 1)[-1], default)
    )
    return nas


async def _run(request, listing, *, catalog=None, headers=None, root=ROOT, nas=None):
    """Drive the activity with canned NAS header bytes, one per listed file.
    Preflight heartbeats per frame, so it needs a real activity context."""
    nas = nas or _nas(headers)
    activities = IngestActivities(
        nas_settings=_settings(root),
        nas_client_factory=lambda: nas,
        catalog=catalog or FakeCatalog(),
    )
    return await ActivityEnvironment().run(activities.preflight, request, listing)


# -- the all-at-once contract ------------------------------------------------


async def test_reports_every_error_at_once_rather_than_the_first():
    """The property the whole activity exists for. A folder with three
    *independent* problems must come back with three errors, not one. The frames
    carry a good serial so the device still resolves, which is what lets the
    missing-calibration check run at all."""
    long_name = "P" + "A" * 300 + ".ORF"

    preflight = await _run(
        _request(self_calibrates=False),  # no calibration intent
        _listing(names=("PA010001.ORF", long_name)),
        catalog=FakeCatalog(devices={SERIAL: ResolvedDevice(DEVICE, "FSL-07", False)}),
    )

    joined = " | ".join(preflight.errors)
    assert "calibration" in joined and "intrinsics" in joined and "255" in joined
    assert len(preflight.errors) == 3


# -- tenant (v2) -------------------------------------------------------------


async def test_a_tenant_the_orchestrator_does_not_serve_fails():
    """v2: ingest into a tenant requires the orchestrator's service principal to
    be a member of it -- explicit, auditable authority, never an RLS bypass.
    Unknown and not-a-member look the same, as they do for people."""
    preflight = await _run(_request(tenant="partner"), _listing())

    assert any("partner" in e for e in preflight.errors)


# -- device resolution ---------------------------------------------------------


async def test_resolves_the_device_from_the_makernote_serial():
    preflight = await _run(_request(), _listing())

    assert preflight.errors == []
    assert preflight.resolved_device_id == DEVICE
    assert preflight.resolved_device_name == "FSL-07"


async def test_an_unknown_serial_fails_and_does_not_fall_back_to_artist():
    """The anti-regression test for the camera decision. `Artist` is a free-text
    rig label; matching on it would resolve a camera whose intrinsics belong to
    different glass, and stage 14 would report confident wrong lengths."""
    catalog = FakeCatalog(devices={"OTHER123": ResolvedDevice(DEVICE, "FSL-07", True)})

    preflight = await _run(_request(), _listing(), catalog=catalog)

    assert preflight.resolved_device_id is None
    assert any(SERIAL in e for e in preflight.errors)


async def test_an_explicit_device_serial_overrides_serial_resolution():
    """The escape hatch for a body whose MakerNote is unreadable, or frames
    copied through a tool that stripped it. v2: named by serial, not row id."""
    other = uuid.uuid4()
    catalog = FakeCatalog(devices={"OTHER123": ResolvedDevice(other, "FSL-03", True)})

    preflight = await _run(
        _request(device_serial="OTHER123"), _listing(), catalog=catalog
    )

    assert preflight.errors == []
    assert preflight.resolved_device_id == other


async def test_a_dive_spanning_two_serials_fails():
    """One folder is one rig. Mixed intrinsics inside a single dive can't be
    expressed in the schema."""
    other = build_orf(
        date_time="2024:08:21 08:56:51", serial_number="XX9Z11111", artist="FSL-03"
    )

    preflight = await _run(_request(), _listing(), headers={"PA010002.ORF": other})

    joined = " ".join(preflight.errors)
    assert "serial" in joined.lower() and SERIAL in joined and "XX9Z11111" in joined


async def test_a_camera_without_intrinsics_fails():
    """Stage 14 needs a camera calibration to exist before the dive is worth
    ingesting; discovering it later means a dive that sits in the cohort
    forever."""
    catalog = FakeCatalog(devices={SERIAL: ResolvedDevice(DEVICE, "FSL-07", False)})

    preflight = await _run(_request(), _listing(), catalog=catalog)

    assert any("intrinsics" in e for e in preflight.errors)


async def test_artist_disagreeing_with_the_resolved_camera_is_a_warning():
    """Not fatal -- the serial is authoritative -- but it means a mislabelled
    device name or a re-housed body, and nothing else would ever notice."""
    catalog = FakeCatalog(devices={SERIAL: ResolvedDevice(DEVICE, "FSL-99", True)})

    preflight = await _run(_request(), _listing(), catalog=catalog)

    assert preflight.errors == []
    assert any("FSL-07" in w and "FSL-99" in w for w in preflight.warnings)


# -- calibration intent ----------------------------------------------------------


async def test_neither_calibration_intent_given_fails():
    """A fish-only dive with no slate frames can never self-calibrate, so stage
    14 can never measure it -- and that is invisible in the files."""
    preflight = await _run(_request(self_calibrates=False), _listing())

    assert any("calibration" in e for e in preflight.errors)


async def test_both_calibration_intents_given_fails():
    """Contradictory intent: own-wins would silently ignore the link."""
    preflight = await _run(
        _request(self_calibrates=True, calibration_source_path="slate/dive"),
        _listing(),
        catalog=FakeCatalog(dives={"slate/dive": SOURCE_DIVE}),
    )

    assert any("calibration" in e for e in preflight.errors)


async def test_borrowing_calibration_alone_is_valid():
    preflight = await _run(
        _request(self_calibrates=False, calibration_source_path="slate/dive"),
        _listing(),
        catalog=FakeCatalog(dives={"slate/dive": SOURCE_DIVE}),
    )

    assert preflight.errors == []
    assert preflight.resolved_calibration_source_dive_id == SOURCE_DIVE


async def test_borrowing_from_a_dive_the_tenant_does_not_have_fails():
    """v2: the source is named by path, so it has to resolve -- a link to
    nothing would be a dive that can never be measured."""
    preflight = await _run(
        _request(self_calibrates=False, calibration_source_path="no/such/dive"),
        _listing(),
    )

    assert any("no/such/dive" in e for e in preflight.errors)


async def test_a_named_slate_template_must_exist():
    """v2: slates are named, so an unknown name is an error, not a NULL."""
    ok = await _run(_request(slate_template="H-Slate"), _listing())
    bad = await _run(_request(slate_template="Z-Slate"), _listing())

    assert ok.errors == [] and ok.resolved_slate_template_id == SLATE
    assert any("Z-Slate" in e for e in bad.errors)


# -- per-frame validation -----------------------------------------------------------


async def test_a_frame_without_a_readable_timestamp_fails():
    """Stage-1 clustering is pure timestamp maths, so a defaulted timestamp
    corrupts it silently. Rejecting the frame is the only safe answer."""
    blind = build_orf(date_time=None, date_time_original=None, serial_number=SERIAL)

    preflight = await _run(_request(), _listing(), headers={"PA010002.ORF": blind})

    assert any("PA010002.ORF" in e for e in preflight.errors)


async def test_the_timestamp_is_the_naive_exif_value_stamped_utc():
    """The convention the existing rows follow: the camera's wall clock,
    labelled UTC, with the recorded offset deliberately NOT applied."""
    preflight = await _run(_request(), _listing())

    assert preflight.images[0].taken_datetime == datetime(
        2024, 8, 21, 8, 56, 51, tzinfo=timezone.utc
    )


async def test_the_camera_offset_is_surfaced_but_not_applied():
    with_offset = build_orf(
        date_time="2024:08:21 08:56:51", offset_time="-08:00", serial_number=SERIAL
    )

    preflight = await _run(
        _request(),
        _listing(names=("PA010001.ORF",)),
        headers={"PA010001.ORF": with_offset},
    )

    assert preflight.images[0].exif_offset == "-08:00"
    assert preflight.images[0].taken_datetime.hour == 8


async def test_a_fallback_timestamp_tag_is_warned_about():
    """0x0132 missing means this body isn't the one the convention was derived
    from. The frame is still usable; the divergence should be visible."""
    fallback = build_orf(
        date_time=None, date_time_original="2024:08:21 08:56:51", serial_number=SERIAL
    )

    preflight = await _run(
        _request(),
        _listing(names=("PA010001.ORF",)),
        headers={"PA010001.ORF": fallback},
    )

    assert preflight.errors == []
    assert any("PA010001.ORF" in w for w in preflight.warnings)


async def test_a_path_over_255_characters_fails_and_names_the_offender():
    """Kept from v1's varchar(255): the research repos and the NAS itself still
    work to that limit, and the offending file is named."""
    long_name = "P" + "A" * 300 + ".ORF"

    preflight = await _run(_request(), _listing(names=("PA010001.ORF", long_name)))

    assert any(long_name in e for e in preflight.errors)


async def test_an_empty_folder_fails():
    """Almost always a mistyped path. Creating an empty dive would leave a row
    that no stage can ever act on."""
    preflight = await _run(_request(), _listing(names=()))

    assert any("No .ORF frames" in e for e in preflight.errors)


# -- reporting ----------------------------------------------------------------------


async def test_reads_only_the_first_megabyte_of_each_frame():
    """What makes a dry run affordable: ~1 MB per file instead of ~15 MB."""
    nas = _nas()

    await _run(_request(), _listing(), nas=nas)

    for call in nas.download_range.call_args_list:
        assert call.kwargs["offset"] == 0
        assert call.kwargs["length"] == EXIF_HEADER_BYTES


async def test_totals_and_subfolders_are_carried_into_the_report():
    listing = _listing(
        subfolders=[SubfolderReport(path=f"{FOLDER}/rollover", orf_count=47)]
    )

    preflight = await _run(_request(), listing)

    assert preflight.total_bytes == 30_000_000
    assert preflight.subfolders[0].orf_count == 47
    assert any("rollover" in w for w in preflight.warnings)


async def test_a_leaf_name_collision_with_an_existing_dive_warns():
    """Layer 1 of duplicate detection. Catches the real prod case: dives 64 and
    66 are both `082929_FishModels_FSL07`."""
    existing = uuid.uuid4()
    catalog = FakeCatalog(dives={"2023.01.01.REEF/082929_FishModels_FSL07": existing})

    preflight = await _run(_request(), _listing(), catalog=catalog)

    assert preflight.errors == []
    assert any(str(existing) in w for w in preflight.warnings)


# -- the stored path has to survive the round trip -----------------------------------


def test_stored_path_round_trips_back_to_the_nas_path_it_came_from():
    """Whatever preflight stores, `resolve_nas_path` must turn back into the
    absolute path the frame was read from. Under the root the stored form is
    share-relative; **outside** it has to stay absolute -- a relative path that
    isn't under the root resolves to a place that does not exist (the
    2025-01-17 pool test is the real case)."""
    settings = _settings()

    under_root = f"{ROOT}/2024.06.20.REEF/082929_FishModels_FSL07/PA010001.ORF"
    assert stored_path(under_root, settings) == (
        "2024.06.20.REEF/082929_FishModels_FSL07/PA010001.ORF"
    )
    assert resolve_nas_path(stored_path(under_root, settings), settings) == under_root

    outside_root = (
        "/fishsense_data/2025.01.17.FishSense.San Diego/ED-00/FSL-10D"
        "/Ginny/P1170188.ORF"
    )
    assert stored_path(outside_root, settings) == outside_root
    assert (
        resolve_nas_path(stored_path(outside_root, settings), settings) == outside_root
    )


async def test_a_folder_outside_the_root_keeps_absolute_frame_paths():
    folder = "/fishsense_data/2025.01.17.FishSense.San Diego/ED-00/FSL-10D/Ginny"
    listing = DiveFolderListing(
        folder_path=folder, files=[_entry("P1170188.ORF", folder=folder)]
    )

    preflight = await _run(_request(dive_path=folder), listing)

    assert preflight.errors == []
    assert preflight.images[0].path == f"{folder}/P1170188.ORF"
