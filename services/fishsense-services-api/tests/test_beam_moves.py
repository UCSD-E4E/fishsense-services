"""When did the laser move mid-dive? The detector (2026-10-25).

A mission's laser dots lie on one image line while the laser holds its aim.
On 21 of the 63 V-Slate missions they lay on two parallel lines, 15-200 px
apart, each over its own stretch of the dive -- the beam's direction changed
by ~0.3-4 deg (a bumped or loose laser). Human clicks lie on both lines in
time order (250, 425, 437, 456), so it is the laser, not the detector; and
the zoom (EXIF) explains none of them at 4.5 mm.

Corpus-wide (read-only, 2026-10-25): 258 missions with >= 16 wide automatic
dots, 36 moves. The same detector on the human dots, as a control: of 185
missions with both, it finds no move the automatic dots miss, and agrees on
the 3 where human dots cover both sides.

Pinned here, on synthetic dots with known answers:

* one line is no move, however noisy within the 10 px tolerance;
* two parallel lines, time-split, is a move, at the window between the last
  dot on the first line and the first on the second;
* lines that cross (a false dot's line, a reflection) are no move, nor two
  lines too close to tell apart, nor a gain too small;
* too few dots decide nothing.
"""

from datetime import datetime, timedelta, timezone

import numpy as np

from fishsense_services_api.beam_moves import (
    DETECTOR_VERSION,
    MIN_DOTS,
    Dot,
    detect_beam_move,
)

T0 = datetime(2024, 12, 6, 14, 40, tzinfo=timezone.utc)


def _line(n, start_min, *, offset=0.0, angle_deg=72.0, noise=1.5, seed=0):
    """Dots along a beam's image line, one a minute from `start_min`, the line
    shifted sideways by `offset` px."""
    rng = np.random.default_rng(seed)
    a = np.radians(angle_deg)
    u, normal = np.array([np.cos(a), np.sin(a)]), np.array([-np.sin(a), np.cos(a)])
    base = np.array([2000.0, 1500.0]) + offset * normal
    return [
        Dot(at=T0 + timedelta(minutes=start_min + i),
            x=float(p[0]), y=float(p[1]))
        for i, p in enumerate(
            base + np.outer(rng.uniform(-300, 300, n), u)
            + np.outer(rng.normal(0, noise, n), normal))
    ]  # fmt: skip


def test_one_line_is_no_move():
    assert detect_beam_move(_line(60, 0, noise=3.0)) is None


def test_two_parallel_lines_split_in_time_is_a_move_at_the_window():
    """Mission 463's shape: 0-19 min on one line, 27-51 on another 45 px off."""
    before, after = _line(20, 0), _line(25, 27, offset=45.0, seed=1)

    move = detect_beam_move(before + after)

    assert move is not None
    assert (move.moved_after, move.moved_before) == (before[-1].at, after[0].at)
    assert move.detector_version == DETECTOR_VERSION
    assert 40 < move.apart_px < 50 and move.angle_deg < 1.0
    assert (move.dots_before, move.dots_after) == (20, 25)


def test_crossing_lines_are_no_move():
    """A second line at an angle is a false dot or a reflection, not an aim
    change (360, 400, 455: 40-56 deg)."""
    dots = _line(30, 0) + _line(30, 30, offset=60.0, angle_deg=30.0, seed=1)

    assert detect_beam_move(dots) is None


def test_lines_too_close_to_tell_apart_are_no_move():
    assert detect_beam_move(_line(30, 0) + _line(30, 30, offset=8.0, seed=1)) is None


def test_a_handful_of_dots_off_the_line_is_no_move():
    """The gain must be worth a second line: 6 dots, or 15% of the mission
    (here 10 dots on another line at the end of 130)."""
    dots = _line(120, 0) + _line(10, 120, offset=40.0, seed=1)

    assert detect_beam_move(dots) is None


def test_too_few_dots_decide_nothing():
    """8 dots on one line and 7 on another 60 px off: a split needs 8 a side
    (`MIN_SIDE`), so 15 dots (under `MIN_DOTS`) say nothing."""
    dots = _line(8, 0) + _line(7, 30, offset=60.0, seed=1)

    assert MIN_DOTS == 16
    assert detect_beam_move(dots) is None


def test_the_answer_does_not_depend_on_the_order_given():
    before, after = _line(20, 0), _line(25, 27, offset=45.0, seed=1)
    shuffled = list(np.random.default_rng(5).permutation(np.array(before + after)))

    assert detect_beam_move(shuffled) == detect_beam_move(before + after)
