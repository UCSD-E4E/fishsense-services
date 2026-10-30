"""A length out of distribution for its species (the owner, 2026-10-30).

Not a refusal and not a correction: a flag, so a reviewer looks twice. The
species is the one BioCLIP names (``species_length_references``, 0084, by
scientific name), and the length is judged two ways:

* ``over_max``: over the species' published maximum (FishBase, total
  length). Our head-to-tail length is about fork length, at most total
  length, so this is impossible: a wrong calibration, wrong points, or a
  wrong species.
* ``long`` / ``short``: over `OUTLIER_Z` robust SDs from the species' typical
  log length in our own lengths (the median and MAD, frozen in 0084 from
  23,024 automatic lengths on 2026-10-30).

What the analysis behind it found (2026-10-30, the lab, fish models left
out): 79 over the maximum, 117 long, 206 short, 1.4% in all; 54% of them in
ten missions (223, 228, 177, ...), so calibrations more than fish. Frames
with BioCLIP under 0.9 were flagged 2.2% of the time, over it 0.6%: a flag
can be the species, not the length. What it cannot see: no flagged frame had
a human length, so no control says a flag finds a wrong length; the typical
range was taken from lengths that hold the very errors it flags; and the
maximum is a record, so it catches only gross errors.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["OUTLIER_Z", "SpeciesReference", "species_length_flag"]

#: Robust SDs from the typical log length to flag (Iglewicz and Hoaglin's).
OUTLIER_Z = 3.5
#: MAD to SD for a normal distribution.
_MAD_TO_SD = 0.6745


@dataclass(frozen=True)
class SpeciesReference:
    """A species' published maximum, meters, and its typical log length
    (median, MAD) in our lengths -- None where it has none."""

    max_length_m: float
    log_median: float | None
    log_mad: float | None


def species_length_flag(
    length_m: float | None, reference: SpeciesReference | None
) -> str | None:
    """``over_max``, ``long``, ``short``, or None (nothing to say)."""
    if length_m is None or reference is None or length_m <= 0:
        return None
    if length_m > reference.max_length_m:
        return "over_max"
    if not reference.log_mad or reference.log_median is None:
        return None
    z = _MAD_TO_SD * (math.log(length_m) - reference.log_median) / reference.log_mad
    if z > OUTLIER_Z:
        return "long"
    if z < -OUTLIER_Z:
        return "short"
    return None
