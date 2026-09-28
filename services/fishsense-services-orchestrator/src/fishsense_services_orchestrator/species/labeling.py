"""The species Label Studio project: its title suffix and labeling config.

Ported verbatim from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-
worker/src/fishsense_api_workflow_worker/activities/
create_species_label_studio_project_activity.py (`SPECIES_PROJECT_TITLE_SUFFIX`,
`SPECIES_LABELING_CONFIG_XML`). The XML is load-bearing and pinned by
test_species_labeling_config_xml_structure.py and test_species_xml_model_parity.py;
every existing per-dive project is healed onto it on its next create
(`labels.populate.LabelProjects`), as v1's were. The labeling-config reconcile
(the ops slice) maps the suffix to this XML.
"""

__all__ = ["SPECIES_LABELING_CONFIG_XML", "SPECIES_PROJECT_TITLE_SUFFIX"]

SPECIES_PROJECT_TITLE_SUFFIX = "Species Labeling"

# Labeling-config XML supplied by the user 2026-05-05. Several control
# names are load-bearing for downstream code:
#   * `name="grouping"` choices — stage 6.1
#     (`update_dive_image_groups_activity`) walks PREDICTION clusters
#     using these labels to materialize LABEL_STUDIO clusters.
#   * `name="exclude"` with `Top 3 photos of group` — historically used
#     by stage 5.1 head/tail. The 2026-05-04 cascade flip moved
#     head/tail off this gate (it now cascades from valid lasers), so
#     this control is informational rather than load-bearing on the
#     species side, but kept in the XML to preserve labeler workflow.
#   * `name="species"` taxonomy — its top-level `Slate` branch with
#     the `Laser on slate` leaf is what stage 9 keys on
#     (`SLATE_CONTENT_MARKER = "Slate, Laser on slate"` in
#     `populate_dive_slate_label_studio_project_activity.py`).
# Renaming any of these breaks the downstream populate / sync chain
# silently (the activity returns no rows rather than erroring).
#
# Schema diffs vs the prior XML:
#   - `<KeyPointLabels name="laser">` removed: lasers are labeled in
#     their own dedicated project (stage 0.1).
#   - `<Choices name="slate">` ("Slate upside down") removed.
#   - Slate branch expanded with H-Slate, Tic-Tac-Toe 1..6, V-Slate 1..4.
#   - Fish Model branch added; its choices were replaced wholesale on
#     2026-07-21 with the current model set (Weasly Fish, Snook, Grouper,
#     Shark, Gray Anthias, Purple Angel, Yellow Anthias).
#   - `Calibration Targets` top-level branch added 2026-07-21 (Ruler,
#     E4E Checkerboard); `Box` added 2026-09-07 (0.15 m). The branch is
#     MIXED — Ruler and Box are rigid known-length targets and grade through
#     the same `Fish.name` join as the fish models, while E4E Checkerboard
#     spans no single head/tail distance and is deliberately unmeasurable.
#     `taxonomy.MEASURABLE_CALIBRATION_TARGETS` is the allowlist that draws
#     that line; adding a leaf here without adding it there (and to
#     `views.KNOWN_FISH_MODELS`) is caught by the parity tests, not by
#     anything at runtime. Kept as its own sibling of `Fish Model` rather
#     than folded into it because they aren't fish — `content_of_image`
#     records the taxonomy path, so these read as
#     "Calibration Targets, Ruler" and stay trivially separable from the
#     fish-model rows downstream. Editing this constant is enough —
#     `create_or_get_label_studio_project` pushes a changed config onto
#     already-created projects, so existing per-dive projects converge on
#     the next populate run rather than keeping the config they were born
#     with.
SPECIES_LABELING_CONFIG_XML = """\
<View>
  <Choices name="grouping" toName="image">
    <Choice value="Part of previous group" />
    <Choice value="Not part of current group" />
  </Choices>

  <Choices name="exclude" toName="image">
    <Choice value="Top 3 photos of group" />
  </Choices>

  <Image name="image" value="$image"/>

  <Header value="Please select the content of the image" />
  <Taxonomy name="species" toName="image" leafsOnly="true">
    <Choice value="None"/>
    <Choice value="Slate">
      <Choice value="Laser on slate"/>
      <Choice value="Laser not on slate"/>

      <Choice value="H-Slate"/>

      <Choice value="Tic-Tac-Toe 1"/>
      <Choice value="Tic-Tac-Toe 2"/>
      <Choice value="Tic-Tac-Toe 3"/>
      <Choice value="Tic-Tac-Toe 4"/>
      <Choice value="Tic-Tac-Toe 5"/>
      <Choice value="Tic-Tac-Toe 6"/>

      <Choice value="V-Slate 1"/>
      <Choice value="V-Slate 2"/>
      <Choice value="V-Slate 3"/>
      <Choice value="V-Slate 4"/>

      <Choice value="Slate not in list"/>
    </Choice>
    <Choice value="Fish">
      <Choice value="Hogfish (Lachnolaimus maximus)"/>
      <Choice value="Black Grouper (Mycteroperca bonaci)"/>
      <Choice value="Goliath Grouper (Epinephelus itajara)"/>
      <Choice value="Nassau Grouper (Epinephelus striatus)"/>
      <Choice value="Red Grouper (Epinephelus morio)"/>
      <Choice value="Yellowtail Snapper (Ocyurus chrysurus)"/>
      <Choice value="Grey Snapper (Lutjanus griseus)"/>
      <Choice value="Mutton Snapper (Lutjanus analis)"/>
      <Choice value="Blue Parrotfish (Scarus coeruleus)"/>
      <Choice value="Midnight Parrotfish (Scarus coelestinus)"/>
      <Choice value="Rainbow Parrotfish (Scarus guacamaia)"/>
      <Choice value="Stoplight Parrotfish (Sparisoma viride)"/>
      <Choice value="Yellowmouth Grouper (Mycteroperca interstitialis)"/>
      <Choice value="Unidentifiable (Cannot see)"/>
      <Choice value="Other (Identifiable but Nontarget)"/>
    </Choice>
    <Choice value="Fish Model">
      <Choice value="Weasly Fish"/>
      <Choice value="Snook"/>
      <Choice value="Grouper"/>
      <Choice value="Shark"/>
      <Choice value="Gray Anthias"/>
      <Choice value="Purple Angel"/>
      <Choice value="Yellow Anthias"/>
    </Choice>
    <Choice value="Calibration Targets">
      <Choice value="Ruler"/>
      <Choice value="Box"/>
      <Choice value="E4E Checkerboard"/>
    </Choice>
  </Taxonomy>

  <Header value="Is the fish measurable?"/>
  <Taxonomy name="measurable" toName="image">
    <Choice value="yes, center of fish" />
    <Choice value="yes, not center of fish" />
    <Choice value="no" />
  </Taxonomy>

  <Header value="How angled is the fish?"/>
  <Taxonomy name="fishAngles" toName="image">
    <Choice value="x &lt; 5°" />
    <Choice value="5° &lt; x &lt; 10°" />
    <Choice value="10° &lt; x &lt; 15°" />
    <Choice value="x &gt; 15°" />
  </Taxonomy>

  <Header value="How curved is the fish?"/>
  <Taxonomy name="fishCurve" toName="image">
    <Choice value="No Curve" />
    <Choice value="Slight Curve" />
    <Choice value="Significant Curve" />
  </Taxonomy>
</View>
"""
