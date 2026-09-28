"""Workflow contract test for PreprocessSpeciesImagesWorkflow.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/tests/test_preprocess_species_images_workflow.py. Test names,
bodies and reasons are v1's; v2 adaptations: the payload is the v2 contract
(capture ids, and the object refs the orchestrator issued), so a member's
raw frame and JPEG come from it rather than from a checksum and a fixed output
folder.

v2 change: there is no positional fallback. v1 numbered `clusters` 1..n when
an older api-worker sent no `cluster_members`; v2's payload always carries
each frame's position, and v1's partial-redraw test (last) is what that
position is for.
"""

import uuid
from datetime import timedelta
from typing import List, Optional, Tuple

from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.species import (
    PreprocessSpeciesImagesInput,
    SpeciesClusterMember,
)
from fishsense_services_processor.species.workflow import (
    PreprocessSpeciesImageInput,
    PreprocessSpeciesImagesWorkflow,
)

# A small but realistic intrinsics shape (3x3 matrix, 5-element distortion
# vector). Values are arbitrary — the stub activity doesn't use them.
_K = [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0]]
_D = [-0.1, 0.05, 0.0, 0.0, 0.0]
TENANT = uuid.uuid4()


def _member(name: str, index: int, size: int) -> SpeciesClusterMember:
    checksum = name * (32 // len(name))
    return SpeciesClusterMember(
        capture_id=uuid.uuid5(uuid.NAMESPACE_OID, name),
        raw=ObjectRef(bucket="scratch", key=f"tenants/{TENANT}/raw/{checksum}.ORF"),
        jpeg=ObjectRef(
            bucket="labels",
            key=f"tenants/{TENANT}/preprocess_groups_jpeg/{checksum}.JPG",
        ),
        cluster_index=index,
        cluster_size=size,
    )


def _positional(clusters):
    """v1's `clusters` shape, numbered as a full pass would number it."""
    return [
        [_member(name, i + 1, len(cluster)) for i, name in enumerate(cluster)]
        for cluster in clusters
    ]


async def _run(payload, stub, queue):
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[PreprocessSpeciesImagesWorkflow],
            activities=[stub],
        ):
            await env.client.execute_workflow(
                PreprocessSpeciesImagesWorkflow.run,
                payload,
                id=f"{queue}-{uuid.uuid4()}",
                task_queue=queue,
            )


def _recording(calls):
    @activity.defn(name="preprocess_species_image")
    async def stub_preprocess_species_image(
        payload: PreprocessSpeciesImageInput,
    ) -> None:
        calls.append(payload)

    return stub_preprocess_species_image


async def test_workflow_fans_out_one_activity_per_image_with_correct_indices():
    calls: List[PreprocessSpeciesImageInput] = []

    await _run(
        PreprocessSpeciesImagesInput(
            dive_id=uuid.uuid4(),
            cluster_members=_positional([["c1", "c2", "c3"], ["c4", "c5"]]),
            camera_matrix=_K,
            distortion_coefficients=_D,
        ),
        _recording(calls),
        "test-stage2",
    )

    assert len(calls) == 5
    by_name = {c.member.raw.key.rsplit("/", 1)[-1][:2]: c.member for c in calls}

    # cluster 1: 3 images, 1-based indices 1..3, size 3
    assert (by_name["c1"].cluster_index, by_name["c1"].cluster_size) == (1, 3)
    assert (by_name["c2"].cluster_index, by_name["c2"].cluster_size) == (2, 3)
    assert (by_name["c3"].cluster_index, by_name["c3"].cluster_size) == (3, 3)
    # cluster 2: 2 images, 1-based indices 1..2, size 2
    assert (by_name["c4"].cluster_index, by_name["c4"].cluster_size) == (1, 2)
    assert (by_name["c5"].cluster_index, by_name["c5"].cluster_size) == (2, 2)

    # The JPEG lands where the orchestrator said, in the species folder.
    assert {c.member.jpeg.key.split("/")[-2] for c in calls} == {
        "preprocess_groups_jpeg"
    }

    # Camera intrinsics are propagated unchanged to every image.
    for c in calls:
        assert c.camera_matrix == _K
        assert c.distortion_coefficients == _D


async def test_workflow_uses_start_to_close_not_schedule_to_close():
    """See PreprocessHeadtailImagesWorkflow's matching test — same fan-out
    shape, same prod failure mode."""
    timeouts: List[Tuple[Optional[timedelta], Optional[timedelta]]] = []

    @activity.defn(name="preprocess_species_image")
    async def stub_preprocess_species_image(
        payload: PreprocessSpeciesImageInput,
    ) -> None:  # pylint: disable=unused-argument
        info = activity.info()
        timeouts.append((info.start_to_close_timeout, info.schedule_to_close_timeout))

    await _run(
        PreprocessSpeciesImagesInput(
            dive_id=uuid.uuid4(),
            cluster_members=_positional([["a"]]),
            camera_matrix=_K,
            distortion_coefficients=_D,
        ),
        stub_preprocess_species_image,
        "test-stage2-timeouts",
    )

    assert len(timeouts) == 1
    start_to_close, schedule_to_close = timeouts[0]
    assert start_to_close == timedelta(minutes=5)
    if schedule_to_close is not None and schedule_to_close > timedelta(0):
        assert schedule_to_close > start_to_close


async def test_workflow_with_no_clusters_makes_no_activity_calls():
    calls: List[PreprocessSpeciesImageInput] = []

    await _run(
        PreprocessSpeciesImagesInput(
            dive_id=uuid.uuid4(),
            cluster_members=[],
            camera_matrix=_K,
            distortion_coefficients=_D,
        ),
        _recording(calls),
        "test-stage2-empty",
    )

    assert not calls


async def test_partial_redraw_keeps_each_frame_position_in_the_whole_cluster():
    """The reprocess case: 2 frames of a 7-image cluster are redrawn.

    They must come out "3 of 7" and "6 of 7" -- the positions their four
    untouched siblings' JPEGs already encode -- not "1 of 2" and "2 of 2".
    Numbering the emitted batch instead overwrites the same object-store keys
    with a different, wrong answer, and nothing errors.
    """
    calls: List[PreprocessSpeciesImageInput] = []

    await _run(
        PreprocessSpeciesImagesInput(
            dive_id=uuid.uuid4(),
            cluster_members=[[_member("c3", 3, 7), _member("c6", 6, 7)]],
            camera_matrix=_K,
            distortion_coefficients=_D,
        ),
        _recording(calls),
        "test-stage2-partial",
    )

    by_name = {c.member.raw.key.rsplit("/", 1)[-1][:2]: c.member for c in calls}
    assert set(by_name) == {"c3", "c6"}, "only the flagged frames are redrawn"
    assert (by_name["c3"].cluster_index, by_name["c3"].cluster_size) == (3, 7)
    assert (by_name["c6"].cluster_index, by_name["c6"].cluster_size) == (6, 7)


async def test_clusters_are_drawn_one_at_a_time():
    """v1 gathered a cluster's frames together and the clusters in turn, so a
    dive never has more than one cluster's decodes queued at once."""
    order: List[str] = []

    @activity.defn(name="preprocess_species_image")
    async def stub(payload: PreprocessSpeciesImageInput) -> None:
        order.append(payload.member.raw.key.rsplit("/", 1)[-1][:2])

    await _run(
        PreprocessSpeciesImagesInput(
            dive_id=uuid.uuid4(),
            cluster_members=_positional([["a1", "a2"], ["b1"], ["c1", "c2"]]),
            camera_matrix=_K,
            distortion_coefficients=_D,
        ),
        stub,
        "test-stage2-order",
    )

    assert [name[0] for name in order] == ["a", "a", "b", "c", "c"]
