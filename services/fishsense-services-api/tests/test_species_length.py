"""A length out of distribution for its species (the owner, 2026-10-30): over
the species' published maximum, or outside its typical range in our own
lengths (`species_length`)."""

from __future__ import annotations

import math

import pytest

from fishsense_services_api.species_length import (
    OUTLIER_Z,
    SpeciesReference,
    species_length_flag,
)

#: Hogfish: FishBase max 91 cm TL; typical log length -1.48 +- 0.147 (MAD).
HOGFISH = SpeciesReference(max_length_m=0.91, log_median=-1.4823, log_mad=0.1473)


def _edge(z: float) -> float:
    return math.exp(HOGFISH.log_median + z * HOGFISH.log_mad / 0.6745)


def test_the_threshold_is_three_and_a_half_robust_sds():
    assert OUTLIER_Z == 3.5


@pytest.mark.parametrize(
    ("length_m", "flag"),
    [
        (0.23, None),  # the typical hogfish
        (_edge(3.4), None),
        (_edge(3.6), "long"),  # 0.50 m: long for a hogfish, not impossible
        (_edge(-3.6), "short"),
        (_edge(-3.4), None),
        (0.95, "over_max"),  # over the published maximum: impossible
    ],
)
def test_a_length_is_judged_against_its_species(length_m, flag):
    assert species_length_flag(length_m, HOGFISH) == flag


def test_over_the_maximum_wins_over_long():
    assert _edge(3.6) < 0.91 < 1.2
    assert species_length_flag(1.2, HOGFISH) == "over_max"


def test_no_reference_no_length_no_flag():
    assert species_length_flag(5.0, None) is None
    assert species_length_flag(None, HOGFISH) is None


def test_without_a_typical_range_only_the_maximum_judges():
    only_max = SpeciesReference(max_length_m=0.91, log_median=None, log_mad=None)
    assert species_length_flag(0.02, only_max) is None
    assert species_length_flag(0.95, only_max) == "over_max"
