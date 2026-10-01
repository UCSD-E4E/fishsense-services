"""Unit tests for listing a dive folder -- the first step of ingest.

Ported from fishsense-lite@a8b2c3bc services/fishsense-api-workflow-worker/tests/
test_list_dive_folder_activity.py. Test bodies and reasons are v1's; only the
harness changed: v2 activities are methods of `IngestActivities`, which is
given its NAS settings and client factory instead of reading globals, and a
request names its tenant.

The behaviour under test is almost entirely about what ingest *refuses* to do.

**One request means one dive, and a dive is exactly one directory.** That is
not a simplification: spider walked the tree recursively but then assigned
`dive = image.parent.relative_to(data_root)` (`discovery.py:238`), so a nested
folder always became its own separate dive row, never extra frames on the
parent. Attaching a subdirectory's frames to the named dive would merge dives
that are distinct rows in prod today — silently, and with no way to tell
afterwards which frames came from where.

So subdirectories holding `.ORF`s are *reported* and not ingested. That is the
Olympus rollover case: the TG-6 wraps its frame counter at `PA199999` and
starts a child folder mid-dive, which reads as "more frames of this dive" to a
human and as "a second dive" to every convention in the database.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from temporalio.exceptions import ApplicationError


def _entry(path: str, *, is_dir: bool = False, size: int = 1024):
    from fishsense_services_orchestrator.ingest.nas import NasEntry

    return NasEntry(
        path=path,
        name=path.rstrip("/").rsplit("/", 1)[-1],
        is_dir=is_dir,
        size=size,
    )


def _listing_client(tree: dict[str, list]):
    """A fake NasClient whose `list_dir` serves a canned folder tree.

    Keyed by the folder path so a test can assert *which* folders were listed —
    the non-recursion property is about the calls made, not just the result.
    """
    client = MagicMock()
    client.listed = []

    def _list_dir(*, folder_path: str):
        client.listed.append(folder_path)
        if folder_path not in tree:
            raise AssertionError(f"unexpected list_dir({folder_path!r})")
        return tree[folder_path]

    client.list_dir.side_effect = _list_dir
    return client


async def _run(request, client, monkeypatch):
    from fishsense_services_orchestrator.ingest.activities import IngestActivities
    from fishsense_services_orchestrator.ingest.nas_frames import NasSettings

    settings = NasSettings(
        url="https://nas.example.test:6021",
        username="u",
        password="p",
        raw_root_path=ROOT,
    )
    activities = IngestActivities(
        nas_settings=settings, nas_client_factory=lambda: client
    )
    return await activities.list_dive_folder(request)


def _request(**kwargs):
    from fishsense_services_orchestrator.ingest.contracts import IngestDiveRequest

    kwargs.setdefault("tenant", "lab")
    kwargs.setdefault("dive_path", "2024.06.20.REEF/082929_FishModels_FSL07")
    kwargs.setdefault("self_calibrates", True)
    return IngestDiveRequest(**kwargs)


ROOT = "/fishsense_data/REEF/data"
FOLDER = f"{ROOT}/2024.06.20.REEF/082929_FishModels_FSL07"


# ── what counts as a frame ────────────────────────────────────────────


async def test_matches_orf_case_insensitively(monkeypatch):
    """Olympus writes `.ORF`; operators and copy tools produce `.orf`. A
    case-sensitive match would silently ingest a partial dive, which
    `finalize` cannot detect because the missing frames were never listed."""
    client = _listing_client(
        {
            FOLDER: [
                _entry(f"{FOLDER}/PA010001.ORF"),
                _entry(f"{FOLDER}/PA010002.orf"),
                _entry(f"{FOLDER}/PA010003.Orf"),
            ]
        }
    )

    listing = await _run(_request(), client, monkeypatch)

    assert [f.name for f in listing.files] == [
        "PA010001.ORF",
        "PA010002.orf",
        "PA010003.Orf",
    ]


async def test_ignores_files_that_are_not_orf(monkeypatch):
    """Dive folders routinely carry JPEGs, `.MOV` clips and Finder debris.
    Only raws are frames."""
    client = _listing_client(
        {
            FOLDER: [
                _entry(f"{FOLDER}/PA010001.ORF"),
                _entry(f"{FOLDER}/PA010001.JPG"),
                _entry(f"{FOLDER}/notes.txt"),
                _entry(f"{FOLDER}/.DS_Store"),
            ]
        }
    )

    listing = await _run(_request(), client, monkeypatch)

    assert [f.name for f in listing.files] == ["PA010001.ORF"]


async def test_files_are_returned_in_a_deterministic_order(monkeypatch):
    """Batching and heartbeat-resume both index into this list, so a retry
    that saw a different order would re-download frames it had already
    registered and skip ones it hadn't. DSM does not promise an order."""
    client = _listing_client(
        {
            FOLDER: [
                _entry(f"{FOLDER}/PA010003.ORF"),
                _entry(f"{FOLDER}/PA010001.ORF"),
                _entry(f"{FOLDER}/PA010002.ORF"),
            ]
        }
    )

    listing = await _run(_request(), client, monkeypatch)

    assert [f.name for f in listing.files] == [
        "PA010001.ORF",
        "PA010002.ORF",
        "PA010003.ORF",
    ]


# ── the non-recursion rule ────────────────────────────────────────────


async def test_a_subfolder_of_orfs_is_reported_not_ingested(monkeypatch):
    """The rollover case. Its frames must NOT join this dive — under the
    convention every existing row follows, that folder is its own dive."""
    sub = f"{FOLDER}/101923_Alligator1_FSL06"
    client = _listing_client(
        {
            FOLDER: [
                _entry(f"{FOLDER}/PA010001.ORF"),
                _entry(sub, is_dir=True),
            ],
            sub: [
                _entry(f"{sub}/PA199998.ORF"),
                _entry(f"{sub}/PA199999.ORF"),
            ],
        }
    )

    listing = await _run(_request(), client, monkeypatch)

    assert [f.name for f in listing.files] == ["PA010001.ORF"]
    assert len(listing.subfolders) == 1
    assert listing.subfolders[0].path == sub
    assert listing.subfolders[0].orf_count == 2


async def test_a_subfolder_without_orfs_is_not_reported(monkeypatch):
    """`.thumbnails`, `__MACOSX` and the like are noise, not a missed dive.
    Reporting them would train operators to ignore the warning."""
    sub = f"{FOLDER}/.thumbnails"
    client = _listing_client(
        {
            FOLDER: [
                _entry(f"{FOLDER}/PA010001.ORF"),
                _entry(sub, is_dir=True),
            ],
            sub: [_entry(f"{sub}/PA010001_thumb.jpg")],
        }
    )

    listing = await _run(_request(), client, monkeypatch)

    assert listing.subfolders == []


async def test_does_not_descend_past_the_immediate_children(monkeypatch):
    """Counting a subfolder's frames needs one level. Going deeper would
    turn a mis-typed path near the share root into a walk of the entire NAS,
    over a download backend that falls over under load."""
    sub = f"{FOLDER}/rollover"
    deeper = f"{sub}/deeper"
    client = _listing_client(
        {
            FOLDER: [_entry(sub, is_dir=True)],
            sub: [
                _entry(f"{sub}/PA199999.ORF"),
                _entry(deeper, is_dir=True),
            ],
        }
    )

    listing = await _run(_request(), client, monkeypatch)

    assert client.listed == [FOLDER, sub]
    assert listing.subfolders[0].orf_count == 1


# ── path resolution ───────────────────────────────────────────────────


async def test_the_request_path_is_resolved_against_the_nas_raw_root(monkeypatch):
    """`Image.path` is stored share-relative but FileStation needs absolute —
    the same asymmetry `stage_raw_bytes_for_dive_activity` handles. Getting it
    wrong surfaces as a 502, not a 404."""
    client = _listing_client({FOLDER: []})

    await _run(_request(), client, monkeypatch)

    assert client.listed == [FOLDER]


async def test_an_absolute_request_path_is_not_double_prefixed(monkeypatch):
    """An operator pasting a full NAS path is the obvious mistake to absorb."""
    client = _listing_client({FOLDER: []})

    await _run(_request(dive_path=FOLDER), client, monkeypatch)

    assert client.listed == [FOLDER]


# ── failure ───────────────────────────────────────────────────────────


async def test_a_missing_folder_fails_non_retryably(monkeypatch):
    """Synology 408 is "no such file". A mistyped path cannot be fixed by
    waiting, so Temporal must not burn its retry budget on it — the same
    classification the staging activity makes."""
    from synology_filestation import DSMError

    client = MagicMock()
    client.list_dir.side_effect = DSMError("Synology API error 408")

    with pytest.raises(ApplicationError) as excinfo:
        await _run(_request(), client, monkeypatch)

    assert excinfo.value.non_retryable
    assert "408" in str(excinfo.value)


async def test_a_transient_nas_error_propagates_for_temporal_to_retry(monkeypatch):
    """502 is FileStation's shared download backend falling over — routine and
    self-healing. It must reach Temporal's bounded jittered policy rather than
    being converted into a permanent failure."""
    from synology_filestation import DSMError

    client = MagicMock()
    client.list_dir.side_effect = DSMError("Synology API error 502")

    with pytest.raises(DSMError):
        await _run(_request(), client, monkeypatch)


async def test_an_empty_folder_lists_cleanly_rather_than_failing(monkeypatch):
    """Preflight is what reports "no frames here", with the rest of its
    findings. Failing at listing would hand the operator one error at a time —
    the opposite of the all-at-once contract."""
    client = _listing_client({FOLDER: []})

    listing = await _run(_request(), client, monkeypatch)

    assert listing.files == []
    assert listing.subfolders == []
