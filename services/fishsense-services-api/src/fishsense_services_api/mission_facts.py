"""What a mission's names and paths say about it (PLAN.md §4.9, migration 0047).

Pure rules, shared by ingest and the backfill so the two cannot drift:

* **The port.** A device labeled with the ``D`` suffix (``FSL-10D``) is the
  corrective optic -- the owner's rule, 2026-10-16. A missing ``D`` says
  nothing: lab data holds bare-flat-port frames that were never reliably
  labeled, so the lack of it is not evidence of a flat port. Device identity
  stays the MakerNote serial; the suffix is stripped when matching names.

* **The deployment.** An e4e-data-management dataset folder,
  ``YYYY.MM.DD.PROJECT.LOCATION``, is a field trip. A folder with a date and a
  project only (``2024.06.20.REEF``) is a reorganized dump, not a dataset.

* **The purpose**, inferred from the mission's name for an operator to review,
  with the reason. Where two rules match, or the name is a split or a pool
  session no rule names, nothing is inferred: an unstated purpose is honest,
  and a wrong one would decide which stages use the mission.
"""

import re
from datetime import date
from pathlib import PurePosixPath
from typing import Literal, get_args

__all__ = [
    "PORTS",
    "PORT_SOURCES",
    "PURPOSES",
    "Port",
    "PortSource",
    "StatedPort",
    "Purpose",
    "RULED",
    "is_dataset_name",
    "dataset_date",
    "dataset_of",
    "device_label_in_path",
    "infer_purpose",
    "port_from_device_label",
    "strip_port_suffix",
]

#: Migrations 0047 and 0048's CHECKs, which are the source of truth.
PURPOSES = (
    "survey",
    "lens_calibration",
    "laser_calibration",
    "known_length",
    "other",
)
PORTS = ("corrective_optic", "flat", "dome", "unknown")
PORT_SOURCES = ("backfill", "device_suffix", "operator", "calibration")
Purpose = Literal[
    "survey", "lens_calibration", "laser_calibration", "known_length", "other"
]
Port = Literal["corrective_optic", "flat", "dome", "unknown"]
#: A port someone states: ``unknown`` is the absence of one.
StatedPort = Literal["corrective_optic", "flat", "dome"]
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
)
#: A calibration board, which nearly every dive carries (0048). It decides a
#: purpose only when the name is nothing but the board -- a date, the rig, a
#: number, "dive" around it (082323_Slate_FSL02, H slate dive 4) -- and then
#: means a calibration-only session. Beside a site or anything else it decides
#: nothing: that is a dive that carried a board, as most do.
_BOARD = re.compile(_word("slates?"), re.I)
_BOARD_ONLY_FILLER = re.compile(
    r"\d{4}-\d{2}-\d{2}|FSL-?\d+D?|(?<![a-z])(?:dive|session)(?![a-z])|\d+"
    r"|(?<![a-z])[a-z](?![a-z])|[\s_\-/.]+",
    re.I,
)
_POOL = re.compile(r"pool", re.I)
#: Calibration words the rules' exact phrases miss ("Checkerboard", "LaserCal",
#: "intrinsics", "cal 2"): such a session is not a survey, whatever else it is.
_CALIBRATION = re.compile(
    r"calib|checker|intrinsic|(?:laser|lens)\s*_?cal|(?<![a-z])cal(?![a-z])", re.I
)


def port_from_device_label(label: str | None) -> str | None:
    """``corrective_optic`` for a ``D``-suffixed device label, else None."""
    if label and _D_LABEL.match(label.strip()):
        return "corrective_optic"
    return None


def strip_port_suffix(name: str | None) -> str | None:
    """The device's own name: ``FSL-10D`` is the body named ``FSL-10``."""
    return None if name is None else _PORT_SUFFIX.sub("", name.strip())


def device_label_in_path(source_path: str) -> str | None:
    """A ``D``-labeled device named by a folder on the mission's path, or None.

    e4edm puts the rig in a folder (``ED-00/FSL-10D/Box``); production's
    2023.08.03 dataset has it as the location (``2023.08.03.FishSense.FSL-01D``).
    """
    for part in reversed(source_path.split("/")):
        for candidate in (part, part.rsplit(".", 1)[-1]):
            if port_from_device_label(candidate):
                return candidate
    return None


def dataset_date(name: str) -> date:
    """The date an e4edm dataset's name starts with (``YYYY.MM.DD``)."""
    return date(*(int(part) for part in name.split(".")[:3]))


def is_dataset_name(part: str) -> bool:
    """Whether a folder is named like an e4edm dataset (``YYYY.MM.DD.P.LOCATION``)."""
    return bool(_DATASET.match(part))


def dataset_of(source_path: str) -> tuple[str, str] | None:
    """The e4edm dataset a mission's folder sits in, as (name, path), or None."""
    parts = source_path.split("/")
    for i, part in enumerate(parts[:-1]):
        if is_dataset_name(part):
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
    if _CALIBRATION.search(said):
        return None, "a calibration no rule names"
    if _POOL.search(said):
        return None, "a pool session no rule names"
    if _board_only(name) and _board_only(leaf):
        return "laser_calibration", "the name names only a calibration board"
    return "survey", "no calibration or known-length word in the name"


def _board_only(text: str) -> bool:
    """The text is the board and filler (a date, the rig, a number, "dive")."""
    if not _BOARD.search(text):
        return False
    return _BOARD_ONLY_FILLER.sub("", _BOARD.sub("", text)) == ""


assert get_args(Purpose) == PURPOSES
assert get_args(Port) == PORTS
assert get_args(PortSource) == PORT_SOURCES
assert set(get_args(StatedPort)) == set(PORTS) - {"unknown"}
