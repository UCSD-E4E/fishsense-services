"""What a species Label Studio task says: its label's fields, and the dive's
slate template and calibration target.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/
sync_species_labels_for_label_studio_project_activity.py (`_first_choice`,
`_first_taxonomy_leaf`, `_content_of_image`, `_parse_results`,
`_slate_type_choice`, `_calibration_target_choice`, `_slate_not_in_list`,
`_reduce_winners`). Behaviour is v1's, the parser's shape especially: it is
the definition of record for what a result means, and
`species.preannotation.build_prediction` is its exact inverse.

v2 changes: reading a task is a pure function (`species_sync_from_task`)
rather than v1's fetch-mutate-PUT, and the functions are public (they were
the activity module's privates).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Hashable, TypeVar

from fishsense_services_api.label_sync_store import SpeciesSync
from fishsense_services_contracts import taxonomy
from fishsense_services_orchestrator.labels.label_studio import LabelStudioTask

__all__ = [
    "TOP_THREE_CHOICE",
    "calibration_target_choice",
    "content_of_image",
    "parse_results",
    "reduce_winners",
    "slate_not_in_list",
    "slate_type_choice",
    "species_sync_from_task",
]

#: The `exclude` control's affirmative choice (the labeling XML's).
TOP_THREE_CHOICE = "Top 3 photos of group"

Dive = TypeVar("Dive", bound=Hashable)
Target = TypeVar("Target")


def _first_choice(results: list[dict], from_name: str) -> str | None:
    """First `value.choices[0]` from results matching `from_name`, or None."""
    for r in results:
        if r["from_name"] == from_name:
            choices = r.get("value", {}).get("choices") or []
            if choices:
                return choices[0]
    return None


def _first_taxonomy_leaf(results: list[dict], from_name: str) -> str | None:
    """First `value.taxonomy[0][0]` from results matching `from_name`.

    The notebook uses `taxonomy[0][0]` for fish_measurable / fish_angle /
    fish_curved — the leaf of the first taxonomy path. Keep that shape.
    """
    for r in results:
        if r["from_name"] == from_name:
            paths = r.get("value", {}).get("taxonomy") or []
            if paths and paths[0]:
                return paths[0][0]
    return None


def content_of_image(results: list[dict]) -> str | None:
    """Join the species taxonomy path with ", ".

    Notebook: `", ".join(taxonomy[0])`. Stage 14 reads this back to derive
    (common, scientific) from the trailing element — keep the join character
    fixed.

    **`taxonomy[0]` is not good enough on the slate branch, and this was a
    live bug.** `Slate` holds two orthogonal answers as sibling paths — the
    content answer (`Laser on slate` / `Laser not on slate`, which stage 9's
    cohort keys on) and the slate type (`H-Slate`, ..., which
    `slate_type_choice` maps to the dive's slate template) — and a labeler
    picks one of each. Label Studio returns them in *selection order*, so
    taking path 0 made stage-9 eligibility depend on which choice was clicked
    first. In prod that cost 34 rows across 6 dives their laser answer.

    So a content answer is preferred when one is present. Everything else
    falls back to path 0 unchanged.
    """
    for r in results:
        if r["from_name"] == "species":
            paths = r.get("value", {}).get("taxonomy") or []
            if not paths:
                continue
            joined = [", ".join(path) for path in paths if path]
            for candidate in joined:
                if candidate in taxonomy.SLATE_LASER_CONTENT:
                    return candidate
            if joined:
                return joined[0]
    return None


def parse_results(annotation: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the species annotation fields out of an LS task result list."""
    results = annotation.get("result") or []

    grouping = _first_choice(results, "grouping")

    exclude_choice = _first_choice(results, "exclude")
    top_three_photos_of_group = (
        exclude_choice == TOP_THREE_CHOICE if exclude_choice is not None else None
    )

    return {
        "grouping": grouping,
        "top_three_photos_of_group": top_three_photos_of_group,
        "content_of_image": content_of_image(results),
        "fish_measurable_category": _first_taxonomy_leaf(results, "measurable"),
        "fish_angle_category": _first_taxonomy_leaf(results, "fishAngles"),
        "fish_curved_category": _first_taxonomy_leaf(results, "fishCurve"),
    }


def species_sync_from_task(task: LabelStudioTask) -> SpeciesSync:
    """What one task says about its species label. With no annotation every
    parsed field is None, and the store keeps what it has (v1 parsed only
    when the task had annotations)."""
    parsed = (
        parse_results(task.annotations[0])
        if task.annotations
        else dict.fromkeys(parse_results({}), None)
    )
    return SpeciesSync(
        completed=task.is_labeled,
        ls_labeler_id=task.annotator_id,
        ls_updated_at=task.updated_at,
        ls_payload=task.payload,
        **parsed,
    )


def slate_type_choice(results: list[dict], valid_slate_names: set[str]) -> str | None:
    """The slate-template name a labeler picked, or None.

    The species Taxonomy carries the slate type (H-Slate / V-Slate N /
    Tic-Tac-Toe N) as its own leaf under `Slate`, alongside `Laser on slate`.
    Here we scan *all* taxonomy paths and return the leaf that matches a real
    slate template name — the "which slate" answer.
    """
    for r in results:
        if r.get("from_name") == "species":
            for path in r.get("value", {}).get("taxonomy") or []:
                if not path:
                    continue
                leaf = path[-1]
                # "Slate not in list" is the labeler saying they cannot
                # identify it. Guarded explicitly rather than relying on it
                # being absent from `valid_slate_names`: seeding a template
                # row with that literal name would otherwise turn "I don't
                # know" into a confident wrong calibration, and a wrong slate
                # is a wrong *scale*, which reprojection residual cannot see.
                if leaf == taxonomy.SLATE_NOT_IN_LIST_LEAF:
                    continue
                if leaf in valid_slate_names:
                    return leaf
    return None


def calibration_target_choice(
    results: list[dict], valid_target_names: set[str]
) -> str | None:
    """The calibration-target name a labeler picked, or None.

    The planar-target counterpart of `slate_type_choice`. Two guards, both in
    `taxonomy.calibration_target_leaf`: the whole path is matched (not the
    bare leaf), and the ruler is refused by name -- it is the *validation*
    set, and it appears in ordinary fish dives. A leaf naming no target
    resolves to nothing rather than guessing.
    """
    for r in results:
        if r.get("from_name") == "species":
            for path in r.get("value", {}).get("taxonomy") or []:
                leaf = taxonomy.calibration_target_leaf(path)
                if leaf is not None and leaf in valid_target_names:
                    return leaf
    return None


def slate_not_in_list(results: list[dict]) -> bool:
    """Did the labeler explicitly say the slate isn't one of the templates?

    Distinct from `slate_type_choice(...) is None`, which also covers "no
    slate in this frame" and "labeler didn't answer". Only the explicit
    sentinel is evidence about the dive.
    """
    for r in results:
        if r.get("from_name") == "species":
            for path in r.get("value", {}).get("taxonomy") or []:
                if path and path[-1] == taxonomy.SLATE_NOT_IN_LIST_LEAF:
                    return True
    return False


def reduce_winners(
    votes: list[tuple[Dive, datetime | None, Target]],
) -> dict[Dive, Target]:
    """Collapse per-image votes to one winner per dive.

    `votes` is `(dive, updated_at, choice)`. Most-recent completed annotation
    wins (a re-label with a newer timestamp overrides an older one); a vote
    with no timestamp never displaces one that has a timestamp.
    """
    best: dict = {}
    for dive_id, ts, choice in votes:
        current = best.get(dive_id)
        if current is None or (
            ts is not None and (current[0] is None or ts > current[0])
        ):
            best[dive_id] = (ts, choice)
    return {dive_id: choice for dive_id, (_, choice) in best.items()}
