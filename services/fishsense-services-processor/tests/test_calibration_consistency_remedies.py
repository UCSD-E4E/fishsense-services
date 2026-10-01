"""What a refusal message tells an operator to do.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-workflow-worker/tests/test_calibration_consistency_remedies.py.

The message is the only thing an operator sees when a calibration is refused —
it is written into `Dive.calibration_refused_reason` and read back from prod —
so a claim in it that the code contradicts sends someone to do unnecessary
work, or to distrust the pipeline.

That happened. The original text said the dive "stays in the calibration cohort
and will be re-selected hourly, blocking higher-id dives", which was true when
it was written and was made false by `_calibration_refusal_still_stands`:
`perform_laser_calibration_activity` now records the refusal via
`set_calibration_refused` and both calibration cohorts exclude a refused dive
until one of its labels is newer than the refusal. Five prod dives carry the
stale sentence, and it was read back as a live diagnosis.

v2 change, pinned in the last test: the remedies name v2's levers. v1's said
`Priority.NONE` and `DELETE /api/v1/dives/{id}/calibration-target/`; v2's
priority is the text `'none'`, the refusal is a `refused` row in
`laser_calibrations` rather than dive columns, and an operator can also clear
it (`laser_calibration_store.clear_calibration_refusal`).
"""

from fishsense_services_processor.calibration.consistency import (
    REFUSAL_REMEDIES,
)


def test_the_message_does_not_claim_an_hourly_re_selection():
    """The claim the recording mechanism falsified."""
    lowered = REFUSAL_REMEDIES.lower()
    assert "re-selected" not in lowered
    assert "hourly" not in lowered
    assert "blocking" not in lowered


def test_the_message_says_the_refusal_is_recorded_and_self_clearing():
    """An operator has to know two things the old text omitted: nothing is
    churning, and relabelling is enough — there is no flag to clear by hand."""
    lowered = REFUSAL_REMEDIES.lower()
    assert "recorded" in lowered
    assert "relabel" in lowered or "label" in lowered


def test_the_message_still_names_the_two_manual_remedies():
    """Parking is for a dive that can never be fixed; clearing the calibration
    target is the checkerboard-only escape. Both are still the operator's
    only levers, so dropping them would leave the message advice-free.

    v2: spelled as v2 stores them -- priority `'none'`, and the dive's
    `calibration_target_id` -- not v1's enum and route, which don't exist here."""
    assert "priority 'none'" in REFUSAL_REMEDIES
    assert "calibration_target_id" in REFUSAL_REMEDIES
    assert "Priority.NONE" not in REFUSAL_REMEDIES
    assert "/api/v1/" not in REFUSAL_REMEDIES
