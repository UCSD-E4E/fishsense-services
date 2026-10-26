"""The worker refusals' names (`refusals`): the processor raises them and the
orchestrator reads them, and failures already recorded keep their names."""

from fishsense_services_contracts.refusals import (
    AUTOMATIC_FRAME_REFUSALS,
    SAM3_REFUSALS,
    WORKER_REFUSALS,
    weights_failure_types,
)


def test_the_names_already_recorded_are_kept():
    """Sam3* since #358 (and before), SlateDetector* and Bioclip* since
    their stages: renaming one would make old failures read differently."""
    assert set(SAM3_REFUSALS) == {
        "NoGpuForSam3",
        "Sam3SettingsInvalid",
        "Sam3WeightsCorrupt",
        "Sam3WeightsUnavailable",
        "Sam3NotInManifest",
        # New with the review of #364: kept names unchanged, one added.
        "Sam3LoadInvalid",
    }
    assert {
        "SlateDetectorSettingsInvalid",
        "SlateDetectorWeightsCorrupt",
        "SlateDetectorWeightsUnavailable",
        "SlateDetectorNotInManifest",
        "SlateDetectorCheckpointInvalid",
        "BioclipSettingsInvalid",
        "BioclipWeightsCorrupt",
        "BioclipWeightsUnavailable",
        "BioclipNotInManifest",
        "NoGpuForKeypoints",
    } <= WORKER_REFUSALS


def test_the_automatic_frames_refuse_for_either_model():
    assert set(AUTOMATIC_FRAME_REFUSALS) == {
        *SAM3_REFUSALS,
        *weights_failure_types("LaserDetector"),
    }
