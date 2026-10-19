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

from ._tiff_builder import build_orf
from .test_preflight import FOLDER, SERIAL, _entry, _listing, _request, _run

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
