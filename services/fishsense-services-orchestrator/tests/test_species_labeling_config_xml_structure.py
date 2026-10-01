"""Structural assertions for the species LS labeling-config XML.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-
worker/tests/test_species_labeling_config_xml_structure.py (v2: the XML lives
in `species.labeling`).

The XML is just a string in
`create_species_label_studio_project_activity.SPECIES_LABELING_CONFIG_XML`,
so a typo (renamed control, dropped Choice value, malformed tag) won't
trip any other test — but it CAN silently break downstream:

  * Stage 9 selects `dive_slate_label` cohort on
    `SpeciesLabel.content_of_image == 'Slate, Laser on slate'`. If the
    species taxonomy loses the `Slate / Laser on slate` leaf, stage 9
    quietly returns no dives forever.
  * Stage 6.1 (`update_dive_image_groups_activity`) walks
    `SpeciesLabel.grouping`. A renamed `name="grouping"` Choices
    block stops grouping populating new clusters.
  * The sync activity's `_first_choice(results, "exclude")` /
    `_first_taxonomy_leaf(results, "fishAngles")` calls hard-code
    these `from_name` values; renaming any control silently zeroes
    out the corresponding column.

We assert XML well-formedness + each load-bearing element by name
and key Choice values. New choices added under existing branches are
fine; only structural changes that would break consumers fail here.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from fishsense_services_orchestrator.species.labeling import (
    SPECIES_LABELING_CONFIG_XML,
)


@pytest.fixture(scope="module")
def root() -> ET.Element:
    return ET.fromstring(SPECIES_LABELING_CONFIG_XML)


def test_xml_is_well_formed(root):
    """ET.fromstring would have raised on a malformed string; reaching
    this fixture is the assertion."""
    assert root.tag == "View"


def test_image_control_is_present_with_expected_name(root):
    """LS task data references `$image`; renaming this control breaks
    every populate (LS rejects task imports referencing an unknown
    name)."""
    images = root.findall(".//Image[@name='image']")
    assert len(images) == 1


def test_grouping_choices_present_with_both_values(root):
    """Stage 6.1 reads `grouping` to materialize LABEL_STUDIO clusters
    from labeler "Part of previous group" / "Not part of current
    group" choices. Both values are load-bearing."""
    grouping = root.find(".//Choices[@name='grouping']")
    assert grouping is not None
    values = {c.get("value") for c in grouping.findall("Choice")}
    assert values == {"Part of previous group", "Not part of current group"}


def test_exclude_top_three_choice_present(root):
    """The sync activity reads `exclude == 'Top 3 photos of group'`
    to populate `top_three_photos_of_group`. This was historically
    used by stage 5.1 head/tail (now driven by valid lasers instead),
    but the column is still surfaced for downstream consumers."""
    exclude = root.find(".//Choices[@name='exclude']")
    assert exclude is not None
    values = {c.get("value") for c in exclude.findall("Choice")}
    assert values == {"Top 3 photos of group"}


def test_species_taxonomy_present_with_slate_laser_on_slate_leaf(root):
    """Stage 9's slate cohort selector keys on
    `SpeciesLabel.content_of_image == 'Slate, Laser on slate'` —
    formed by joining the species taxonomy path with ", ". Without
    the `Slate / Laser on slate` leaf the slate pipeline silently
    drains."""
    taxonomy = root.find(".//Taxonomy[@name='species']")
    assert taxonomy is not None

    slate_branch = None
    for child in taxonomy.findall("Choice"):
        if child.get("value") == "Slate":
            slate_branch = child
            break
    assert slate_branch is not None, "Slate top-level Choice missing"

    slate_leaf_values = {c.get("value") for c in slate_branch.findall("Choice")}
    assert "Laser on slate" in slate_leaf_values, (
        "stage 9 slate cohort keys on 'Slate, Laser on slate' — "
        "the leaf must remain reachable under the Slate branch"
    )


def test_species_taxonomy_branches_named_correctly(root):
    """Top-level taxonomy branches under `name="species"`. Sync joins
    the path with ", " so renaming a branch silently changes
    every produced `content_of_image` string."""
    taxonomy = root.find(".//Taxonomy[@name='species']")
    top_level = {c.get("value") for c in taxonomy.findall("Choice")}
    # Three branches plus the "None" sentinel are load-bearing for
    # downstream string matching:
    #   - "Slate"  (stage 9)
    #   - "Fish"   (consumed by stage 14 measurement / dashboards)
    #   - "Fish Model" (post 2026-05-05; non-target detector)
    #   - "None"   (labeler-friendly "skip")
    assert {"None", "Slate", "Fish", "Fish Model"} <= top_level


def test_attribute_taxonomies_present_with_expected_names(root):
    """The three fish-attribute taxonomies feed
    `fish_measurable_category`, `fish_angle_category`,
    `fish_curved_category`. The sync activity reads them by literal
    `from_name`, so renaming silently drops the column."""
    attribute_names = {t.get("name") for t in root.findall(".//Taxonomy")} - {"species"}
    assert attribute_names == {"measurable", "fishAngles", "fishCurve"}


def test_no_keypoint_labels_remain(root):
    """Post 2026-05-05 the laser KeyPointLabels are gone from the
    species XML — laser keypoints are labeled in the dedicated stage
    0.1 LS project. If a future XML edit re-introduces a
    `<KeyPointLabels>` here, the species sync activity's stripped
    laser-extraction path would need to come back; pin its absence."""
    assert not root.findall(".//KeyPointLabels")


def test_no_slate_upside_down_choice(root):
    """Post 2026-05-05 the `slate` Choices block ("Slate upside down")
    is gone. The sync activity's `slate_upside_down` extraction was
    stripped accordingly. Pin the absence."""
    slate_choices = root.findall(".//Choices[@name='slate']")
    assert not slate_choices


def test_slate_branch_offers_the_not_in_list_sentinel(root):
    """A labeler must be able to say "that slate isn't on your list".

    The slate-type leaves are exactly the `DiveSlate` template rows, and a
    slate can be missing from that table permanently — V-Slate 7 was lost
    during a dive, so it can never be scanned and never added. Without this
    leaf the only options are a wrong neighbour or silence, and the wrong
    neighbour is the harmful one: a wrong slate template gives a wrong
    *scale*, which reprojection residual cannot detect.
    """
    from fishsense_services_contracts import taxonomy

    slate_branch = None
    for child in root.findall(".//Taxonomy[@name='species']/Choice"):
        if child.get("value") == "Slate":
            slate_branch = child
    assert slate_branch is not None, "Slate top-level Choice missing"

    leaves = {c.get("value") for c in slate_branch.findall("Choice")}
    assert taxonomy.SLATE_NOT_IN_LIST_LEAF in leaves, (
        "the species taxonomy must offer "
        f"{taxonomy.SLATE_NOT_IN_LIST_LEAF!r} so an unidentifiable slate "
        "has an honest answer"
    )
