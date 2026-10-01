"""Classifying Synology FileStation errors as permanent or transient.

v1 (fishsense-lite@a8b2c3bc, activities/nas_errors.py) has no dedicated tests;
these pin its documented contract before the port. The rule it encodes:
**retry belongs to the Temporal retry policy, never to an inner loop** -- an
inner loop under Temporal's retry once caused a 200x-per-file download storm
that tripped the NAS auto-block (krg-infra#501). So the helpers only classify.
"""

import pytest
from temporalio.exceptions import ApplicationError

from fishsense_services_orchestrator.ingest.nas_errors import (
    NAS_FILE_NOT_FOUND_TYPE,
    dsm_error_code,
    raise_if_permanent_dsm_error,
)


def test_the_code_is_recovered_from_the_dsm_message():
    assert dsm_error_code(RuntimeError("Synology API error 408")) == 408


def test_a_message_without_a_code_yields_none():
    assert dsm_error_code(RuntimeError("connection reset")) is None


def test_a_missing_file_is_permanent_and_names_what_was_missing():
    with pytest.raises(ApplicationError) as raised:
        raise_if_permanent_dsm_error(
            RuntimeError("Synology API error 408"), context="dives/d10/P1.ORF"
        )

    assert raised.value.non_retryable is True
    assert raised.value.type == NAS_FILE_NOT_FOUND_TYPE
    assert "dives/d10/P1.ORF" in str(raised.value)


@pytest.mark.parametrize("code", [502, 407, 402])
def test_routine_transient_codes_are_left_to_temporals_backoff(code):
    """502 (download backend falling over), 407 (fail-closed), 402 (busy) are
    self-healing under backoff; treating them as permanent would kill dives."""
    raise_if_permanent_dsm_error(
        RuntimeError(f"Synology API error {code}"), context="x"
    )  # returns: the caller re-raises and Temporal retries


def test_an_unrecognised_error_errs_toward_retrying():
    raise_if_permanent_dsm_error(RuntimeError("something odd"), context="x")


def test_the_error_type_matches_what_retry_policies_name():
    """Temporal matches non-retryable types by string; a rename here would
    silently restore retrying doomed work."""
    assert NAS_FILE_NOT_FOUND_TYPE == "NasFileNotFound"
