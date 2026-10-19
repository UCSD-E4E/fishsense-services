"""Preflight decides a mission's camera port, and says so (PLAN.md §4.9).

The owner's rules (2026-10-16): a device labelled with the ``D`` suffix is the
corrective optic, and nothing else is known -- a missing ``D`` is not evidence
of a flat port. So preflight takes a ``D`` from the folder path (e4edm's
``ED-00/FSL-10D/...``) or from every frame's EXIF Artist, records an
operator's statement as theirs, refuses a statement that contradicts a ``D``,
and warns when the port stays unknown. Purpose is stored only when stated;
otherwise preflight says what the name suggests.
"""

from __future__ import annotations

from fishsense_services_orchestrator.ingest.activities import MissionFolderListing
from fishsense_services_orchestrator.ingest.catalog import ResolvedDevice

from ._tiff_builder import build_orf
from .test_preflight import (
    DEVICE,
    FOLDER,
    SERIAL,
    FakeCatalog,
    _entry,
    _listing,
    _request,
    _run,
)

PATH = "2024.06.20.REEF/082929_FishModels_FSL07"

D_FOLDER = "/fishsense_data/REEF/data/2025.01.17.FishSense.San Diego/ED-00/FSL-07D/Box"


def _d_listing():
    return MissionFolderListing(
        folder_path=D_FOLDER,
        files=[_entry("PA010001.ORF", folder=D_FOLDER)],
        subfolders=[],
    )


def _artist(artist):
    header = build_orf(
        date_time="2024:08:21 08:56:51", serial_number=SERIAL, artist=artist
    )
    return {"PA010001.ORF": header, "PA010002.ORF": header}


async def test_a_d_on_the_path_is_the_corrective_optic():
    preflight = await _run(
        _request(mission_path="2025.01.17.FishSense.San Diego/ED-00/FSL-07D/Box"),
        _d_listing(),
    )

    assert preflight.errors == []
    assert (preflight.resolved_port, preflight.resolved_port_source) == (
        "corrective_optic",
        "device_suffix",
    )
    assert "FSL-07D" in preflight.resolved_port_note


async def test_a_d_in_every_artist_is_the_corrective_optic_and_names_the_device():
    preflight = await _run(_request(), _listing(), headers=_artist("FSL-07D"))

    assert (preflight.resolved_port, preflight.resolved_port_source) == (
        "corrective_optic",
        "device_suffix",
    )
    # FSL-07D is the body named FSL-07: the suffix is the port, not a mismatch.
    assert not any("disagrees" in w for w in preflight.warnings)


async def test_without_a_d_or_a_statement_the_port_is_unknown_and_said_so():
    preflight = await _run(_request(), _listing())

    assert (preflight.resolved_port, preflight.resolved_port_source) == (
        "unknown",
        None,
    )
    assert any("port is unknown" in w for w in preflight.warnings)


async def test_an_operators_statement_is_theirs():
    preflight = await _run(
        _request(port="flat", port_note="bare flat port, pool test"), _listing()
    )

    assert (
        preflight.resolved_port,
        preflight.resolved_port_source,
        preflight.resolved_port_note,
    ) == ("flat", "operator", "bare flat port, pool test")
    assert not any("port is unknown" in w for w in preflight.warnings)


async def test_a_statement_that_contradicts_a_d_is_an_error():
    preflight = await _run(
        _request(
            mission_path="2025.01.17.FishSense.San Diego/ED-00/FSL-07D/Box",
            port="flat",
        ),
        _d_listing(),
    )

    assert any("FSL-07D" in e and "flat" in e for e in preflight.errors)


async def test_agreeing_with_a_d_records_the_d():
    preflight = await _run(
        _request(
            mission_path="2025.01.17.FishSense.San Diego/ED-00/FSL-07D/Box",
            port="corrective_optic",
        ),
        _d_listing(),
    )

    assert preflight.errors == []
    assert preflight.resolved_port_source == "device_suffix"


async def test_an_unstated_purpose_is_suggested_not_stored():
    preflight = await _run(_request(), _listing())

    assert preflight.resolved_purpose is None
    # FOLDER is 082929_FishModels_FSL07: decoys, so known length.
    assert FOLDER.endswith("FishModels_FSL07")
    assert any("known_length" in w and "purpose" in w for w in preflight.warnings)


async def test_a_stated_purpose_is_kept():
    preflight = await _run(_request(purpose="known_length"), _listing())

    assert preflight.resolved_purpose == "known_length"
    assert not any("purpose" in w for w in preflight.warnings)


async def test_one_tagged_frame_is_not_every_frame():
    """A D in one frame's Artist and none in the rest is partial evidence; a
    port from the owner's rule cannot be changed afterwards, so it must not be
    recorded from part of the folder."""
    tagged = build_orf(
        date_time="2024:08:21 08:56:51", serial_number=SERIAL, artist="FSL-07D"
    )
    untagged = build_orf(
        date_time="2024:08:21 08:56:52", serial_number=SERIAL, artist=None
    )

    preflight = await _run(
        _request(),
        _listing(),
        headers={"PA010001.ORF": tagged, "PA010002.ORF": untagged},
    )

    assert (preflight.resolved_port, preflight.resolved_port_source) == (
        "unknown",
        None,
    )


async def test_a_device_named_with_its_d_matches_its_own_artist():
    catalog = FakeCatalog(devices={SERIAL: ResolvedDevice(DEVICE, "FSL-07D", True)})

    preflight = await _run(
        _request(), _listing(), catalog=catalog, headers=_artist("FSL-07D")
    )

    assert not any("disagrees" in w for w in preflight.warnings)


async def test_contradicting_an_existing_missions_ruled_port_is_a_preflight_error():
    """Every problem at once: a re-ingest stating another port than the
    owner's rule fixed is found here, not when create refuses it."""
    catalog = FakeCatalog(ports={PATH: ("corrective_optic", "backfill")})

    preflight = await _run(_request(port="flat"), _listing(), catalog=catalog)

    assert any("owner's rule" in e and "flat" in e for e in preflight.errors)


async def test_a_re_ingest_that_agrees_or_states_nothing_is_fine():
    catalog = FakeCatalog(ports={PATH: ("corrective_optic", "backfill")})

    agrees = await _run(_request(port="corrective_optic"), _listing(), catalog=catalog)
    silent = await _run(_request(), _listing(), catalog=catalog)

    assert agrees.errors == [] and silent.errors == []


async def test_an_operators_earlier_port_can_be_restated():
    catalog = FakeCatalog(ports={PATH: ("flat", "operator")})

    preflight = await _run(_request(port="dome"), _listing(), catalog=catalog)

    assert preflight.errors == []
