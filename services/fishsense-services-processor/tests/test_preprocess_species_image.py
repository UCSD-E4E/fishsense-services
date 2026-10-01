"""The stage-2 per-image activity: read the staged raw frame, rectify, draw
"i/N", write the JPEG where the orchestrator said.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/activities/
preprocess_species_image.py (v1 had no unit test of the activity itself; its
two integration tests need a real `.ORF` fixture, which neither repo has).

v2 changes, pinned here:

* the frame and the JPEG are the `ObjectRef`s in the payload; the processor
  builds no key;
* rectification uses fishsense-core's own `CameraIntrinsics` (v1 built the API
  SDK's, which v2 does not have), through the same `RectifiedImage(RawImage)`
  chain and core's default decode, as v1 on 4.1.0;
* the raw frame is streamed to a scratch file, not held in memory, and the
  file is gone afterwards.
"""

import uuid
from pathlib import Path

import numpy as np
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.species import SpeciesClusterMember
from fishsense_services_processor.species import activities as sut
from fishsense_services_processor.species.workflow import PreprocessSpeciesImageInput

TENANT = uuid.uuid4()
K = [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0]]
D = [-0.1, 0.05, 0.0, 0.0, 0.0]
RAW = ObjectRef(bucket="scratch", key=f"tenants/{TENANT}/raw/{'a' * 32}.ORF")
JPEG = ObjectRef(
    bucket="labels", key=f"fishsense-lite/preprocess_groups_jpeg/{'a' * 32}.JPG"
)


class FakeStore:
    def __init__(self):
        self.downloads = []
        self.uploads = []
        self.seen_files = []

    async def download_raw(self, ref, directory):
        path = Path(directory) / ref.key.rsplit("/", 1)[-1]
        path.write_bytes(b"raw bytes")
        self.downloads.append(ref)
        self.seen_files.append(path)
        return path

    async def upload_processed_jpeg(self, ref, data):
        self.uploads.append((ref, data))


def _payload(index=2, size=7):
    return PreprocessSpeciesImageInput(
        member=SpeciesClusterMember(
            capture_id=uuid.uuid4(), raw=RAW, jpeg=JPEG,
            cluster_index=index, cluster_size=size,
        ),
        camera_matrix=K,
        distortion_coefficients=D,
    )  # fmt: skip


async def test_the_frame_is_read_drawn_and_written_where_the_orchestrator_said(
    monkeypatch,
):
    drawn = []

    def fake_render(path, camera_matrix, distortion, index, size):
        drawn.append((Path(path).read_bytes(), camera_matrix, distortion, index, size))
        return b"jpeg bytes"

    monkeypatch.setattr(sut, "_rectify_overlay_encode", fake_render)
    store = FakeStore()
    activities = sut.SpeciesImageActivities(store_factory=lambda: store)

    await ActivityEnvironment().run(activities.preprocess_species_image, _payload())

    assert store.downloads == [RAW]
    assert drawn == [(b"raw bytes", K, D, 2, 7)]
    # A migrated frame's redraw overwrites v1's JPEG in place: its URL is the
    # one Label Studio tasks hold.
    assert store.uploads == [(JPEG, b"jpeg bytes")]
    assert not any(path.exists() for path in store.seen_files), "scratch left"


async def test_the_store_is_built_once(monkeypatch):
    monkeypatch.setattr(sut, "_rectify_overlay_encode", lambda *a: b"j")
    built = []

    def factory():
        built.append(1)
        return FakeStore()

    activities = sut.SpeciesImageActivities(store_factory=factory)
    for _ in range(3):
        await ActivityEnvironment().run(activities.preprocess_species_image, _payload())

    assert built == [1]


def test_rectification_is_cores_chain_with_cores_intrinsics(monkeypatch):
    seen = {}

    class FakeRaw:
        def __init__(self, source):
            seen["source"] = source

    class FakeRectified:
        def __init__(self, image, intrinsics):
            seen["image"] = image
            seen["intrinsics"] = intrinsics
            self.data = np.full((1080, 1920, 3), 128, dtype=np.uint8)

    monkeypatch.setattr(sut, "RawImage", FakeRaw)
    monkeypatch.setattr(sut, "RectifiedImage", FakeRectified)

    out = sut._rectify_overlay_encode(  # pylint: disable=protected-access
        Path("/scratch/a.ORF"), K, D, 1, 1
    )

    assert seen["source"] == Path("/scratch/a.ORF")
    assert isinstance(seen["image"], FakeRaw)
    assert isinstance(seen["intrinsics"], sut.CameraIntrinsics)
    assert np.array_equal(seen["intrinsics"].camera_matrix, np.array(K))
    assert np.array_equal(seen["intrinsics"].distortion_coefficients, np.array(D))
    assert out[:2] == b"\xff\xd8"
