"""The slate detector's orchestrator activities: select, resolve, persist.

New in v2 (the model is 2026-10-03_slate_detector@95a77d95's presence
classifier), built as laser and species prediction's are, and pinned here:

* the selector takes the oldest dive across every tenant served, asking each
  for the current `SLATE_DETECTOR_VERSION`;
* the resolver hands the processor each frame's staged raw -- a key the
  orchestrator issued (PLAN.md §9.11) -- and the dive's pinhole intrinsics; a
  dive it cannot resolve is a final refusal;
* each result is persisted as the processor stamped it (an older processor's
  version is stale, and re-predicted); a store refusal is final.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from fishsense_services_api.slate_presence_store import (
    ForeignCapture,
    SlateDetectionCandidate,
    SlateDetectionCapture,
    SlateDetectionInputs,
    SlateDetectionUnavailable,
    SlatePresenceRow,
)
from fishsense_services_contracts.slate_presence import (
    SLATE_DETECTOR_VERSION,
    DetectSlateImage,
    SlatePresenceResult,
)
from fishsense_services_orchestrator.object_store.contracts import StagingTarget
from fishsense_services_orchestrator.slate_detect.activities import (
    SlateDetectionActivities,
)

from ._species import LAYOUT

T0 = datetime(2026, 9, 1, tzinfo=UTC)
LAB, REEF = uuid.uuid4(), uuid.uuid4()
DIVE = uuid.uuid4()
TARGET = StagingTarget(LAB, DIVE)
K = [[3500.0, 0.0, 2000.0], [0.0, 3500.0, 1500.0], [0.0, 0.0, 1.0]]
D = [-0.1, 0.05, 0.0, 0.0, 0.0]
SHA = "b8d377ba22d155e7056a5e9ae747fdd0970c7c73dee981bbee17d95c8156cf78"


class FakeCatalog:
    """`SlatePresenceCatalog`, in memory."""

    def __init__(self, *, candidates=None, inputs=None, refuse=None):
        self.candidates = candidates or {}
        self.inputs = inputs
        self.refuse = refuse
        self.versions = []
        self.persisted = []

    async def member_tenants(self):
        return [LAB, REEF]

    async def next_dive_for_slate_detection(self, tenant_id, *, model_version):
        self.versions.append(model_version)
        return self.candidates.get(tenant_id)

    async def slate_detection_inputs(self, tenant_id, dive_id, *, model_version):
        self.versions.append(model_version)
        if isinstance(self.refuse, SlateDetectionUnavailable):
            raise self.refuse
        return self.inputs

    async def persist_slate_presence(self, tenant_id, dive_id, rows):
        if isinstance(self.refuse, ForeignCapture):
            raise self.refuse
        self.persisted.append((tenant_id, dive_id, list(rows)))
        return len(rows)


def _activities(catalog):
    return SlateDetectionActivities(catalog=catalog, layout=LAYOUT)


async def _run(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


async def test_selects_the_oldest_dive_across_tenants():
    catalog = FakeCatalog(
        candidates={
            LAB: SlateDetectionCandidate(uuid.uuid4(), T0 + timedelta(hours=1)),
            REEF: SlateDetectionCandidate(DIVE, T0),
        }
    )

    target = await _run(_activities(catalog).select_next_dive_for_slate_detection)

    assert target == StagingTarget(REEF, DIVE)
    assert catalog.versions == [SLATE_DETECTOR_VERSION] * 2


async def test_no_candidate_is_none():
    assert (
        await _run(_activities(FakeCatalog()).select_next_dive_for_slate_detection)
        is None
    )


async def test_resolves_each_frames_staged_raw_and_the_dives_intrinsics():
    captures = [SlateDetectionCapture(uuid.uuid4(), f"{n:032x}") for n in (1, 2)]
    catalog = FakeCatalog(inputs=SlateDetectionInputs(DIVE, K, D, captures))

    payload = await _run(_activities(catalog).resolve_slate_detection_inputs, TARGET)

    assert (payload.tenant_id, payload.dive_id) == (LAB, DIVE)
    assert (payload.camera_matrix, payload.distortion_coefficients) == (K, D)
    assert payload.images == [
        DetectSlateImage(capture_id=c.capture_id, raw=LAYOUT.raw(LAB, c.checksum))
        for c in captures
    ]
    assert catalog.versions == [SLATE_DETECTOR_VERSION]


async def test_a_dive_that_cannot_be_resolved_is_final():
    catalog = FakeCatalog(refuse=SlateDetectionUnavailable("no pinhole camera"))

    with pytest.raises(ApplicationError) as raised:
        await _run(_activities(catalog).resolve_slate_detection_inputs, TARGET)

    assert raised.value.type == "SlateDetectionUnavailable"
    assert raised.value.non_retryable


def _result(status="predicted", probability=0.91, version=SLATE_DETECTOR_VERSION):
    return SlatePresenceResult(
        capture_id=uuid.uuid4(), status=status, probability=probability,
        model_version=version, weights_sha256=SHA,
    )  # fmt: skip


async def test_persists_each_result_as_the_processor_stamped_it():
    catalog = FakeCatalog()
    results = [_result(), _result("decode_failed", None), _result(version=0)]

    written = await _run(
        _activities(catalog).persist_slate_presence_predictions, TARGET, results
    )

    assert written == 3
    ((tenant, dive, rows),) = catalog.persisted
    assert (tenant, dive) == (LAB, DIVE)
    assert rows == [
        SlatePresenceRow(r.capture_id, r.status, r.probability, r.model_version, SHA)
        for r in results
    ]


async def test_nothing_to_persist_touches_nothing():
    catalog = FakeCatalog()
    assert (
        await _run(_activities(catalog).persist_slate_presence_predictions, TARGET, [])
        == 0
    )
    assert catalog.persisted == []


async def test_a_store_refusal_is_final():
    catalog = FakeCatalog(refuse=ForeignCapture("not captures of dive"))

    with pytest.raises(ApplicationError) as raised:
        await _run(
            _activities(catalog).persist_slate_presence_predictions,
            TARGET,
            [_result()],
        )

    assert raised.value.type == "InvalidPredictions"
    assert raised.value.non_retryable
