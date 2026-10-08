"""Path repair: point captures whose files moved on the NAS at where they went.

New in v2 (2026-10-07). Frames of dives 219, 237 and 249 were moved into
subfolders of their dive folders after ingest; their rows named files that no
longer existed, and the slate scan stalled on the first such dive. Dive 237
was repaired by hand; this is the same repair, as an ops workflow on the slot,
with the NAS doing the hashing (`Client.md5`) so nothing is downloaded.

What an operator relies on, pinned here:

* **a dry run by default**: it reports what it would change and changes
  nothing; `apply` is explicit;
* a frame is re-pointed only when it is in **exactly one** subfolder of its
  own folder, the NAS's md5 of that file **is the row's checksum**, and **no
  other capture** holds the path. Anything else is reported, by reason;
* the NAS is asked to hash only frames that are not at their path, and each
  folder is listed once, however many frames it holds;
* a re-point names the row, the path it was checked at and the checksum, so a
  row that changed meanwhile is left as it is now and reported;
* a stored path keeps its form: share-relative stays share-relative;
* an unknown dive number is a non-retryable `DiveNotFound`;
* every frame lands in exactly one bucket, so the report accounts for the
  dive.
"""

from __future__ import annotations

import uuid

import pytest
from synology_filestation import DSMError, NoSuchFile
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.capture_path_store import PathCapture
from fishsense_services_orchestrator.ingest.nas import NasEntry
from fishsense_services_orchestrator.ingest.nas_frames import NasSettings
from fishsense_services_orchestrator.ops.paths import activities as sut
from fishsense_services_orchestrator.ops.paths.contracts import MovedFrame

ROOT = "/fishsense_data/REEF/data"
FOLDER = "2024.06.20.REEF/102023_Alligator/101923_Alligator/101923_Alligator_FSL02"
SUB = "101823_Alligator2_FSL02"
LAB = uuid.UUID("7b0c7d4e-3f58-4d8e-9a3c-0c5f1f7f2d11")
DIVE = uuid.UUID("11111111-2222-4333-8444-555555555555")


def _md5(n: int) -> str:
    return f"{n:032x}"


def _capture(number, name, *, folder=FOLDER, checksum=None, algorithm="md5",
             canonical=True) -> PathCapture:  # fmt: skip
    return PathCapture(
        id=uuid.UUID(int=number),
        number=number,
        source_path=f"{folder}/{name}",
        checksum=checksum or _md5(number),
        checksum_algorithm=algorithm,
        is_canonical=canonical,
    )


class FakeCatalog:
    def __init__(self, captures, *, held=None, changed=()):
        self.captures = list(captures)
        #: path -> the capture id holding it, beyond the captures' own paths.
        self.held = dict(held or {})
        #: Capture ids whose row changes before the re-point.
        self.changed = set(changed)
        self.repoints: list[tuple] = []

    async def member_tenants(self):
        return [LAB]

    async def dive_by_number(self, tenant_id, number):
        return DIVE if (tenant_id, number) == (LAB, 249) else None

    async def captures_with_paths(self, tenant_id, dive_id):
        assert (tenant_id, dive_id) == (LAB, DIVE)
        return list(self.captures)

    async def path_holder(self, tenant_id, path):
        for capture in self.captures:
            if capture.source_path == path:
                return capture.id
        return self.held.get(path)

    async def repoint_capture(self, tenant_id, capture_id, *, old_path, new_path,
                              checksum):  # fmt: skip
        self.repoints.append((capture_id, old_path, new_path, checksum))
        return capture_id not in self.changed


class FakeNas:
    """The NAS as a tree: `files` maps a share-relative path to the md5 the
    NAS computes for it. Folders are implied by the paths."""

    def __init__(self, files: dict[str, str]):
        self.files = {f"{ROOT}/{path}": digest for path, digest in files.items()}
        self.listed: list[str] = []
        self.hashed: list[str] = []

    def list_dir(self, *, folder_path):
        self.listed.append(folder_path)
        prefix = folder_path.rstrip("/") + "/"
        children = {}
        for path in self.files:
            if path.startswith(prefix):
                head, _, rest = path[len(prefix) :].partition("/")
                children[head] = bool(rest)
        if not children:
            raise NoSuchFile("no such file or folder")
        return [
            NasEntry(path=prefix + name, name=name, is_dir=is_dir, size=0)
            for name, is_dir in sorted(children.items())
        ]

    def md5(self, *, file_path):
        self.hashed.append(file_path)
        return self.files[file_path]


def _settings():
    return NasSettings(
        url="https://nas.example.test:6021",
        username="svc",
        password="unused",
        raw_root_path=ROOT,
    )


async def _repair(catalog, nas, *, apply=False, dive=249):
    activities = sut.PathRepairActivities(
        catalog=catalog, nas_settings=_settings(), nas_client_factory=lambda: nas
    )
    return await ActivityEnvironment().run(
        activities.repair_moved_capture_paths, dive, apply
    )


def _moved(number, name="P1.ORF", canonical=True) -> MovedFrame:
    return MovedFrame(
        capture_number=number,
        old_path=f"{FOLDER}/{name}",
        new_path=f"{FOLDER}/{SUB}/{name}",
        canonical=canonical,
    )


# -- the dry run, and apply --------------------------------------------------------


async def test_a_frame_at_its_path_is_left_and_never_hashed():
    nas = FakeNas({f"{FOLDER}/P1.ORF": _md5(1)})
    catalog = FakeCatalog([_capture(1, "P1.ORF")])

    report = await _repair(catalog, nas)

    assert (report.frames, report.at_path) == (1, 1)
    assert nas.hashed == []
    assert catalog.repoints == []


async def test_a_dry_run_reports_a_verified_move_and_changes_nothing():
    nas = FakeNas({f"{FOLDER}/{SUB}/P1.ORF": _md5(1)})
    catalog = FakeCatalog([_capture(1, "P1.ORF")])

    report = await _repair(catalog, nas)

    assert report.applied is False
    assert report.would_repair == [_moved(1)]
    assert report.repaired == []
    assert catalog.repoints == []
    assert nas.hashed == [f"{ROOT}/{FOLDER}/{SUB}/P1.ORF"]


async def test_apply_points_the_row_at_where_its_file_went():
    nas = FakeNas({f"{FOLDER}/{SUB}/P1.ORF": _md5(1)})
    catalog = FakeCatalog([_capture(1, "P1.ORF")])

    report = await _repair(catalog, nas, apply=True)

    assert report.applied is True
    assert report.repaired == [_moved(1)]
    assert report.would_repair == []
    assert catalog.repoints == [
        (uuid.UUID(int=1), f"{FOLDER}/P1.ORF", f"{FOLDER}/{SUB}/P1.ORF", _md5(1))
    ]


async def test_an_absolute_path_stays_absolute():
    folder = f"{ROOT}/{FOLDER}"
    nas = FakeNas({f"{FOLDER}/{SUB}/P1.ORF": _md5(1)})
    catalog = FakeCatalog([_capture(1, "P1.ORF", folder=folder)])

    report = await _repair(catalog, nas, apply=True)

    assert catalog.repoints[0][2] == f"{folder}/{SUB}/P1.ORF"
    assert report.repaired[0].new_path == f"{folder}/{SUB}/P1.ORF"


# -- what is left alone, and why ---------------------------------------------------


async def test_a_file_whose_hash_is_not_the_rows_is_not_the_frame():
    nas = FakeNas({f"{FOLDER}/{SUB}/P1.ORF": _md5(99)})
    catalog = FakeCatalog([_capture(1, "P1.ORF")])

    report = await _repair(catalog, nas, apply=True)

    (finding,) = report.checksum_mismatch
    assert finding.capture_number == 1
    assert finding.candidates == [f"{FOLDER}/{SUB}/P1.ORF"]
    assert _md5(99) in finding.detail
    assert catalog.repoints == []


async def test_a_frame_in_no_subfolder_is_not_found():
    nas = FakeNas({f"{FOLDER}/P2.ORF": _md5(2)})
    catalog = FakeCatalog([_capture(1, "P1.ORF"), _capture(2, "P2.ORF")])

    report = await _repair(catalog, nas, apply=True)

    assert [f.capture_number for f in report.not_found] == [1]
    assert report.at_path == 1


async def test_a_frame_in_two_subfolders_is_a_persons_call():
    nas = FakeNas({f"{FOLDER}/a/P1.ORF": _md5(1), f"{FOLDER}/b/P1.ORF": _md5(1)})
    catalog = FakeCatalog([_capture(1, "P1.ORF")])

    report = await _repair(catalog, nas, apply=True)

    (finding,) = report.ambiguous
    assert finding.candidates == [f"{FOLDER}/a/P1.ORF", f"{FOLDER}/b/P1.ORF"]
    assert nas.hashed == []
    assert catalog.repoints == []


async def test_a_path_another_capture_holds_is_not_taken():
    """`UNIQUE (tenant_id, source_path)`: reported, not attempted -- the
    subfolder may have been ingested as a dive of its own."""
    nas = FakeNas({f"{FOLDER}/{SUB}/P1.ORF": _md5(1)})
    catalog = FakeCatalog(
        [_capture(1, "P1.ORF")], held={f"{FOLDER}/{SUB}/P1.ORF": uuid.uuid4()}
    )

    report = await _repair(catalog, nas, apply=True)

    assert [f.capture_number for f in report.path_taken] == [1]
    assert catalog.repoints == []


async def test_a_sha256_row_cannot_be_checked_by_the_nas():
    nas = FakeNas({f"{FOLDER}/{SUB}/P1.ORF": _md5(1)})
    catalog = FakeCatalog([_capture(1, "P1.ORF", checksum="ab" * 32,
                                    algorithm="sha256")])  # fmt: skip

    report = await _repair(catalog, nas, apply=True)

    assert [f.capture_number for f in report.unsupported] == [1]
    assert nas.hashed == []


async def test_a_row_that_changed_before_the_repoint_is_left_as_it_is():
    nas = FakeNas({f"{FOLDER}/{SUB}/P1.ORF": _md5(1)})
    catalog = FakeCatalog([_capture(1, "P1.ORF")], changed={uuid.UUID(int=1)})

    report = await _repair(catalog, nas, apply=True)

    assert [f.capture_number for f in report.changed_since_checked] == [1]
    assert report.repaired == []


async def test_a_folder_that_is_gone_leaves_every_frame_in_it_not_found():
    nas = FakeNas({"elsewhere/P9.ORF": _md5(9)})
    catalog = FakeCatalog([_capture(1, "P1.ORF"), _capture(2, "P2.ORF")])

    report = await _repair(catalog, nas, apply=True)

    assert [f.capture_number for f in report.not_found] == [1, 2]
    assert all("folder" in f.detail for f in report.not_found)


async def test_any_other_nas_error_propagates_for_the_retry_policy():
    class Down(FakeNas):
        def list_dir(self, *, folder_path):
            raise DSMError("Synology API error 502")

    with pytest.raises(DSMError):
        await _repair(FakeCatalog([_capture(1, "P1.ORF")]), Down({}))


# -- the dive as a whole -----------------------------------------------------------


async def test_each_folder_is_listed_once():
    nas = FakeNas({f"{FOLDER}/{SUB}/P{n}.ORF": _md5(n) for n in range(1, 6)})
    catalog = FakeCatalog([_capture(n, f"P{n}.ORF") for n in range(1, 6)])

    report = await _repair(catalog, nas)

    assert len(report.would_repair) == 5
    assert nas.listed == [f"{ROOT}/{FOLDER}", f"{ROOT}/{FOLDER}/{SUB}"]


async def test_every_frame_lands_in_exactly_one_bucket():
    nas = FakeNas({
        f"{FOLDER}/P1.ORF": _md5(1),            # at its path
        f"{FOLDER}/{SUB}/P2.ORF": _md5(2),      # moved, verified
        f"{FOLDER}/{SUB}/P3.ORF": _md5(99),     # a different file of that name
        f"{FOLDER}/a/P4.ORF": _md5(4),          # in two subfolders
        f"{FOLDER}/b/P4.ORF": _md5(4),
    })  # fmt: skip
    catalog = FakeCatalog([_capture(n, f"P{n}.ORF") for n in range(1, 6)])

    report = await _repair(catalog, nas)

    assert report.frames == 5
    assert report.at_path + len(report.would_repair) + report.left_alone == 5


async def test_an_unknown_dive_is_a_non_retryable_error():
    with pytest.raises(ApplicationError) as excinfo:
        await _repair(FakeCatalog([]), FakeNas({}), dive=9999)
    assert excinfo.value.type == "DiveNotFound"
    assert excinfo.value.non_retryable
