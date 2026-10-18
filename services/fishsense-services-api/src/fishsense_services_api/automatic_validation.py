"""The automatic-results validation harness: the paper's metrics, pure.

New in v2. Scores automatic results against human labels and tape exactly as
cscw-fishsense2027@96a8da07 did:

* **pool** (e2e_measurement/score.py): on the fish-model and ruler frames of
  the ten pool dives (run_e2e.py `ORDER`), the stage ladder of Table 4 -- each
  human input replaced in turn by the automatic one -- scored per fish
  (production's p90 estimator per dive x model) against the tape length.
  Paper: manual 3.0 %, fully automatic (auto dot, auto head/tail, label-free
  calibration) 10.5 % at 91 % of frames;
* **reef** (e2e_measurement/tail/evaluate.py): on the eight calibrated reef
  dives (tail/stage.py `CALIBRATED_REEF`), the fully automatic length (auto
  dot, auto head/tail) against the manual one (human dot and clicks), **same
  stored calibration**. Paper: median +0.3 %, MAE 5.4 %, 62 % within 5 %, 82 %
  within 10 %, 3.1 % off by over 20 %; and coverage of the frames humans
  measured, over all ten reef dives (below);
* **calibration** (calibration_analysis/e1j_size_constancy/register.py,
  Table 3): each session's label-free vanishing point against its stored
  calibration's, along the dive line, in degrees.

**The paper's own reef frames** (Phase C, 2026-10-18): tail/stage.py's 791,
as e2e_measurement/tail/frames.csv persisted them -- every frame of the eight
calibrated dives, and 362 and 366 sampled to 200 each (pandas, seed 0, which
SQL cannot redraw) -- by v1 image id, which v2 captures carry as `v1_id`
(`PAPER_REEF_FRAMES`; all 791 found in production, same dives). 289 of them
were rejected by labellers as not measurable (a head/tail label with no
points): coverage is over the 502 measured (tail/gate.py), and the rejected
frames given a length are their own metric. **Coverage, seeded where?** The
paper's stated 72 % (red 91 %, green 66 %) seeds SAM at the *human* dot
(tail/coverage.py); the fully automatic chain seeds at the automatic dot, for
which the paper's own tail/keypoints.csv gives 357/502 = 71.1 % (red 92.2 %,
green 64.9 %) and a mask on 47/289 = 16.3 % of rejected frames -- derived,
not stated in the paper. The report compares against the automatic-dot
numbers and shows the human-dot ones beside them.

Geometry is score.py's (`depth`: the z of the laser ray's point closest to the
dot's camera ray; `length`: head and tail on the plane at that depth), which
stage 14's kernel reproduces to its float32 floor. The frames come from a
database (`automatic_validation_store`); the CLI is `validate-automatic`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

__all__ = [
    "LADDER",
    "PAPER",
    "PAPER_CALIBRATION_SESSIONS",
    "PAPER_POOL_DIVES",
    "PAPER_REEF_CALIBRATED_DIVES",
    "PAPER_REEF_DIVES",
    "PAPER_REEF_FRAMES",
    "PAPER_TABLE3",
    "LadderRow",
    "SessionCalibration",
    "Table3Row",
    "calibration_error_deg",
    "reef_rejected_given_length",
    "table3",
    "ReefComparison",
    "ValidationFrame",
    "depth",
    "fish_p90",
    "format_report",
    "length",
    "pool_ladder",
    "reef_comparison",
    "reef_coverage",
]

#: run_e2e.py `ORDER`: fish-model dives first, then the angle tests.
PAPER_POOL_DIVES = (58, 59, 60, 61, 66, 76, 84, 87, 94, 114)
#: Table 3's calibration sessions (register.py `DIVES`); the pool dives
#: borrow them through their calibration links (score.py `PAIR`).
PAPER_CALIBRATION_SESSIONS = (62, 63, 65, 71, 77, 80, 83, 87, 94, 114)
#: tail/stage.py `CALIBRATED_REEF`.
PAPER_REEF_CALIBRATED_DIVES = (347, 341, 465, 349, 279, 471, 436, 383)
#: ...and the two green-laser reef dives with no stored calibration.
PAPER_REEF_DIVES = (*PAPER_REEF_CALIBRATED_DIVES, 362, 366)

#: The paper's numbers, for the report.
PAPER = {
    "pool": {"A": 0.030, "B": 0.031, "C": 0.102, "D": 0.034, "E": 0.101, "F": 0.105},
    "pool_coverage": {"A": 1.0, "B": 0.997, "C": 0.92, "D": 1.0, "E": 0.91, "F": 0.91},
    "reef": {"median": 0.003, "mae": 0.054, "within_5": 0.62, "within_10": 0.82,
             "over_20": 0.031},
    # Seeded at the automatic dot: the fully automatic chain (derived from
    # tail/keypoints.csv; see the module docstring).
    "reef_coverage": {"all": 0.711, "red": 0.922, "green": 0.649},
    "reef_rejected_given_length": 0.163,
    # Seeded at the human dot: what PAPER.md states (72 %, 91 %, 66 %).
    "reef_coverage_human_dot": {"all": 0.719, "red": 0.913, "green": 0.661},
    "reef_rejected_given_length_human_dot": 0.156,
}  # fmt: skip

#: Table 3 (PAPER.md §4.3, labelfree_tv.csv): each session's label-free
#: vanishing-point error along the dive line, degrees.
PAPER_TABLE3 = {62: 0.033, 63: -0.002, 65: 0.025, 71: 0.009, 77: 0.009, 80: 0.007,
                83: -0.038, 87: -0.031, 94: -0.044, 114: -0.129}  # fmt: skip
#: register.py's focal length for the error in degrees.
TABLE3_F_PX = 2850.0


def _paper_reef_frames() -> dict[int, tuple[int, bool]]:
    data = json.loads(
        (Path(__file__).parent / "automatic_validation_reef_frames.json").read_text()
    )
    return {image: (dive, measured) for image, dive, measured in data["frames"]}


#: The paper's 791 reef frames: v1 image id -> (dive number, human-measured).
PAPER_REEF_FRAMES = _paper_reef_frames()

#: Table 4: (config, dot, head/tail, calibration). C's automatic head/tail is
#: seeded by the *human* dot (production's head/tail predictions are).
LADDER = (
    ("A", "human", "human", "stored"),
    ("B", "auto", "human", "stored"),
    ("C", "human", "auto_humandot", "stored"),
    ("D", "human", "human", "label_free"),
    ("E", "auto", "auto", "stored"),
    ("F", "auto", "auto", "label_free"),
)

Point = tuple[float, float]
HeadTail = tuple[float, float, float, float]
Calibration = tuple[Sequence[float], Sequence[float]]


@dataclass(frozen=True)
class ValidationFrame:
    """One human-labelled frame and what the automatic chain made of it."""

    capture_number: int
    dive_number: int
    set: str
    model: Optional[str]
    true_length_m: Optional[float]
    green: bool
    camera_matrix: list
    human_dot: Point
    #: None on a frame the labeller rejected as not measurable.
    human_head_tail: Optional[HeadTail]
    auto_dot: Optional[Point]
    #: The automatic head/tail seeded at the automatic dot (None: no length).
    auto_head_tail: Optional[HeadTail]
    stored_calibration: Optional[Calibration]
    label_free_calibration: Optional[Calibration]
    #: Seeded at the human dot instead (ladder row C), where known.
    auto_head_tail_humandot: Optional[HeadTail] = None
    #: The labeller rejected it as not measurable (a head/tail label with no
    #: points): out of coverage's denominator, in its own metric.
    rejected: bool = False


def depth(k, origin, axis, x: float, y: float) -> float:
    """score.py `depth`: z of the laser point closest to the dot's ray."""
    d = np.linalg.solve(np.asarray(k, float), [x, y, 1.0])
    a = np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    o = np.asarray(origin, float)
    m = np.array([[d @ d, -d @ a], [d @ a, -a @ a]])
    _, t = np.linalg.solve(m, np.array([d @ o, a @ o]))
    return float((o + t * a)[2])


def length(k, z: float, hx: float, hy: float, tx: float, ty: float) -> float:
    """score.py `length`: head to tail on the plane at depth z."""
    ki = np.linalg.inv(np.asarray(k, float))
    return float(np.linalg.norm(ki @ [hx, hy, 1.0] * z - ki @ [tx, ty, 1.0] * z))


def fish_p90(lengths: Sequence[float]) -> float:
    """score.py `p90`, production's per-fish estimator."""
    s = np.sort(np.asarray(lengths, float))
    return float(s[min(len(s) - 1, int(math.ceil(0.9 * len(s))) - 1)])


def _length(frame: ValidationFrame, dot, head_tail, calibration) -> Optional[float]:
    if dot is None or head_tail is None or calibration is None:
        return None
    if not all(v is not None and math.isfinite(v) for v in (*dot, *head_tail)):
        return None
    z = depth(frame.camera_matrix, calibration[0], calibration[1], *dot)
    if z <= 0:
        return None
    return length(frame.camera_matrix, z, *head_tail)


def _inputs(frame: ValidationFrame, dot: str, head_tail: str, calibration: str):
    return (
        frame.human_dot if dot == "human" else frame.auto_dot,
        {
            "human": frame.human_head_tail,
            "auto": frame.auto_head_tail,
            "auto_humandot": frame.auto_head_tail_humandot,
        }[head_tail],
        frame.stored_calibration if calibration == "stored"
        else frame.label_free_calibration,
    )  # fmt: skip


@dataclass(frozen=True)
class LadderRow:
    config: str
    dot: str
    head_tail: str
    calibration: str
    frames: int
    measured: int
    coverage: float
    median_err: Optional[float]
    mae: Optional[float]
    fish_p90_mae: Optional[float]
    fish_n: int


def pool_ladder(frames: Sequence[ValidationFrame]) -> list[LadderRow]:
    """Table 4 over pool frames with a tape length. Row C only where some frame
    has a head/tail seeded at the human dot."""
    pool = [f for f in frames if f.true_length_m]
    rows = []
    for config, dot, ht, cal in LADDER:
        if ht == "auto_humandot" and not any(f.auto_head_tail_humandot for f in pool):
            continue
        measured = []
        for f in pool:
            got = _length(f, *_inputs(f, dot, ht, cal))
            if got is not None:
                measured.append((f, got))
        errors = np.array([got / f.true_length_m - 1 for f, got in measured])
        fish: dict[tuple, list[float]] = {}
        for f, got in measured:
            fish.setdefault((f.dive_number, f.model), []).append(got)
        truth = {(f.dive_number, f.model): f.true_length_m for f in pool}
        fish_err = [fish_p90(v) / truth[k] - 1 for k, v in fish.items()]
        rows.append(
            LadderRow(
                config=config, dot=dot, head_tail=ht, calibration=cal,
                frames=len(pool), measured=len(measured),
                coverage=len(measured) / len(pool) if pool else 0.0,
                median_err=float(np.median(errors)) if len(errors) else None,
                mae=float(np.abs(errors).mean()) if len(errors) else None,
                fish_p90_mae=float(np.mean(np.abs(fish_err))) if fish_err else None,
                fish_n=len(fish_err),
            )
        )  # fmt: skip
    return rows


@dataclass(frozen=True)
class ReefComparison:
    frames: int
    measured: int
    median: Optional[float]
    mae: Optional[float]
    within_5: Optional[float]
    within_10: Optional[float]
    over_20: Optional[float]


def reef_comparison(frames: Sequence[ValidationFrame]) -> ReefComparison:
    """Fully automatic vs manual length, same stored calibration (evaluate.py),
    on reef frames whose dive has one."""
    reef = [
        f for f in frames if f.set == "reef" and f.stored_calibration and not f.rejected
    ]
    rel = []
    for f in reef:
        manual = _length(f, f.human_dot, f.human_head_tail, f.stored_calibration)
        auto = _length(f, f.auto_dot, f.auto_head_tail, f.stored_calibration)
        if manual and auto is not None:
            rel.append(auto / manual - 1)
    r = np.abs(np.array(rel))
    stat = (lambda v: float(v)) if rel else (lambda v: None)
    return ReefComparison(
        frames=len(reef),
        measured=len(rel),
        median=stat(np.median(rel)) if rel else None,
        mae=stat(r.mean()) if rel else None,
        within_5=stat((r <= 0.05).mean()) if rel else None,
        within_10=stat((r <= 0.10).mean()) if rel else None,
        over_20=stat((r > 0.20).mean()) if rel else None,
    )


def reef_coverage(frames: Sequence[ValidationFrame]) -> dict[str, Optional[float]]:
    """Share of the reef frames humans measured that got an automatic
    head/tail (a kept mask at the automatic dot), overall and by laser."""
    reef = [f for f in frames if f.set == "reef" and not f.rejected]

    def share(group):
        return (
            sum(f.auto_head_tail is not None for f in group) / len(group)
            if group
            else None
        )

    return {
        "all": share(reef),
        "red": share([f for f in reef if not f.green]),
        "green": share([f for f in reef if f.green]),
    }


def reef_rejected_given_length(frames: Sequence[ValidationFrame]) -> Optional[float]:
    """Share of the frames labellers rejected that the chain measured anyway."""
    rejected = [f for f in frames if f.set == "reef" and f.rejected]
    if not rejected:
        return None
    return sum(f.auto_head_tail is not None for f in rejected) / len(rejected)


@dataclass(frozen=True)
class SessionCalibration:
    """A calibration session's stored laser axis, and its label-free fit as
    the fit recorded it: `vanishing_px` is register.py's ``tv_fit`` (the
    vanishing point's position along the dive line) and `line_direction` its
    ``u`` (`size_constancy.line_direction`, slate_unknown.frame's rule)."""

    dive_number: int
    camera_matrix: list
    stored_axis: Optional[Sequence[float]]
    vanishing_px: Optional[float]
    line_direction: Optional[Sequence[float]]


def calibration_error_deg(session: SessionCalibration) -> Optional[float]:
    """register.py: ``err_px = tv_fit - v . u``, v the stored calibration's
    vanishing point (``fx ax/az + cx, fy ay/az + cy``); in degrees at
    f = 2850 px. None without an accepted fit or a stored axis."""
    if (
        session.vanishing_px is None
        or not session.line_direction
        or not session.stored_axis
    ):
        return None
    k = np.asarray(session.camera_matrix, float)
    ax = np.asarray(session.stored_axis, float)
    v = np.array([k[0, 0] * ax[0] / ax[2] + k[0, 2], k[1, 1] * ax[1] / ax[2] + k[1, 2]])
    err_px = session.vanishing_px - float(v @ np.asarray(session.line_direction, float))
    return math.degrees(err_px / TABLE3_F_PX)


@dataclass(frozen=True)
class Table3Row:
    dive_number: int
    err_deg: Optional[float]
    paper_err_deg: Optional[float]


def table3(
    sessions: Sequence[SessionCalibration], expected: Sequence[int] = ()
) -> list[Table3Row]:
    """A row per session, and one per `expected` session not among them (no
    error: no dive, or no accepted fit), so a missing one shows."""
    errors = {s.dive_number: calibration_error_deg(s) for s in sessions}
    return [
        Table3Row(
            dive_number=n, err_deg=errors.get(n), paper_err_deg=PAPER_TABLE3.get(n)
        )
        for n in sorted({*errors, *expected})
    ]


def _deg(v: Optional[float]) -> str:
    return "    -   " if v is None else f"{v:+7.3f}°"


def _pct(v: Optional[float], signed: bool = False) -> str:
    if v is None:
        return "   -  "
    return f"{100 * v:+6.1f}%" if signed else f"{100 * v:6.1f}%"


def format_report(
    source: str,
    ladder: Sequence[LadderRow],
    reef: ReefComparison,
    coverage: dict[str, Optional[float]],
    *,
    rejected: Optional[float] = None,
    calibration: Sequence[Table3Row] = (),
) -> str:
    """The paper's tables, with its numbers beside ours."""
    lines = [f"automatic-results validation ({source})", "",
             "POOL (Table 4): per-fish p90 error against tape",
             "  cfg  dot   head/tail      calibration  frames  measured  coverage"
             "   per-fish  (paper)  fish"]  # fmt: skip
    for r in ladder:
        lines.append(
            f"  {r.config:<4} {r.dot:<5} {r.head_tail:<14} {r.calibration:<12}"
            f" {r.frames:>6} {r.measured:>9} {_pct(r.coverage)} {_pct(r.fish_p90_mae)}"
            f" ({_pct(PAPER['pool'].get(r.config))}) {r.fish_n:>4}"
        )
    p = PAPER["reef"]
    lines += [
        "",
        "REEF: fully automatic vs manual length, same stored calibration",
        f"  frames {reef.frames}, automatic length for {reef.measured}",
        f"  median {_pct(reef.median, True)} (paper {_pct(p['median'], True)})",
        f"  MAE    {_pct(reef.mae)} (paper {_pct(p['mae'])})",
        f"  <=5%   {_pct(reef.within_5)} (paper {_pct(p['within_5'])})",
        f"  <=10%  {_pct(reef.within_10)} (paper {_pct(p['within_10'])})",
        f"  >20%   {_pct(reef.over_20)} (paper {_pct(p['over_20'])})",
        "",
        "REEF coverage of the frames humans measured",
        "  (paper: at the automatic dot, from its keypoints.csv; "
        "stated, at the human dot)",
    ]
    for key in ("all", "red", "green"):
        lines.append(
            f"  {key:<5} {_pct(coverage[key])} (paper {_pct(PAPER['reef_coverage'][key])};"
            f" human dot {_pct(PAPER['reef_coverage_human_dot'][key])})"
        )
    lines.append(
        f"  rejected by labellers, given a length anyway {_pct(rejected)} (paper"
        f" {_pct(PAPER['reef_rejected_given_length'])}; human dot"
        f" {_pct(PAPER['reef_rejected_given_length_human_dot'])})"
    )
    if calibration:
        lines += ["", "TABLE 3: label-free vanishing point vs stored, along the dive line",
                  "  session     ours    (paper)"]  # fmt: skip
        for r in calibration:
            lines.append(
                f"  {r.dive_number:>7} {_deg(r.err_deg)} ({_deg(r.paper_err_deg)})"
            )
    return "\n".join(lines)
