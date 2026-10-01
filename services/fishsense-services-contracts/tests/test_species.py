"""Stage 2 (species preprocessing): what the orchestrator hands the processor.

Ported in shape from fishsense-lite@77e8f8e5 libs/fishsense-shared/src/
fishsense_shared/preprocess_contracts.py (`PreprocessSpeciesImagesInput`,
`SpeciesClusterMember`). v2 changes, pinned here:

* **each member carries its own position in the whole prediction cluster**,
  and nothing else says it. v1 kept `clusters` (checksums) beside the optional
  `cluster_members` only so an older data-worker could still run a payload
  during a rolling deploy; its positional fallback is the partial-redraw bug
  ("3 of a 7-image cluster drawn as 1/3..3/3"). v2 names its own queues, so
  the fallback has nothing to serve;
* **the orchestrator issues the keys**: each member names where its raw frame
  was staged and where its JPEG goes (PLAN.md §9.11), so the processor never
  builds one.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.species import (
    PreprocessSpeciesImagesInput,
    SpeciesClusterMember,
)

K = [[1000.0, 0.0, 960.0], [0.0, 1000.0, 540.0], [0.0, 0.0, 1.0]]
D = [-0.1, 0.05, 0.0, 0.0, 0.0]


def _member(index=3, size=7):
    tenant = uuid4()
    return SpeciesClusterMember(
        capture_id=uuid4(),
        raw=ObjectRef(bucket="scratch", key=f"tenants/{tenant}/raw/{'a' * 32}.ORF"),
        jpeg=ObjectRef(
            bucket="labels",
            key=f"tenants/{tenant}/preprocess_groups_jpeg/{'a' * 32}.JPG",
        ),
        cluster_index=index,
        cluster_size=size,
    )


def test_the_input_round_trips():
    payload = PreprocessSpeciesImagesInput(
        dive_id=uuid4(),
        camera_matrix=K,
        distortion_coefficients=D,
        cluster_members=[[_member(3, 7), _member(6, 7)], [_member(1, 1)]],
    )

    assert (
        PreprocessSpeciesImagesInput.model_validate_json(payload.model_dump_json())
        == payload
    )


def test_a_member_knows_its_place_in_the_whole_cluster():
    member = _member(6, 7)
    assert (member.cluster_index, member.cluster_size) == (6, 7)


@pytest.mark.parametrize(("index", "size"), [(0, 3), (4, 3), (1, 0)])
def test_a_position_outside_its_cluster_is_refused(index, size):
    """The overlay's "i of N" is 1-based and i <= N: anything else is drawn
    on a labeler's frame as a lie."""
    with pytest.raises(ValidationError):
        _member(index, size)


def test_the_intrinsics_must_be_a_camera_matrix():
    with pytest.raises(ValidationError):
        PreprocessSpeciesImagesInput(
            dive_id=uuid4(),
            camera_matrix=[[1.0, 0.0], [0.0, 1.0]],
            distortion_coefficients=D,
            cluster_members=[],
        )


def test_there_is_no_positional_fallback():
    """v1's `clusters` field existed only for its rolling deploys."""
    assert "clusters" not in PreprocessSpeciesImagesInput.model_fields
