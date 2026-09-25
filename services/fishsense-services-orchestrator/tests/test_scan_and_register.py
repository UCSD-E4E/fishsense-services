"""`scan_and_register` -- the only ingest step that downloads whole frames.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/tests/
test_scan_and_register_images_activity.py. Test names, bodies and reasons are
v1's; the harness changed (a recording fake **catalog** stands in for v1's fake
API client, and the NAS client comes from the factory). v2 adaptations are
marked: the batch names its tenant and device, and ids are UUIDs.

Everything before it reads: listing enumerates, preflight reads 1 MB headers and
decides. This one pulls whole files and creates capture rows, so its failure
modes are the expensive ones.

Three properties carry the weight:

  * **Skip without downloading.** A re-run over an already-ingested folder must
    cost nothing. Checking after the download would still be correct and would
    still move ~14.5 MB per frame across the NAS for no reason.
  * **Resume is DB-backed.** A retry skips what was actually persisted, never
    what a heartbeat index claims.
  * **Reject, never default.** A frame with no readable timestamp is refused.
    Stage-1 clustering is pure timestamp arithmetic, so a fabricated value
    corrupts it silently -- and `finalize` refuses to promote a dive with any
    rejection, so a bad frame stops the dive rather than entering it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from synology_filestation import DSMError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.ingest_store import RegisteredCapture
from fishsense_services_orchestrator.ingest import activities as sut
from fishsense_services_orchestrator.ingest.activities import IngestActivities
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings

from ._tiff_builder import build_orf

TENANT = uuid.uuid4()
DIVE = uuid.uuid4()
DEVICE = uuid.uuid4()
ROOT = "/fishsense_data/REEF/data"
FOLDER = "2024.06.20.REEF/082929_FishModels_FSL07"
EXISTING_AT = datetime(2024, 8, 21, 7, 0, 0, tzinfo=timezone.utc)


def _orf(date_time="2024:08:21 08:56:51") -> bytes:
    """Padded past one hash chunk so the streaming read is exercised."""
    return build_orf(date_time=date_time, serial_number="BJ6C67989") + b"\0" * 40_000


class RecordingCatalog:
    """`registered_captures` is how existing paths are discovered -- one call
    per batch rather than one lookup per frame."""

    def __init__(self, existing_paths=()):
        # Real rows carry a timestamp, and skipped frames must still feed the
        # batch max -- see test_a_fully_ingested_rerun_still_reports_the_max.
        self.existing = {p: EXISTING_AT for p in existing_paths}
        self.registered: list[dict] = []

    async def registered_captures(self, tenant_id, dive_id):
        assert (tenant_id, dive_id) == (TENANT, DIVE)
        return dict(self.existing)

    async def register_capture(self, tenant_id, **capture):
        self.registered.append({"tenant_id": tenant_id, **capture})
        return RegisteredCapture(uuid.uuid4(), True)


def _nas(contents: dict[str, bytes], errors: dict[str, int] | None = None):
    nas = MagicMock()
    nas.downloaded = []

    def _download_to(*, src_path: str, dest_dir: str):
        name = src_path.rsplit("/", 1)[-1]
        nas.downloaded.append(src_path)
        code = (errors or {}).get(name)
        if code is not None:
            raise DSMError(f"Synology API error {code}")
        Path(dest_dir, name).write_bytes(contents[name])

    nas.download_to.side_effect = _download_to
    return nas


async def _run(paths, contents, *, catalog=None, errors=None, env=None):
    nas = _nas(contents, errors)
    activities = IngestActivities(
        nas_settings=NasSettings(url="https://nas.test:6021", username="u",
                                 password="p", raw_root_path=ROOT),
        nas_client_factory=lambda: nas,
        catalog=catalog or RecordingCatalog(),
    )  # fmt: skip
    result = await (env or ActivityEnvironment()).run(
        activities.scan_and_register,
        TENANT,
        DIVE,
        [f"{FOLDER}/{p}" for p in paths],
        DEVICE,
    )
    nas.downloaded = [p.rsplit("/", 1)[-1] for p in nas.downloaded]
    return result, nas


# -- registering ---------------------------------------------------------------


async def test_registers_a_frame_with_its_checksum_and_timestamp():
    data = _orf()
    catalog = RecordingCatalog()

    result, _ = await _run(["A.ORF"], {"A.ORF": data}, catalog=catalog)

    assert result.registered == 1
    assert result.rejected == []
    (capture,) = catalog.registered
    assert capture == {
        "tenant_id": TENANT,
        "dive_id": DIVE,
        "device_id": DEVICE,
        "source_path": f"{FOLDER}/A.ORF",
        "captured_at": datetime(2024, 8, 21, 8, 56, 51, tzinfo=timezone.utc),
        "checksum": hashlib.md5(data).hexdigest(),
    }
    # The store computes canonicality; there is no way to send it. v1 had to
    # remember not to -- sending it would mark every duplicate canonical and
    # destroy the distinction canonical-only pipeline gating depends on.
    assert "is_canonical" not in capture


async def test_downloads_the_resolved_nas_path():
    """v2: stored paths are share-relative; the NAS needs them absolute."""
    nas_calls = []
    catalog = RecordingCatalog()
    nas = _nas({"A.ORF": _orf()})
    original = nas.download_to.side_effect
    nas.download_to.side_effect = lambda **kw: (nas_calls.append(kw), original(**kw))
    activities = IngestActivities(
        nas_settings=NasSettings(url="https://nas.test:6021", username="u",
                                 password="p", raw_root_path=ROOT),
        nas_client_factory=lambda: nas,
        catalog=catalog,
    )  # fmt: skip

    await ActivityEnvironment().run(
        activities.scan_and_register, TENANT, DIVE, [f"{FOLDER}/A.ORF"], DEVICE
    )

    assert nas_calls[0]["src_path"] == f"{ROOT}/{FOLDER}/A.ORF"


async def test_the_checksum_is_md5_of_the_whole_file():
    """The convention every migrated row follows. A reader that hashed only the
    header would agree with itself forever and disagree with all ~131k rows --
    and duplicate detection would silently report zero overlap."""
    data = _orf()
    catalog = RecordingCatalog()

    await _run(["A.ORF"], {"A.ORF": data}, catalog=catalog)

    assert catalog.registered[0]["checksum"] == hashlib.md5(data).hexdigest()


async def test_reports_the_max_timestamp_for_the_dive():
    """The dive's datetime is the MAX of its frames -- how every existing dive
    row was derived. `finalize` needs it and only this step reads whole files."""
    early = _orf("2024:08:21 08:00:00")
    late = _orf("2024:08:21 09:30:00")

    result, _ = await _run(["A.ORF", "B.ORF"], {"A.ORF": early, "B.ORF": late})

    assert result.max_taken_datetime == datetime(
        2024, 8, 21, 9, 30, 0, tzinfo=timezone.utc
    )


# -- skipping ------------------------------------------------------------------


async def test_an_already_registered_path_is_skipped_without_downloading():
    """The property that makes a re-run cheap. Checking after the download
    would be just as correct and would still move ~14.5 MB per frame."""
    data = _orf()
    catalog = RecordingCatalog(existing_paths=[f"{FOLDER}/A.ORF"])

    result, nas = await _run(
        ["A.ORF", "B.ORF"], {"A.ORF": data, "B.ORF": data}, catalog=catalog
    )

    assert result.skipped_existing == 1
    assert result.registered == 1
    assert nas.downloaded == ["B.ORF"]


async def test_a_fully_ingested_batch_downloads_nothing():
    data = _orf()
    catalog = RecordingCatalog(existing_paths=[f"{FOLDER}/A.ORF", f"{FOLDER}/B.ORF"])

    result, nas = await _run(
        ["A.ORF", "B.ORF"], {"A.ORF": data, "B.ORF": data}, catalog=catalog
    )

    assert result.skipped_existing == 2
    assert result.registered == 0
    assert nas.downloaded == []
    assert catalog.registered == []


# -- rejecting -----------------------------------------------------------------


async def test_a_frame_with_no_readable_timestamp_is_rejected_not_defaulted():
    """Stage-1 clustering is pure timestamp arithmetic. `finalize` refuses to
    promote a dive with any rejection, so this stops the dive rather than
    quietly seeding a frame that will cluster wrongly forever."""
    blind = build_orf(date_time=None, date_time_original=None) + b"\0" * 40_000
    catalog = RecordingCatalog()

    result, _ = await _run(["A.ORF"], {"A.ORF": blind}, catalog=catalog)

    assert result.registered == 0
    assert [r.path for r in result.rejected] == [f"{FOLDER}/A.ORF"]
    assert catalog.registered == []


async def test_one_rejected_frame_does_not_stop_the_others():
    """The batch reports everything it saw; `finalize` decides. Aborting here
    would hide how many frames are affected behind whichever failed first."""
    good, blind = _orf(), build_orf(date_time=None, date_time_original=None)

    result, _ = await _run(
        ["A.ORF", "B.ORF", "C.ORF"],
        {"A.ORF": good, "B.ORF": blind + b"\0" * 40_000, "C.ORF": good},
    )

    assert result.registered == 2
    assert len(result.rejected) == 1


# -- NAS failure classification ------------------------------------------------


async def test_a_missing_file_fails_non_retryably():
    """Synology 408 is "no such file" -- waiting cannot fix a path that isn't
    there, so Temporal must not burn its retry budget. Unlike verification,
    ingest is trying to DO something: a missing frame is a failure, not a
    finding."""
    with pytest.raises(ApplicationError) as excinfo:
        await _run(["A.ORF"], {}, errors={"A.ORF": 408})

    assert excinfo.value.non_retryable


async def test_a_transient_nas_error_propagates_for_temporal_to_retry():
    """502 is the shared backend having a moment -- routine and self-healing. It
    must reach the bounded jittered policy rather than becoming permanent."""
    with pytest.raises(DSMError):
        await _run(["A.ORF"], {}, errors={"A.ORF": 502})


# -- heartbeat and resume ------------------------------------------------------


async def test_heartbeats_the_index_of_each_frame():
    """A batch is many whole-file downloads; the heartbeat is liveness and
    progress, so a stuck download is noticed."""
    beats = []
    env = ActivityEnvironment()
    env.on_heartbeat = beats.append
    data = _orf()

    await _run(["A.ORF", "B.ORF"], {"A.ORF": data, "B.ORF": data}, env=env)

    assert beats == [0, 1]


async def test_resume_is_db_backed_not_heartbeat_backed():
    """A retry must skip what was actually PERSISTED, not what a heartbeat
    index claims.

    Skipping frames below the heartbeat index would drop their outcomes from
    the batch result -- so a transient 502 mid-batch could erase an earlier
    no-EXIF rejection, and `finalize` would then see no rejections and promote
    a dive with a missing frame. That is precisely the guard `finalize` exists
    to be.

    Here a prior attempt registered A but rejected B (no EXIF). On retry, even
    with a heartbeat index past both: A is skipped because the DB says so, and
    B is re-read and re-rejected rather than silently forgotten.
    """
    good = _orf()
    blind = build_orf(date_time=None, date_time_original=None) + b"\0" * 40_000
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, heartbeat_details=[2])
    catalog = RecordingCatalog(existing_paths=[f"{FOLDER}/A.ORF"])

    result, nas = await _run(
        ["A.ORF", "B.ORF", "C.ORF"],
        {"A.ORF": good, "B.ORF": blind, "C.ORF": good},
        catalog=catalog,
        env=env,
    )

    assert nas.downloaded == ["B.ORF", "C.ORF"]
    assert result.skipped_existing == 1
    assert result.registered == 1
    assert [r.path for r in result.rejected] == [f"{FOLDER}/B.ORF"]


async def test_a_fully_ingested_rerun_still_reports_the_max_timestamp():
    """The dive's datetime is the MAX over the dive, and `finalize` gets it
    from here. Counting only newly-registered frames returned None for a
    fully-ingested batch, which would leave the datetime unset on any re-run --
    and something too early whenever a batch was partly skipped."""
    data = _orf()
    catalog = RecordingCatalog(existing_paths=[f"{FOLDER}/A.ORF", f"{FOLDER}/B.ORF"])

    result, nas = await _run(
        ["A.ORF", "B.ORF"], {"A.ORF": data, "B.ORF": data}, catalog=catalog
    )

    assert nas.downloaded == []
    assert result.registered == 0
    assert result.max_taken_datetime == EXISTING_AT


async def test_a_skipped_frame_can_hold_the_batch_max():
    """Mixed batch: the existing row is later than the new one, so the max has
    to come from the frame that was never downloaded."""
    early = _orf("2024:08:21 06:00:00")
    catalog = RecordingCatalog(existing_paths=[f"{FOLDER}/A.ORF"])

    result, _ = await _run(
        ["A.ORF", "B.ORF"], {"A.ORF": early, "B.ORF": early}, catalog=catalog
    )

    assert result.registered == 1
    assert result.max_taken_datetime == EXISTING_AT


# -- read-only where it must be ------------------------------------------------


def test_the_module_never_writes_to_the_nas():
    """The orchestrator's ingest reads the NAS and never writes to it: it
    downloads and must never upload or delete."""
    source = inspect.getsource(sut)
    for forbidden in (".upload(", ".delete(", "upload_bytes", "create_folder"):
        assert forbidden not in source, f"NAS must stay read-only: {forbidden}"
