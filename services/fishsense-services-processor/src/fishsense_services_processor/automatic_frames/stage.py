"""The automatic-results GPU stage: production's seams wired in.

On the GPU role, beside laser predict and head/tail predict, whose models it
reuses in the same process (the detector and SAM 3.1 load once, under their
own locks). Nothing loads at import: the registry imports every stage
wherever it runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fishsense_services_contracts.laser import LASER_PREDICTOR_VERSION
from fishsense_services_contracts.object_store import ObjectStoreConnection
from fishsense_services_processor.automatic_frames.activities import (
    AutomaticFramesActivities,
)
from fishsense_services_processor.automatic_frames.workflow import (
    PredictAutomaticFramesWorkflow,
)
from fishsense_services_processor.automatic_results.frames import (
    LaserDot,
    ScoredSam3Adapter,
)
from fishsense_services_processor.headtail_predict import stage as headtail_stage
from fishsense_services_processor.headtail_predict.activities import (
    HeadtailPredictActivities,
)
from fishsense_services_processor.headtail_predict.predict import (
    cuda_available,
    get_segmenter,
)
from fishsense_services_processor.headtail_preprocess.activities import (
    rectify_and_encode_jpeg,
)
from fishsense_services_processor.object_store import ProcessorObjectStore
from fishsense_services_processor.registry import ROLE_GPU, Stage


def _store() -> ProcessorObjectStore:
    return ProcessorObjectStore.from_settings(ObjectStoreConnection())


def _predict_dot(raw_path: Path, camera, distortion) -> Optional[LaserDot]:
    """The laser stage's kernel (wavelength unknown, as production's)."""
    # pylint: disable-next=import-outside-toplevel,protected-access
    from fishsense_services_processor.laser_predict.activities import _predict_from_raw

    prediction, *_, checkpoint = _predict_from_raw(raw_path, camera, distortion, None)
    if prediction.x is None or prediction.y is None:
        return None
    return LaserDot(
        float(prediction.x),
        float(prediction.y),
        float(prediction.confidence),
        LASER_PREDICTOR_VERSION,
        checkpoint,
    )


# The head/tail stage's verified SAM 3.1 fetch, with its non-retryable
# failure types (one per cause), reused as it is.
# pylint: disable-next=protected-access
_SAM3 = HeadtailPredictActivities(
    store_factory=_store, sam3_checkpoint=headtail_stage._sam3_checkpoint
)._verified_sam3_checkpoint

_ACTIVITIES = AutomaticFramesActivities(
    store_factory=_store,
    sam3_checkpoint=_SAM3,
    cuda_available=cuda_available,
    predict_dot=_predict_dot,
    render_jpeg=rectify_and_encode_jpeg,
    segmenter=lambda path: ScoredSam3Adapter(get_segmenter(path)),
)

STAGE = Stage(
    name="automatic_frames",
    role=ROLE_GPU,
    workflows=[PredictAutomaticFramesWorkflow],
    activities=[_ACTIVITIES.predict_automatic_frame],
)
