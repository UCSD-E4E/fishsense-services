"""The shadow calibration's fit: a port of cscw's size_constancy_sam.py.

The shadow stage (docs/shadow-size-constancy.md) ports
cscw-fishsense2027@8312780a `analysis/codesign/size_constancy_sam.py` rather
than reimplementing it, and its published pool result must reproduce after the
port (§9.2): per session, the along-line offset of the fitted vanishing point
from the stored calibration's (`err_deg`, at F = 2850 px), the 3-D angle
between their axes (`angle_deg`), and the bootstrap SE (`se_deg`).

The fixture (`fixtures/shadow_pool.json`, built by
analysis/shadow_calibration/build_pool_fixture.py from cscw's own loaders)
holds each session's inputs and the published rows, which have 5 decimals:
the port must match each to the last printed digit.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import numpy as np
import pytest

from fishsense_services_processor.shadow_calibration.fit import (
    F_PX,
    N_BOOT,
    VARIANT_INDEX,
    fit_tv,
    port_fit,
)

POOL = json.loads((Path(__file__).parent / "fixtures/shadow_pool.json").read_text())
SESSIONS = {s["session"]: s for s in POOL["sessions"]}
#: The published CSV prints 5 decimals.
PRINTED = 5.000001e-6


def _inputs(session, line):
    sized = [f for f in session["frames"] if f["size"] is not None]
    xy = np.array([[f["dot_x"], f["dot_y"]] for f in sized])
    s = np.array([f["size"] for f in sized])
    return xy, s, np.array(session[line]["u"]), session[line]["offset"]


def _fit(session, variant, line):
    xy, s, u, off = _inputs(session, line)
    return port_fit(xy, s, u, off, np.array(session["camera_matrix"]),
                    np.array(session["stored_laser_axis"]),
                    seed=(session["session"], VARIANT_INDEX[variant]))  # fmt: skip


def test_the_constants_are_the_scripts():
    assert (F_PX, N_BOOT) == (POOL["f_px"], POOL["n_boot"]) == (2850.0, 500)
    assert VARIANT_INDEX == {"sam3": 0, "corners": 1, "sam3_all": 2, "sam3_predline": 3}


@pytest.mark.parametrize("session", sorted(SESSIONS))
@pytest.mark.parametrize("variant, line", [("sam3_all", "line"),
                                           ("sam3_predline", "detector_line")])  # fmt: skip
def test_each_pool_session_reproduces_its_published_row(session, variant, line):
    s = SESSIONS[session]
    published = s["published"][variant]
    fit = _fit(s, variant, line)

    assert fit.frames == published["frames"]
    assert fit.size_spread == pytest.approx(published["size_spread"], abs=PRINTED)
    assert fit.err_deg_f2850 == pytest.approx(published["err_deg"], abs=PRINTED)
    assert fit.angle_deg == pytest.approx(published["angle_deg"], abs=PRINTED)
    assert fit.se_deg == pytest.approx(published["se_deg"], abs=PRINTED)


def test_the_pool_medians_are_the_papers():
    """Median |err_deg| 0.0232 (the spec's "0.023"), median angle 0.029,
    10/10 within 0.05; the detector line's median angle 0.047."""
    fits = [_fit(SESSIONS[k], "sam3_all", "line") for k in sorted(SESSIONS)]
    errs = [abs(f.err_deg_f2850) for f in fits]
    assert statistics.median(errs) == pytest.approx(0.02322, abs=1e-5)
    assert statistics.median(f.angle_deg for f in fits) == pytest.approx(
        0.029, abs=5e-4
    )
    assert sum(e <= 0.05 for e in errs) == 10
    pred = [
        _fit(SESSIONS[k], "sam3_predline", "detector_line") for k in sorted(SESSIONS)
    ]
    assert statistics.median(f.angle_deg for f in pred) == pytest.approx(
        0.047, abs=5e-4
    )


def test_fit_tv_is_the_scripts_bounded_search():
    """slate_unknown.fit_tv: Brent on [t_min - 4005, t_min - 5]. Size and the
    dot's distance from the vanishing point both go as 1/range, so on exact
    sizes ``s = c (t - tv)`` it recovers tv."""
    t = np.linspace(200.0, 900.0, 12)
    for tv in (-500.0, -2000.0, -3000.0):
        assert fit_tv(t, 0.07 * (t - tv)) == pytest.approx(tv, abs=0.05)


def test_the_offset_at_the_cameras_own_fx_is_kept_beside_f2850():
    """The paper reports the camera-fx offset (author, 2026-10-21)."""
    s = SESSIONS[62]
    fit = _fit(s, "sam3_all", "line")
    fx = s["camera_matrix"][0][0]
    assert fit.err_deg_fx == pytest.approx(np.degrees(fit.err_px / fx))
    assert fit.err_deg_f2850 == pytest.approx(np.degrees(fit.err_px / F_PX))
