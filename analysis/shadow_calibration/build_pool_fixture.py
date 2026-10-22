"""Build the pool reproduction fixture for the shadow calibration's port.

The shadow stage ports cscw-fishsense2027@8312780a
`analysis/codesign/size_constancy_sam.py`. Its published pool result
(`analysis/codesign/results/size_constancy_sessions.csv`) must reproduce after
the port (shadow design §9.2). This writes, per pool session, exactly the
inputs that script reads -- the human dots, SAM 3.1 sizes and scores, the
session's laser line and the detector line (`prediction_lines()`), the
camera, the stored calibration -- and the published rows to match, into
`services/fishsense-services-processor/tests/fixtures/shadow_pool.json`.

It imports cscw's own loaders, so the fixture is what the script saw.

Run: uv run --project services/fishsense-services-processor \\
         python analysis/shadow_calibration/build_pool_fixture.py [CSCW]
"""

from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

CSCW = Path(sys.argv[1] if len(sys.argv) > 1
            else "/home/chris/Repos/school/e4e/fishsense/cscw-fishsense2027")
OUT = (Path(__file__).parents[2]
       / "services/fishsense-services-processor/tests/fixtures/shadow_pool.json")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    sys.path.insert(0, str(CSCW / "calibration_analysis/e1j_size_constancy"))
    sys.path.insert(0, str(CSCW / "e2e_measurement"))
    sys.path.insert(0, str(CSCW))
    sc = load("size_constancy_sam", CSCW / "analysis/codesign/size_constancy_sam.py")
    E = CSCW / "calibration_analysis/e1j_size_constancy"
    commit = subprocess.run(["git", "-C", str(CSCW), "rev-parse", "--short=8", "HEAD"],
                            capture_output=True, text=True).stdout.strip()  # fmt: skip
    stored, _, intr = sc.S.calibrations()
    cams = {int(r["dive_id"]): int(r["camera_id"])
            for r in csv.DictReader(open(CSCW / "e2e_measurement/extrinsics.psv"), delimiter="|")}  # fmt: skip
    sizes = {r["image_id"]: float(r["size"]) for r in csv.DictReader(open(E / "sizes/sam3_text.csv"))}
    detail = {r["image_id"]: r for r in csv.DictReader(open(E / "sam_sizes_detail.csv"))}
    frames = list(csv.DictReader(open(E / "sizes_frames.csv")))
    lines = {int(r["dive"]): r for r in csv.DictReader(open(E / "labelfree_tv.csv"))}
    pl = sc.prediction_lines()
    published = list(csv.DictReader(open(CSCW / "analysis/codesign/results/size_constancy_sessions.csv")))
    # The automatic track's detector dots on every canonical frame of the pool
    # missions, exported read-only from production (2026-10-21): the shadow
    # stage's detector source (author, 2026-10-21).
    auto = list(csv.DictReader(open(Path(__file__).parent / "data/pool_automatic_dots.csv")))
    sessions = []
    for dive in sorted(lines):
        K = np.asarray(intr[cams[dive]], float)
        pos, axis = stored[dive]
        sessions.append({
            "session": dive,
            "camera_matrix": K.tolist(),
            "stored_laser_position": [float(v) for v in pos],
            "stored_laser_axis": [float(v) for v in axis],
            "line": {"u": [float(lines[dive]["ux"]), float(lines[dive]["uy"])],
                     "offset": float(lines[dive]["line_offset"])},
            "detector_line": {"u": [float(v) for v in pl[dive][0]], "offset": pl[dive][1],
                              "points": pl[dive][2], "of": pl[dive][3]},
            "frames": [
                {"image_id": int(r["image_id"]), "dot_x": float(r["dot_x"]),
                 "dot_y": float(r["dot_y"]), "size": sizes.get(r["image_id"]),
                 "sam_score": (float(detail[r["image_id"]]["text_score"])
                               if detail[r["image_id"]]["text_score"] else None),
                 "sam_prompt": detail[r["image_id"]]["text_prompt"] or None}
                for r in frames if int(r["dive_id"]) == dive
            ],
            "detector_dots": [
                {"image_id": int(r["v1_id"]), "x": float(r["laser_x"]),
                 "y": float(r["laser_y"]), "confidence": float(r["laser_confidence"])}
                for r in auto if int(r["dive"]) == dive and r["laser_x"]
            ],
            "published": {r["method"]: {k: float(r[k]) for k in
                                        ("frames", "size_spread", "err_deg", "angle_deg", "se_deg")}
                          for r in published if int(r["session"]) == dive},
        })  # fmt: skip
    # Dive 279's red specks (imwut_2026_fishsense_lite@ce372d6
    # fish_model_analysis/data/slate_sizes.csv; IN_PLANE_DRIFT.md:346-358).
    imwut = Path(str(CSCW).replace("cscw-fishsense2027", "imwut_2026_fishsense_lite"))
    dive279 = [
        {"image_id": int(r["image_id"]), "dot_x": float(r["dot_x"]), "dot_y": float(r["dot_y"]),
         "dot_source": r["dot_source"], "size": float(r["size"]) if r["size"] else None}
        for r in csv.DictReader(open(imwut / "fish_model_analysis/data/slate_sizes.csv"))
        if r["dive_id"] == "279"
    ]  # fmt: skip
    OUT.write_text(json.dumps({
        "dive279": dive279,
        "source": f"cscw-fishsense2027@{commit} analysis/codesign/size_constancy_sam.py",
        "f_px": 2850.0, "n_boot": 500, "sessions": sessions,
    }, indent=1))
    print(f"wrote {OUT} ({len(sessions)} sessions, {sum(len(s['frames']) for s in sessions)} frames)")


if __name__ == "__main__":
    main()
