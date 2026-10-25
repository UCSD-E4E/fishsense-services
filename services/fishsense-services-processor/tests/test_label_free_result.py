"""The label-free fit's shared result (`automatic_calibration.label_free`)
and the bracket gate services' known-size fit borrows from core (review of
#354).

`refused` and `accepted` fill every field the caller leaves out from core's
own `LabelFreeFit`, so a field core adds cannot make a services fit raise:
4.3.2 added `implausible_pairs`, and the hand-written copies this replaced
raised a TypeError on it. `fit_known_sizes` (calibration v3) gates on core's
private `size_constancy._pinned`; its semantics are pinned here, so a core
release that changes or drops it fails this suite rather than moving v3's
refusals silently.
"""

from __future__ import annotations

from dataclasses import fields

import numpy as np
import pytest
from fishsense_core import size_constancy as sc

from fishsense_services_processor.automatic_calibration import label_free, v3

K = [[2850.0, 0.0, 2000.0], [0.0, 2850.0, 1500.0], [0.0, 0.0, 1.0]]


def test_a_services_refusal_is_cores_refusal_field_for_field():
    seen = {"used": [1, 2], "frames_used": 2, "pairs": 5}
    ours = label_free.refused("too_few_frames", **seen)
    core = sc._refused(  # pylint: disable=protected-access
        "too_few_frames", sc.O_DESIGN_M, **seen
    )
    for f in fields(sc.LabelFreeFit):
        assert getattr(ours, f.name) == getattr(core, f.name), f.name
    assert ours.scale_sd is None


def test_an_accepted_fit_fills_the_fields_its_caller_does_not_know():
    fit = label_free.accepted(
        laser_position=[0.1, 0.0, 0.0], laser_axis=[0.0, 0.0, 1.0], o_mag_m=0.1,
        used=[0, 1, 2], frames_used=3,
    )  # fmt: skip
    assert fit.outcome == "accepted" and fit.refusal_reason is None
    assert fit.implausible_pairs == 0 and fit.pairs == 0
    assert fit.vanishing_px is None and fit.scale_sd is None


def test_a_field_with_no_empty_value_fails_loudly():
    """A required field core adds that is neither Optional, a list nor an
    int has no honest empty value: refuse to make one up."""
    with pytest.raises(TypeError):
        label_free._empty(float)  # pylint: disable=protected-access


def test_cores_bracket_gate_is_the_one_known_sizes_relies_on():
    """`_pinned(t, tv)`: the optimum sits within `_AT_BOUND_PX` of either
    end of the search bracket, which runs `_BRACKET_PX` back from
    `_BRACKET_GAP_PX` short of the nearest frame."""
    t = np.array([0.0, 100.0])
    near = float(t.min()) - 5.0
    far = near - 4000.0
    assert (sc._BRACKET_GAP_PX, sc._BRACKET_PX, sc._AT_BOUND_PX) == (5.0, 4000.0, 1.0)
    assert sc._pinned(t, near) and sc._pinned(
        t, far
    )  # pylint: disable=protected-access
    assert not sc._pinned(t, near - 2.0)  # pylint: disable=protected-access
    assert not sc._pinned(t, far + 2.0)  # pylint: disable=protected-access


def test_known_sizes_that_never_shrink_are_unconstrained():
    """No size change along the line, so no vanishing position: the optimum
    is the bracket's edge, and v3 refuses it rather than fitting it."""
    n = 12
    dots = np.column_stack([1000 + 40.0 * np.arange(n), 800 + 10.0 * np.arange(n)])
    fit = v3.fit_known_sizes(dots, [1.0] * n, K, min_size_ratio=1.0)
    assert fit.refusal_reason == "unconstrained"
