"""Stage 2 (species preprocessing): the orchestrator's input to the processor.

Ported in shape from fishsense-lite@77e8f8e5 libs/fishsense-shared/src/
fishsense_shared/preprocess_contracts.py (`PreprocessSpeciesImagesInput`,
`SpeciesClusterMember`). The processor rectifies each staged raw frame with the
dive's camera intrinsics, draws "i/N" -- the frame's position in its WHOLE
prediction cluster -- and writes the JPEG a labeler sees.

v2 changes:

* **a member's position travels with it, and only there.** v1 kept
  `clusters` (checksums) beside an optional `cluster_members` so an older
  data-worker could still run a payload mid-deploy, numbering `clusters`
  positionally -- the partial-redraw bug, where 3 frames of a 7-image
  cluster are drawn 1/3..3/3 over keys their siblings' 4/7..7/7 share. v2
  names its own queues, so there is no older reader to serve;
* **the orchestrator issues the keys** (PLAN.md §9.11): each member names
  where its raw frame was staged and where its JPEG goes -- over v1's JPEG
  for a migrated frame, whose URL Label Studio tasks already hold.

Integration adds these to ``MODELS`` and publishes the schema.
"""

from uuid import UUID
from typing import Annotated

from pydantic import BaseModel, Field, model_validator, ConfigDict

from fishsense_services_contracts.object_store import ObjectRef
from fishsense_services_contracts.renamed import renamed_from

__all__ = ["PreprocessSpeciesImagesInput", "SpeciesClusterMember"]


class SpeciesClusterMember(BaseModel):
    """One frame to draw, and where it sits in its prediction cluster."""

    capture_id: UUID
    #: The staged raw frame (scratch).
    raw: ObjectRef
    #: Where the processed JPEG is written.
    jpeg: ObjectRef
    #: 1-based position in the WHOLE cluster, not in the frames being drawn.
    cluster_index: int = Field(ge=1)
    #: The whole cluster's size; a frame in no cluster is 1 of 1.
    cluster_size: int = Field(ge=1)

    @model_validator(mode="after")
    def _within_its_cluster(self) -> "SpeciesClusterMember":
        if self.cluster_index > self.cluster_size:
            raise ValueError(
                f"image {self.cluster_index} of {self.cluster_size} is not in "
                "its cluster"
            )
        return self


class PreprocessSpeciesImagesInput(BaseModel):
    model_config = ConfigDict(validate_by_name=True)
    mission_id: Annotated[UUID, renamed_from("dive_id", "mission_id")]
    #: 3x3, from the dive's device's current camera calibration.
    camera_matrix: list[list[float]] = Field(min_length=3, max_length=3)
    distortion_coefficients: list[float]
    #: One list per cluster, drawn a cluster at a time (v1's order).
    cluster_members: list[list[SpeciesClusterMember]]

    @model_validator(mode="after")
    def _square(self) -> "PreprocessSpeciesImagesInput":
        if any(len(row) != 3 for row in self.camera_matrix):
            raise ValueError("camera_matrix must be 3x3")
        return self
