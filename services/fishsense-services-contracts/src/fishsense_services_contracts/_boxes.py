"""The pixel box both stages exchange: the head/tail stage's kept mask, which
the species stage crops by. Private (not a payload model): only the
validator is shared."""

from __future__ import annotations

from typing import List, Optional

__all__ = ["check_box"]


def check_box(box: Optional[List[int]]) -> Optional[List[int]]:
    """``[x_min, y_min, x_max, y_max]`` in rectified-frame pixels, the max
    exclusive (a numpy slice), so a box is never empty and never inverted."""
    if box is None:
        return None
    if len(box) != 4:
        raise ValueError(f"a box is [x_min, y_min, x_max, y_max], got {box}")
    x_min, y_min, x_max, y_max = box
    if min(x_min, y_min) < 0 or x_max <= x_min or y_max <= y_min:
        raise ValueError(f"not a non-empty box in frame pixels: {box}")
    return box
