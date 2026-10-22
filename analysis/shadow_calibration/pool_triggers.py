"""How often each proposed refusal and gate step triggers on the 10 pool sessions.

For the author's threshold decision (review of the shadow design, 2026-10-21):
"report for the 10 pool sessions how many each proposed refusal (Section 6) and
each gate step (Section 5.3) would trigger". Read-only; nothing here runs in
production.

Inputs, all pinned:
- cscw-fishsense2027@8312780a (`CSCW`): the port's pool frames, human dots,
  SAM 3.1 sizes and scores, laser lines, stored calibrations and intrinsics --
  exactly what `analysis/codesign/size_constancy_sam.py` reads.
- `data/pool_automatic_dots.csv`: the automatic track's detector dots on the
  pool missions' every canonical frame, exported read-only from production
  (2026-10-21). A STAND-IN for `laser_predictions`: same detector kernel and
  region gate, but production's human-path detector covers only 128 of these
  missions' 1,596 frames.
- The fit is cscw's own `slate_unknown.fit_tv`; the 0.6 m rule is production's
  own `calibration.consistency.check_observation_geometry`, imported, not copied.

Known limit: the detector source reuses the SAM size chosen at the HUMAN dot.
The stage will re-select the mask at the detector dot; a dot far from the
human one would pick another mask, so these counts are a lower bound on what
that changes.

Run: uv run --project services/fishsense-services-processor \\
         python analysis/shadow_calibration/pool_triggers.py [CSCW]
"""

from __future__ import annotations

import ast
import csv
import sys
from pathlib import Path

import numpy as np

CSCW = Path(sys.argv[1] if len(sys.argv) > 1
            else "/home/chris/Repos/school/e4e/fishsense/cscw-fishsense2027")
sys.path.insert(0, str(CSCW / "calibration_analysis/e1j_size_constancy"))
import slate_unknown as su  # noqa: E402  (the port's own estimator)

from fishsense_services_processor.calibration.consistency import (  # noqa: E402
    CalibrationUnderdeterminedError,
    MIN_OBSERVATION_LEVER_M,
    check_observation_geometry,
)

HERE = Path(__file__).parent
POOL = (62, 63, 65, 71, 77, 80, 83, 87, 94, 114)
O_DESIGN_M, N_BOOT = 0.104, 500
# Proposed thresholds (design §5.3, §6).
MIN_CONF, OFF_LINE_PX, SECOND_LINE_SHARE, RESID_MAX = 0.5, 10.0, 0.25, 0.10
MIN_SPREAD, MAX_SE_DEG, MIN_MED_SCORE, MAX_NO_MASK = 1.5, 0.05, 0.5, 0.25
STRICT_DEG, BUDGET_DEG = 0.15, 0.389  # IMWUT §5.3: -0.526..+0.389 at 2 m


def rows(p, delim=","):
    return list(csv.DictReader(open(p), delimiter=delim))


def ransac_line(xy, rng, tol=OFF_LINE_PX, iters=500):
    """The port's `prediction_lines`: RANSAC, then TLS through the inliers;
    (u, offset, inlier mask) with u oriented to negative y."""
    best = None
    for _ in range(iters):
        i, j = rng.choice(len(xy), 2, replace=False)
        d = xy[j] - xy[i]
        if np.hypot(*d) < 1:
            continue
        nn = np.array([-d[1], d[0]]) / np.hypot(*d)
        inl = np.abs(xy @ nn - nn @ xy[i]) <= tol
        if best is None or inl.sum() > best.sum():
            best = inl
    q = xy[best]
    m = q.mean(0)
    u = np.linalg.svd(q - m)[2][0]
    u = u if u[1] < 0 else -u
    return u, float(m @ np.array([-u[1], u[0]])), best


def beam(tv, u, off, K, o_mag):
    """Axis and origin as `score.calibrations` builds them from a fitted t_v."""
    n = np.array([-u[1], u[0]])
    v = tv * u + off * n
    ax = np.linalg.solve(K, [v[0], v[1], 1.0])
    ax /= np.linalg.norm(ax)
    o = np.array([u[0] / K[0][0], u[1] / K[1][1]])
    return np.append(o_mag * o / np.linalg.norm(o), 0.0), ax


def laser_points(xy, K, origin, axis):
    """Each dot's camera-space point on the beam (closest to its pixel ray)."""
    Ki = np.linalg.inv(K)
    pts = []
    for x, y in xy:
        r = Ki @ [x, y, 1.0]
        r /= np.linalg.norm(r)
        # min |origin + l*axis - m*r| over (l, m)
        A = np.array([[axis @ axis, -axis @ r], [axis @ r, -r @ r]])
        b = np.array([-(origin @ axis), -(origin @ r)])
        lam, _ = np.linalg.solve(A, b)
        pts.append(origin + lam * axis)
    return np.array(pts)


def lever(xy, K, origin, axis):
    pts = laser_points(xy, K, origin, axis)
    return float(pts[:, 2].max() - pts[:, 2].min()), pts


def trim(t, s):
    """Drop, one at a time, a frame whose deleted residual exceeds RESID_MAX."""
    keep = np.arange(len(t))
    dropped = 0
    while len(keep) > 2:
        ls = np.log(s[keep])
        r = []
        for i in range(len(keep)):
            rest = np.delete(keep, i)
            tv = su.fit_tv(t[rest], s[rest])
            c = np.median(np.log(s[rest]) - np.log(t[rest] - tv))
            gap = t[keep[i]] - tv
            r.append(np.inf if gap <= 0 else abs(ls[i] - np.log(gap) - c))
        w = int(np.argmax(r))
        if r[w] <= RESID_MAX:
            break
        keep = np.delete(keep, w)
        dropped += 1
    return keep, dropped


def pinned(t, tv):
    hi = float(np.min(t)) - 5.0
    return tv > hi - 1.0 or tv < hi - 4000.0 + 1.0


def fit(source, dive, xy, s, u, off, K, stored_axis, stored_pos, scores, nomask):
    t = xy @ u
    out = {"source": source, "session": dive, "frames": len(t)}
    tv = su.fit_tv(t, s)
    brng = np.random.default_rng([dive, 0 if source == "labels" else 3])
    boots = [su.fit_tv(t[k], s[k]) for k in
             (brng.integers(0, len(t), len(t)) for _ in range(N_BOOT))]  # fmt: skip
    origin, axis = beam(tv, u, off, K, O_DESIGN_M)
    sa = stored_axis / np.linalg.norm(stored_axis)
    vs = K @ sa
    tv_true = float((vs[:2] / vs[2]) @ u)
    lev, pts = lever(xy, K, origin, axis)
    lev_prod, _ = lever(xy, K, beam(tv, u, off, K, float(np.linalg.norm(stored_pos[:2])))[0], axis)
    try:
        check_observation_geometry(pts)
        lever_ok = True
    except CalibrationUnderdeterminedError:
        lever_ok = False
    # Slate length constancy: sqrt-area in metres at each frame's range.
    z = pts[:, 2]
    size_m = s * (4014 / 1600) / K[0][0] * z
    out.update(
        size_spread=float(s.max() / s.min()),
        err_deg_f2850=float(np.degrees((tv - tv_true) / su.F_PX)),
        err_deg_fx=float(np.degrees((tv - tv_true) / K[0][0])),
        angle_deg=float(np.degrees(np.arccos(np.clip(axis @ sa, -1, 1)))),
        se_deg=float(np.degrees(np.std(boots) / su.F_PX)),
        lever_m=lev, lever_m_prod_offset=lev_prod,
        sam_median=float(np.median(scores)) if len(scores) else float("nan"),
        no_mask_share=nomask,
        size_cv_pct=float(100 * np.std(size_m) / np.mean(size_m)),
        size_maxdev_pct=float(100 * np.max(np.abs(size_m / np.median(size_m) - 1))),
        pinned=pinned(t, tv),
    )
    out["refusals"] = [r for r, hit in [
        ("too_few_frames", len(t) < su.MIN_FRAMES),
        ("too_little_size_spread", out["size_spread"] < MIN_SPREAD),
        ("lever_arm_below_0.6m", not lever_ok),
        ("se_too_high", out["se_deg"] > MAX_SE_DEG),
        ("sam_scores_low", out["sam_median"] < MIN_MED_SCORE or nomask > MAX_NO_MASK),
        ("unconstrained", out["pinned"]),
    ] if hit]  # fmt: skip
    return out


def main():
    E = CSCW / "calibration_analysis/e1j_size_constancy"
    frames = rows(E / "sizes_frames.csv")
    sizes = {r["image_id"]: float(r["size"]) for r in rows(E / "sizes/sam3_text.csv")}
    detail = {r["image_id"]: r for r in rows(E / "sam_sizes_detail.csv")}
    lines = {int(r["dive"]): r for r in rows(E / "labelfree_tv.csv")}
    ext = {int(r["dive_id"]): r for r in rows(CSCW / "e2e_measurement/extrinsics.psv", "|")}
    intr = {int(r["camera_id"]): np.array(ast.literal_eval(r["camera_matrix"]))
            for r in rows(CSCW / "laser_detection_analysis/intrinsics.csv")}  # fmt: skip
    auto = rows(HERE / "data/pool_automatic_dots.csv")
    results, gates = [], []
    for dive in POOL:
        K = intr[int(ext[dive]["camera_id"])]
        stored_axis = np.array(ast.literal_eval(ext[dive]["laser_axis"]), float)
        stored_pos = np.array(ast.literal_eval(ext[dive]["laser_position"]), float)
        fr = [r for r in frames if int(r["dive_id"]) == dive]
        sized = [r for r in fr if r["image_id"] in sizes]
        nomask = 1 - len(sized) / len(fr)
        L = lines[dive]
        u = np.array([float(L["ux"]), float(L["uy"])])
        # (a) label dots: the port exactly.
        xy = np.array([[float(r["dot_x"]), float(r["dot_y"])] for r in sized])
        s = np.array([sizes[r["image_id"]] for r in sized])
        sc = [float(detail[r["image_id"]]["text_score"]) for r in sized]
        results.append(fit("labels", dive, xy, s, u, float(L["line_offset"]), K,
                           stored_axis, stored_pos, sc, nomask))  # fmt: skip
        # (b) detector dots, through the proposed gate.
        dots = {r["v1_id"]: r for r in auto if int(r["dive"]) == dive}
        alld = np.array([[float(r["laser_x"]), float(r["laser_y"])] for r in dots.values()
                         if r["laser_x"] and float(r["laser_confidence"]) >= MIN_CONF])  # fmt: skip
        g = {"session": dive, "slate_frames": len(fr), "no_dot": 0, "conf_low": 0,
             "off_line": 0, "second_line": 0, "ambiguous_line": False,
             "trimmed": 0, "line_points": f"{0}/{len(alld)}"}  # fmt: skip
        du, doff, inl = ransac_line(alld, np.random.default_rng(0))
        g["line_points"] = f"{int(inl.sum())}/{len(alld)}"
        kept, off_dots = [], []
        for r in sized:
            d = dots.get(r["image_id"])
            if not d or not d["laser_x"]:
                g["no_dot"] += 1
                continue
            if float(d["laser_confidence"]) < MIN_CONF:
                g["conf_low"] += 1
                continue
            p = np.array([float(d["laser_x"]), float(d["laser_y"])])
            if abs(p @ np.array([-du[1], du[0]]) - doff) > OFF_LINE_PX:
                g["off_line"] += 1
                off_dots.append(p)
                continue
            kept.append((r, p))
        if len(off_dots) >= 2:
            _, _, rin = ransac_line(np.array(off_dots), np.random.default_rng(0))
            g["second_line"] = int(rin.sum())
            g["ambiguous_line"] = bool(g["second_line"] >= max(su.MIN_FRAMES, SECOND_LINE_SHARE * len(kept)))
        gates.append(g)
        if len(kept) < 3:
            results.append({"source": "detector", "session": dive, "frames": len(kept),
                            "refusals": ["too_few_frames"]})  # fmt: skip
            lab_keep, lab_trim = trim(xy @ u, s)
            results[-2]["trimmed_if_gated"] = lab_trim
            continue
        dxy = np.array([p for _, p in kept])
        ds = np.array([sizes[r["image_id"]] for r, _ in kept])
        keep, g["trimmed"] = trim(dxy @ du, ds)
        dsc = [float(detail[r["image_id"]]["text_score"]) for r, _ in kept]
        res = fit("detector", dive, dxy[keep], ds[keep], du, doff, K, stored_axis,
                  stored_pos, [dsc[i] for i in keep], nomask)  # fmt: skip
        if g["ambiguous_line"]:
            res["refusals"].insert(0, "ambiguous_line")
        results.append(res)
        # The trim on the labels source too (it is not the port, but §7.4's gated variant).
        lab_keep, lab_trim = trim(xy @ u, s)
        results[-2]["trimmed_if_gated"] = lab_trim
    return results, gates


if __name__ == "__main__":
    results, gates = main()
    keys = ["source", "session", "frames", "size_spread", "lever_m", "lever_m_prod_offset",
            "se_deg", "sam_median", "no_mask_share", "pinned", "err_deg_f2850", "err_deg_fx",
            "angle_deg", "size_cv_pct", "size_maxdev_pct", "refusals"]  # fmt: skip
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    with open(out / "pool_triggers.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys + ["trimmed_if_gated"], extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({**r, "refusals": ";".join(r["refusals"])})
    with open(out / "pool_gate.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(gates[0]))
        w.writeheader()
        w.writerows(gates)
    for r in results:
        if "size_spread" not in r:
            print(r["source"], r["session"], r["frames"], r["refusals"])
            continue
        print(r["source"], r["session"], r["frames"], round(r["size_spread"], 2),
              round(r["lever_m"], 2), round(r["lever_m_prod_offset"], 2), round(r["se_deg"], 3),
              round(r["sam_median"], 2), round(r["err_deg_f2850"], 4), round(r["err_deg_fx"], 4),
              round(r["angle_deg"], 3), round(r["size_cv_pct"], 1), r["refusals"], r.get("trimmed_if_gated", ""))  # fmt: skip
    for g in gates:
        print(g)
