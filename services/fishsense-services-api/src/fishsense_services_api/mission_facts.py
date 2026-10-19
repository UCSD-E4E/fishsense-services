"""What a mission's names and paths say about it (PLAN.md §4.9, migration 0047).

Pure rules, shared by ingest and the backfill so the two cannot drift:

* **The port.** A device labelled with the ``D`` suffix (``FSL-10D``) is the
  corrective optic -- the owner's rule, 2026-10-16. A missing ``D`` says
  nothing: lab data holds bare-flat-port frames that were never reliably
  labelled, so the lack of it is not evidence of a flat port. Device identity
  stays the MakerNote serial; the suffix is stripped when matching names.

* **The deployment.** An e4e-data-management dataset folder,
  ``YYYY.MM.DD.PROJECT.LOCATION``, is a field trip. A folder with a date and a
  project only (``2024.06.20.REEF``) is a reorganised dump, not a dataset.

* **The purpose**, inferred from the mission's name for an operator to review,
  with the reason. Where two rules match, or the name is a split or a pool
  session no rule names, nothing is inferred: an unstated purpose is honest,
  and a wrong one would decide which stages use the mission.
"""

import re
from pathlib import PurePosixPath
from typing import Literal, get_args

__all__ = [
    "PORTS",
    "PORT_SOURCES",
    "PURPOSES",
    "Port",
    "PortSource",
    "Purpose",
    "RULED",
    "dataset_of",
    "device_label_in_path",
    "infer_purpose",
    "port_from_device_label",
    "strip_port_suffix",
]

#: Migration 0047's CHECKs, which are the source of truth.
PURPOSES = (
    "survey",
    "lens_calibration",
    "laser_calibration",
    "slate",
    "known_length",
    "other",
)
PORTS = ("corrective_optic", "flat", "dome", "unknown")
PORT_SOURCES = ("backfill", "device_suffix", "operator", "calibration")
Purpose = Literal[
    "survey", "lens_calibration", "laser_calibration", "slate", "known_length", "other"
]
Port = Literal["corrective_optic", "flat", "dome", "unknown"]
PortSource = Literal["backfill", "device_suffix", "operator", "calibration"]
#: Sources that are the owner's rules: fact, which an operator cannot contradict.
RULED = ("backfill", "device_suffix")

_PORT_SUFFIX = re.compile(r"(?<=\d)[Dd]$")
#: A rig's label: letters, an optional dash, its number, then the suffix.
_D_LABEL = re.compile(r"^[A-Za-z]{2,}-?\d+[Dd]$")
_DATASET = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.[^.]+\.\S.*$")


def _word(*words: str) -> str:
    # Underscores and digits separate words in these names (082929_Slate_FSL05).
    return "|".join(rf"(?<![a-z]){w}(?![a-z])" for w in words)


#: In order of the purposes they name; a name matching two is not decided.
_PURPOSE_RULES = (
    ("laser_calibration", re.compile(r"laser\s*calibration", re.I)),
    ("lens_calibration", re.compile(r"lens\s*calibration", re.I)),
    (
        "known_length",
        re.compile(
            _word("box", "ginny", "george") + r"|fish\s*models?|angle\s*tests?", re.I
        ),
    ),
    ("slate", re.compile(r"slate", re.I)),
)
_POOL = re.compile(r"pool", re.I)


def port_from_device_label(label: str | None) -> str | None:
    """``corrective_optic`` for a ``D``-suffixed device label, else None."""
    if label and _D_LABEL.match(label.strip()):
        return "corrective_optic"
    return None


def strip_port_suffix(name: str | None) -> str | None:
    """The device's own name: ``FSL-10D`` is the body named ``FSL-10``."""
    return None if name is None else _PORT_SUFFIX.sub("", name.strip())


def device_label_in_path(source_path: str) -> str | None:
    """A ``D``-labelled device named by a folder on the mission's path, or None.

    e4edm puts the rig in a folder (``ED-00/FSL-10D/Box``); production's
    2023.08.03 dataset has it as the location (``2023.08.03.FishSense.FSL-01D``).
    """
    for part in reversed(source_path.split("/")):
        for candidate in (part, part.rsplit(".", 1)[-1]):
            if port_from_device_label(candidate):
                return candidate
    return None


def dataset_of(source_path: str) -> tuple[str, str] | None:
    """The e4edm dataset a mission's folder sits in, as (name, path), or None."""
    parts = source_path.split("/")
    for i, part in enumerate(parts[:-1]):
        if _DATASET.match(part):
            return part, "/".join(parts[: i + 1])
    return None


def infer_purpose(name: str | None, source_path: str) -> tuple[str | None, str]:
    """(purpose or None, why) from the mission's name and its folder's name."""
    if not name:
        return None, "the mission has no name"
    if "[split" in source_path:
        return None, "a split of another session"
    leaf = PurePosixPath(source_path).name
    said = f"{name} {leaf}"
    found = [purpose for purpose, rule in _PURPOSE_RULES if rule.search(said)]
    if len(found) > 1:
        return None, f"the name says both {' and '.join(found)}"
    if found:
        return found[0], f"the name says {found[0].replace('_', ' ')}"
    if _POOL.search(said):
        return None, "a pool session no rule names"
    return "survey", "no calibration or known-length word in the name"


assert get_args(Purpose) == PURPOSES
assert get_args(Port) == PORTS
assert get_args(PortSource) == PORT_SOURCES
