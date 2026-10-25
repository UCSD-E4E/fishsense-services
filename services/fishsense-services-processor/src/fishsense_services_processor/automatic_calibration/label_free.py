"""The label-free fit's result, as services' calibration fits share it.

fishsense-core's `size_constancy.LabelFreeFit` plus `scale_sd`, and `refused`,
used by calibration v3 (`v3`, the known-size fit) and v4 (`pose`, the slate's
pose). Neither imports the other for it (review of #354: `pose` took both
from `v3`).

`refused` derives its empty fields from core's dataclass rather than listing
them (review of #354): core 4.3.2 added `implausible_pairs`, and a copied
list would have raised on every services refusal once it was pinned.
"""

from __future__ import annotations

import typing
from dataclasses import MISSING, dataclass, fields
from typing import Any, Optional

from fishsense_core import size_constancy as sc

__all__ = ["LabelFreeFit", "refused"]


@dataclass(frozen=True)
class LabelFreeFit(sc.LabelFreeFit):
    """fishsense-core's `size_constancy.LabelFreeFit`, plus `scale_sd`, which
    v3 sets and core does not carry (core #97 left services-only fields to
    services): the length scale's predicted SD from dot and size noise
    (calibration v3 on SAM sizes, 2026-10-23); None where not predicted."""

    scale_sd: Optional[float] = None

    @classmethod
    def of(cls, fit: sc.LabelFreeFit) -> "LabelFreeFit":
        """`fit` as this type (unchanged if it already is one)."""
        if isinstance(fit, cls):
            return fit
        return cls(**{f.name: getattr(fit, f.name) for f in fields(sc.LabelFreeFit)})


def _empty(hint: Any) -> Any:
    """What a refused fit holds in a field it never reached."""
    origin = typing.get_origin(hint)
    if origin is typing.Union and type(None) in typing.get_args(hint):
        return None
    if hint is list or origin is list:
        return []
    if hint is int:
        return 0
    raise TypeError(f"no empty value for a LabelFreeFit field of type {hint!r}")


def refused(reason: str, *, o_mag_m: float = sc.O_DESIGN_M, **seen) -> LabelFreeFit:
    """A refused fit, with what it saw: every field not in `seen` empty."""
    hints = typing.get_type_hints(LabelFreeFit)
    values = {
        f.name: (f.default if f.default is not MISSING else _empty(hints[f.name]))
        for f in fields(LabelFreeFit)
        if f.name not in ("outcome", "refusal_reason", "o_mag_m")
    }
    values.update(seen)
    return LabelFreeFit(
        outcome="refused", refusal_reason=reason, o_mag_m=o_mag_m, **values
    )
