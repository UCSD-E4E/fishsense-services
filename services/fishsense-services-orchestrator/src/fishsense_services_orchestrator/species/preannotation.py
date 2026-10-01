"""Turn a stored species judgement into a Label Studio pre-annotation.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-
worker/src/fishsense_api_workflow_worker/activities/species_preannotation.py.
v2: its inverse is `species.parsing.parse_results`, and the row it reads is
`fishsense_services_api.species_store.SpeciesLabelRow`.

A batch of hand-labelled species judgements arrived as a spreadsheet — 3,000
FSL-01 frames spanning 2023-08 to 2024-06, keyed by image checksum. They are
loaded as **sentinel** species rows (no Label Studio project), which every
preprocess cohort reads as "no label for this image", so the dives they belong
to stay in the normal labelling flow. This module renders such a row as an LS
prediction, so the labeler is shown the judgement pre-filled and confirms it
instead of retyping it.

**It is the exact inverse of `parse_results` in the species sync**, and that
parser is the definition of record for what a result shape means. The
round-trip is pinned in `test_species_preannotation.py`, for the same reason
the taxonomy SQL/Python parity test exists: a pre-annotation whose shape the
parser reads differently would write the wrong column the moment a labeler
accepted it, with nothing raising.

Three fields are deliberately **not** pre-annotated, and this is the important
part of the design:

* `grouping` — a judgement about the frame *before* this one. The spreadsheet
  has no column for it and it is not inferable from one frame.
* `exclude` / `top_three_photos_of_group` — selects which frames of an
  individual get measured, and stage 14 keys measurability on it. A fabricated
  default would silently decide what gets measured.
* the slate branch's type choice (`H-Slate`, `V-Slate 2`, …) — the spreadsheet
  records a bare `Slate`, which is the taxonomy's parent node, not a leaf.
  The dive's slate template is written only from that choice and a wrong
  slate yields a wrong *scale*, which is precisely what reprojection residual
  cannot see. So the 346 slate rows carry no usable `content_of_image` and are
  not loaded at all; they stay a human's job.
"""

from typing import Any, Dict, Optional, Protocol

__all__ = ["PREANNOTATION_MODEL_VERSION", "build_prediction"]

#: Stamped on every prediction so a labeler (and anyone reading the task later)
#: can tell this came from the spreadsheet import and not from a model.
PREANNOTATION_MODEL_VERSION = "species-csv-import-2026-09"

#: LS control names, from `SPECIES_LABELING_CONFIG_XML`. Every control targets
#: the `image` object; a mismatched `to_name` makes LS drop the prediction
#: silently rather than erroring.
_IMAGE_OBJECT = "image"
_SPECIES_CONTROL = "species"
_TAXONOMY_CONTROLS = (
    ("measurable", "fish_measurable_category"),
    ("fishAngles", "fish_angle_category"),
    ("fishCurve", "fish_curved_category"),
)


class _HasJudgement(Protocol):
    """The four attributes this module reads. `SpeciesLabelRow` satisfies it."""

    content_of_image: Optional[str]
    fish_measurable_category: Optional[str]
    fish_angle_category: Optional[str]
    fish_curved_category: Optional[str]


def _taxonomy_result(from_name: str, path: list[str]) -> Dict[str, Any]:
    return {
        "from_name": from_name,
        "to_name": _IMAGE_OBJECT,
        "type": "taxonomy",
        "value": {"taxonomy": [path]},
    }


def _species_path(content: Optional[str]) -> Optional[list[str]]:
    """`content_of_image` split into the nested path LS wants, or None when it
    is not a full path to a leaf.

    The species Taxonomy is `leafsOnly="true"`, so a value that stops at a
    parent names nothing selectable. LS answers such a prediction either by
    dropping it or by rejecting the whole import batch -- and a rejected batch
    takes the run's real tasks down with it, so this refuses rather than sends.

    `"Fish Model,"` is the case that makes this necessary rather than
    defensive: it is what a labeler produces by picking the parent node, it
    exists in prod, and it is the same value `rigid_target_sql` needed a TRIM
    guard for. A spreadsheet's bare `"Slate"` is the other.
    """
    if not content:
        return None
    parts = [part.strip() for part in content.split(", ")]
    if len(parts) < 2 or any(not part for part in parts):
        return None
    return parts


def build_prediction(label: _HasJudgement) -> Optional[Dict[str, Any]]:
    """Render `label` as an LS prediction, or None if it says nothing.

    `content_of_image` is a ", "-joined taxonomy path, so it splits back into
    the nested path LS wants -- but only if it reaches a leaf; see
    `_species_path`. A malformed species value drops that one result and keeps
    the rest, because a usable measurable or angle judgement arriving beside a
    bad species value is still worth showing. The three attribute taxonomies
    are flat, and the parser reads them with `taxonomy[0][0]` — the first
    element of the first path — so each must be emitted as a
    **single-element** path or the parser would read a parent where a leaf was
    meant.
    """
    results: list[Dict[str, Any]] = []

    path = _species_path(getattr(label, "content_of_image", None))
    if path is not None:
        results.append(_taxonomy_result(_SPECIES_CONTROL, path))

    for control, attribute in _TAXONOMY_CONTROLS:
        value = getattr(label, attribute, None)
        if value:
            results.append(_taxonomy_result(control, [value]))

    if not results:
        return None
    return {"model_version": PREANNOTATION_MODEL_VERSION, "result": results}
