"""The shadow calibration's fit: a port of cscw's SAM size-constancy script.

Ported from cscw-fishsense2027@8312780a:
- `calibration_analysis/e1j_size_constancy/slate_unknown.py`: `fit_tv`
  (:70-73), `F_PX` and `MIN_FRAMES` (:26);
- `analysis/codesign/size_constancy_sam.py`: the per-variant fit, its
  bootstrap (500 draws, `default_rng([session, variant])`) and its two error
  metrics (:96-118);
- `e2e_measurement/score.py` `calibrations()` (:59-75): the beam's origin from
  the assumed laser offset |O|, `O_DESIGN_M` (:29).

Kept from the script, exactly, so its pool result reproduces
(tests/test_shadow_fit.py, against its published CSV):
- the estimator: the vanishing position `t_v` along the dive's laser line that
  makes `s / (t - t_v)` most nearly constant, by Brent's bounded search on
  ``[t_min - 4005, t_min - 5]`` px;
- the fitted axis ``K^-1 [t_v u + off n, 1]``, normalized, with ``n = (-u1, u0)``;
- `err_deg`: the signed offset of the fitted vanishing point from the stored
  calibration's, along the line, at the fixed ``F_PX = 2850`` px; and
  `angle_deg`, the 3-D angle between the axes;
- the bootstrap SE, frames resampled with replacement, one generator per
  (session, variant) so a fit's SE doesn't depend on which others run.

The estimator is fishsense-core's (core #97): `fit_vanishing_position` and
`beam_from_vanishing` in `fishsense_core.size_constancy` are this module's
former `fit_tv` and `beam` line for line, and the bootstrap is its
`bootstrap_se` with this script's generator and draws,
``bootstrap_se(t, s, rng=np.random.default_rng([session, variant]),
draws=500)``. The pool rows reproduce unchanged (2026-10-23). What stays here
is the shadow stage's own: the per-variant seeding, and the evaluation
against a reference calibration (`err_deg`, `angle_deg`), which is not
calibration.

v2 additions, none of which changes a ported number:
- `err_deg_fx`: the same offset at the camera's own fx, which the paper
  reports (author, 2026-10-21);
- the origin, from the assumed |O| along the line direction in normalized
  coordinates (score.py). The script fits the direction only, so ranges and
  the lever arm are conditional on |O|, recorded with every result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
from fishsense_core import size_constancy as sc

__all__ = [
    "F_PX",
    "MIN_FRAMES",
    "N_BOOT",
    "O_DESIGN_M",
    "PortFit",
    "VARIANT_INDEX",
    "port_fit",
]

#: Pixels per radian for `err_deg` (slate_unknown.F_PX): fixed, not per camera.
F_PX = 2850.0
#: Frames a fit needs (slate_unknown.MIN_FRAMES).
MIN_FRAMES = 8
#: Bootstrap draws (size_constancy_sam.N_BOOT).
N_BOOT = 500
#: The assumed laser offset, m: the fleet median of 31 stored calibrations,
#: standing in for the mount's CAD value (score.py `O_DESIGN_M`).
O_DESIGN_M = sc.O_DESIGN_M
#: The script's variants, whose index seeds each one's bootstrap.
VARIANT_INDEX = {"sam3": 0, "corners": 1, "sam3_all": 2, "sam3_predline": 3}


@dataclass(frozen=True)
class PortFit:
    """One fit, as the script reports it, plus the v2 additions."""

    tv: float
    origin: np.ndarray
    axis: np.ndarray
    frames: int
    size_spread: float
    se_px: float
    se_deg: float
    #: Signed along-line offset of the vanishing point from the reference's,
    #: and the axes' 3-D angle; None with no reference calibration.
    err_px: Optional[float] = None
    err_deg_f2850: Optional[float] = None
    err_deg_fx: Optional[float] = None
    angle_deg: Optional[float] = None
    #: SD of log s - log(t - t_v) - c over the frames (c their median).
    residual: Optional[float] = None


def port_fit(
    xy: np.ndarray,
    s: np.ndarray,
    u: np.ndarray,
    off: float,
    K: np.ndarray,
    reference_axis: Optional[np.ndarray],
    *,
    seed: tuple[int, int],
    o_mag: float = O_DESIGN_M,
    n_boot: int = N_BOOT,
) -> PortFit:
    """size_constancy_sam.py's fit of one variant: dots `xy` (full-res
    rectified px), sizes `s`, on the line (`u`, `off`), against the reference
    calibration's axis. `seed` is ``(session, VARIANT_INDEX[variant])``."""
    xy, s, u, K = (np.asarray(a, float) for a in (xy, s, u, K))
    t = xy @ u
    tv = sc.fit_vanishing_position(t, s)
    se_px = sc.bootstrap_se(t, s, rng=np.random.default_rng(list(seed)), draws=n_boot)
    origin, axis = sc.beam_from_vanishing(tv, u, off, K, o_mag)
    log_r = np.log(s) - np.log(t - tv)
    out = dict(
        tv=float(tv),
        origin=origin,
        axis=axis,
        frames=len(t),
        size_spread=float(s.max() / s.min()),
        se_px=se_px,
        se_deg=float(np.degrees(se_px / F_PX)),
        residual=float(np.std(log_r - np.median(log_r))),
    )
    if reference_axis is not None:
        sa = np.asarray(reference_axis, float)
        sa = sa / np.linalg.norm(sa)
        vs = K @ sa
        tv_true = float((vs[:2] / vs[2]) @ u)
        err_px = float(tv - tv_true)
        out.update(
            err_px=err_px,
            err_deg_f2850=float(np.degrees(err_px / F_PX)),
            err_deg_fx=float(np.degrees(err_px / K[0][0])),
            angle_deg=float(np.degrees(np.arccos(np.clip(axis @ sa, -1, 1)))),
        )
    return PortFit(**out)
