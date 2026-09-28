"""The species XML and `taxonomy.LABELED_FISH_MODELS` must not drift.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-
worker/tests/test_species_xml_model_parity.py (v2: the XML lives in
`species.labeling`, the list in `fishsense_services_contracts.taxonomy`).

`LABELED_FISH_MODELS` is what the API grades against — it decides which models
need a `fishmodelreference` row. The species labeling XML is what a labeler can
actually pick. If the two disagree, the disagreement is **silent in both
directions**:

  * a model in the XML but not the list gets no reference row, and the accuracy
    view inner-joins it away — its measurements simply never appear (this is
    exactly what `Weasly Fish` did);
  * a model in the list but not the XML demands a reference row nobody can ever
    produce a measurement for.

The XML lives in the api-worker and the grading lives in the API, which is why
the shared list exists at all and why this parity test does.
"""

from __future__ import annotations

import re

from fishsense_services_contracts import taxonomy

from fishsense_services_orchestrator.species.labeling import (
    SPECIES_LABELING_CONFIG_XML,
)


def _xml_fish_models() -> list[str]:
    """Names nested under the `Fish Model` parent choice, in XML order.

    Parsed from the real pasted-from-prod XML rather than a copy, so the test
    fails when someone edits the config — which is the moment it matters.
    """
    block = re.search(
        r'<Choice value="Fish Model">(.*?)</Choice>\s*<Choice value="Calibration Targets">',
        SPECIES_LABELING_CONFIG_XML,
        re.S,
    )
    assert block, "Fish Model branch not found — did the XML change shape?"
    return re.findall(r'<Choice value="([^"]+)"\s*/>', block.group(1))


def test_the_parser_actually_finds_the_models():
    """Guards the regex itself: a parse that silently returned [] would make
    every assertion below vacuously true."""
    assert len(_xml_fish_models()) >= 6


def test_every_xml_fish_model_is_in_the_shared_list():
    missing = set(_xml_fish_models()) - set(taxonomy.LABELED_FISH_MODELS)

    assert not missing, f"in the species XML but ungradeable: {sorted(missing)}"


def test_the_shared_list_invents_no_models():
    extra = set(taxonomy.LABELED_FISH_MODELS) - set(_xml_fish_models())

    assert not extra, f"listed but not labelable: {sorted(extra)}"


def test_weasly_fish_is_labelable():
    """The specific row this work exists for — it was pickable in prod with no
    reference row, so its measurements were graded by nobody."""
    assert "Weasly Fish" in _xml_fish_models()


def _xml_calibration_targets() -> list[str]:
    """Leaves nested under the `Calibration Targets` parent choice, in XML
    order."""
    block = re.search(
        r'<Choice value="Calibration Targets">(.*?)</Choice>\s*</Taxonomy>',
        SPECIES_LABELING_CONFIG_XML,
        re.S,
    )
    assert block, "Calibration Targets branch not found — did the XML change shape?"
    return re.findall(r'<Choice value="([^"]+)"\s*/>', block.group(1))


def test_the_calibration_target_parser_actually_finds_the_leaves():
    """Guards the regex, same as `_xml_fish_models`'s: a silent [] would make
    the assertion below vacuous."""
    assert len(_xml_calibration_targets()) >= 2


def test_every_measurable_calibration_target_is_labelable():
    """`MEASURABLE_CALIBRATION_TARGETS` demands a reference row for each name;
    a name nobody can pick demands one no measurement will ever use.

    The mirror of `test_the_shared_list_invents_no_models`, for the branch that
    holds the ruler and the box.
    """
    labelable = set(_xml_calibration_targets())
    named = set(taxonomy.MEASURABLE_CALIBRATION_TARGETS.values())

    assert (
        named <= labelable
    ), f"measurable but not pickable: {sorted(named - labelable)}"


def test_the_box_is_labelable():
    """The row this change exists for."""
    assert "Box" in _xml_calibration_targets()


def _xml_calibration_target_paths() -> list[str]:
    """The full `", "`-joined taxonomy paths the XML can emit for this branch.

    Label Studio writes the whole path into `content_of_image`, so the PARENT
    choice's spelling is load-bearing and not just decoration.
    """
    parent = re.search(
        r'<Choice value="(Calibration Targets)">',
        SPECIES_LABELING_CONFIG_XML,
    )
    assert parent, "Calibration Targets parent choice not found"
    return [f"{parent.group(1)}, {leaf}" for leaf in _xml_calibration_targets()]


def test_every_measurable_calibration_target_path_is_emittable():
    """The dict's KEYS are what has to match, not its values.

    `MEASURABLE_CALIBRATION_TARGETS` maps a full path to a `Fish.name`, and it
    is the path that `parse_model_name`, `rigid_target_sql` and the cohort
    condition all compare against `content_of_image`. The sibling test above
    checks only the values — the leaf names — so renaming the PARENT choice
    (say to `Calibration Target`) would leave every test in this file green
    while silently unmeasuring every box and ruler frame: LS would emit a path
    no predicate matches, `is_measurable` would go false, and the frames would
    just stop being offered to stage 14 with nothing raised anywhere.
    """
    emittable = set(_xml_calibration_target_paths())
    declared = set(taxonomy.MEASURABLE_CALIBRATION_TARGETS)

    assert declared <= emittable, (
        f"declared measurable but the XML cannot emit it: "
        f"{sorted(declared - emittable)}"
    )


def test_the_path_parity_check_would_notice_a_renamed_parent():
    """Guards the test above against passing vacuously.

    If `_xml_calibration_target_paths` ever returned paths built from a
    hardcoded parent rather than the XML's own, the check would compare the
    constant against itself.
    """
    assert all(
        path.startswith("Calibration Targets, ")
        for path in _xml_calibration_target_paths()
    )
    assert "Calibration Targets, Box" in _xml_calibration_target_paths()
