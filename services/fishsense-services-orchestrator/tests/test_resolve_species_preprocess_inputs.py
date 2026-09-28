"""The stage-2 resolver: which frames to draw, and where each sits.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_resolve_species_preprocess_inputs_activity.py (18),
test_species_cluster_positions.py (3), and the species cases of
test_reprocess_flag_drains.py and test_resolvers_honour_needs_reprocess.py.
Test names, bodies and reasons are v1's; v2 adaptations: the catalog hands the
resolver its facts in one read (v1: six SDK calls), images are capture uuids,
and a member names its frame by the refs the orchestrator issues, so
`_clusters` reads the checksums back out of those keys.

v1's rules, all kept: canonical frames only; a frame is eligible with a valid
laser and no live non-sentinel species row, or with a flagged live row; a
member's i/N is its position in the WHOLE prediction cluster; an eligible
frame in no cluster is its own 1 of 1, after the real clusters.

v2 changes, pinned last: the device's intrinsics stand in for v1's
`dive.camera_id`; each member carries the staged raw ref and the JPEG target
(over v1's JPEG for a migrated frame).
"""

from __future__ import annotations

import pytest
from temporalio.testing import ActivityEnvironment

from fishsense_services_contracts.species import PreprocessSpeciesImagesInput
from fishsense_services_orchestrator.species.activities import SpeciesActivities
from fishsense_services_orchestrator.species.contracts import SpeciesTarget

from ._species import (
    D,
    DIVE,
    K,
    LAYOUT,
    TENANT,
    FakeSpeciesCatalog,
    FakeStore,
    capture,
    checksum_of,
    facts,
    image as _image,
    species,
)

TARGET = SpeciesTarget(TENANT, DIVE)


def _clusters(result: PreprocessSpeciesImagesInput) -> list[list[str]]:
    """v1's `clusters`: the checksums per cluster, read back from the raw keys
    (reduced to the short names the tests spell them with)."""
    return [
        [m.raw.key.rsplit("/", 1)[-1][:3] for m in group]
        for group in result.cluster_members
    ]


def _species(n, *, completed=False, project=70):
    return species(n, completed=completed, project=project)


async def _resolve(preprocess, store=None) -> PreprocessSpeciesImagesInput:
    activities = SpeciesActivities(
        catalog=FakeSpeciesCatalog(preprocess=preprocess), store=store or FakeStore()
    )
    return await ActivityEnvironment().run(
        activities.resolve_species_preprocess_inputs, TARGET
    )


async def test_keeps_laser_valid_unlabeled_images_in_cluster_order():
    result = await _resolve(
        facts(
            images=[_image(1, "aaa"), _image(2, "bbb"), _image(3, "ccc")],
            clusters=[[1, 2], [3]],
            valid=[1, 2, 3],
        )
    )

    assert result.dive_id == DIVE
    assert _clusters(result) == [["aaa", "bbb"], ["ccc"]]
    assert result.camera_matrix == K
    assert result.distortion_coefficients == D


async def test_drops_images_without_valid_laser():
    """Image 2 has no valid laser (incomplete) → drop from cluster."""
    result = await _resolve(
        facts(
            images=[_image(1, "aaa"), _image(2, "bbb")],
            clusters=[[1, 2]],
            valid=[1],
        )
    )

    assert _clusters(result) == [["aaa"]]


async def test_drops_images_with_non_sentinel_species_label():
    """Image 1 has an incomplete species label in a real project → already
    populated, must not re-import. Image 2 has none → keep."""
    result = await _resolve(
        facts(
            images=[_image(1, "aaa"), _image(2, "bbb")],
            clusters=[[1, 2]],
            valid=[1, 2],
            labels=[_species(1, project=70)],
        )
    )

    assert _clusters(result) == [["bbb"]]


async def test_keeps_images_with_only_null_project_species_sentinels():
    """NULL-project species rows are legacy sentinels — must not drop
    the image (consistent with the API cohort selector)."""
    result = await _resolve(
        facts(
            images=[_image(1, "aaa")],
            clusters=[[1]],
            valid=[1],
            labels=[_species(1, project=None)],
        )
    )

    assert _clusters(result) == [["aaa"]]


async def test_drops_clusters_that_become_empty_after_filter():
    result = await _resolve(
        facts(
            images=[_image(1, "aaa"), _image(2, "bbb")],
            clusters=[[1], [2]],
            valid=[1],
        )
    )

    assert _clusters(result) == [["aaa"]]


async def test_drops_image_ids_that_no_longer_have_image_rows():
    result = await _resolve(
        facts(images=[_image(1, "aaa")], clusters=[[1, 99]], valid=[1, 99])
    )

    assert _clusters(result) == [["aaa"]]


async def test_raises_when_dive_not_found():
    with pytest.raises(ValueError, match="not found"):
        await _resolve(None)


async def test_raises_when_no_camera_id():
    """v1: the dive has no camera. v2: it names no device."""
    with pytest.raises(ValueError, match="no device"):
        await _resolve(facts(device=None))


async def test_raises_when_no_intrinsics():
    with pytest.raises(ValueError, match="no intrinsics"):
        await _resolve(facts(intrinsics=None))


# ---------- selector ↔ resolver consistency ----------
#
# The selector decides "does this dive need work?", the resolver "what work
# for this dive?". If they disagree, the parent logs "0 images" and exits
# silently. Each case mirrors a row of the selector's truth table
# (fishsense_services_api tests/test_species_store.py).


async def test_selector_picks_dive__resolver_emits_at_least_one_image():
    result = await _resolve(
        facts(images=[_image(31, "ccc")], clusters=[[31]], valid=[31])
    )

    assert _clusters(result) == [["ccc"]]


async def test_selector_skips_dive__resolver_returns_empty():
    result = await _resolve(
        facts(
            images=[_image(11, "aaa"), _image(12, "bbb")],
            clusters=[[11, 12]],
            valid=[11, 12],
            labels=[_species(11, project=70), _species(12, project=70)],
        )
    )

    assert not result.cluster_members


async def test_selector_picks_dive__resolver_only_emits_unlabeled_subset():
    result = await _resolve(
        facts(
            images=[_image(11, "aaa"), _image(12, "bbb")],
            clusters=[[11, 12]],
            valid=[11, 12],
            labels=[
                _species(11, project=None),  # sentinel — keep in cohort
                _species(12, project=70),  # real row — drop from cohort
            ],
        )
    )

    assert _clusters(result) == [["aaa"]]


# --- orphan frames (no PREDICTION cluster) ---------------------------------
#
# Stage 1 clustering is ONE-SHOT per dive, so a laser-valid frame it missed
# can never be clustered later. Reaching images only through clusters left
# such a frame without a stage-2 JPEG forever, and populate publishes a
# project only when nothing is deferred: one orphan kept the whole project a
# draft (prod 2026-08-28: dive 442 had 6 orphans holding back 352 tasks).


async def test_emits_orphan_laser_valid_image_as_its_own_cluster():
    """Image 3 is laser-valid and unlabeled but in no cluster."""
    result = await _resolve(
        facts(
            images=[_image(1, "aaa"), _image(2, "bbb"), _image(3, "ccc")],
            clusters=[[1, 2]],
            valid=[1, 2, 3],
        )
    )

    assert _clusters(result) == [["aaa", "bbb"], ["ccc"]]


async def test_orphans_are_appended_after_real_clusters():
    """Real clusters keep their order and position; orphans follow."""
    result = await _resolve(
        facts(
            images=[
                _image(1, "aaa"),
                _image(2, "bbb"),
                _image(3, "ccc"),
                _image(4, "ddd"),
            ],
            clusters=[[1], [2]],
            valid=[1, 2, 3, 4],
        )
    )

    clusters = _clusters(result)
    assert clusters[:2] == [["aaa"], ["bbb"]]
    # Each orphan is its own singleton -- never merged into one fake cluster,
    # which would render "image 1 of 2" for unrelated frames.
    assert sorted(clusters[2:]) == [["ccc"], ["ddd"]]
    assert all(
        (m.cluster_index, m.cluster_size) == (1, 1)
        for group in result.cluster_members[2:]
        for m in group
    )


async def test_orphans_respect_the_laser_valid_gate():
    result = await _resolve(
        facts(images=[_image(1, "aaa"), _image(2, "bbb")], clusters=[[1]], valid=[1])
    )

    assert _clusters(result) == [["aaa"]]


async def test_orphans_respect_the_already_labeled_gate():
    result = await _resolve(
        facts(
            images=[_image(1, "aaa"), _image(2, "bbb")],
            clusters=[[1]],
            valid=[1, 2],
            labels=[_species(2, project=70)],
        )
    )

    assert _clusters(result) == [["aaa"]]


async def test_orphans_respect_the_canonical_gate():
    """A non-canonical unclustered image is dropped: the catalog hands the
    resolver canonical captures only, so a duplicate never reaches it --
    whatever its lasers say."""
    result = await _resolve(
        facts(images=[_image(1, "aaa")], clusters=[[1]], valid=[1, 2])
    )

    assert _clusters(result) == [["aaa"]]


async def test_no_orphans_leaves_output_unchanged():
    result = await _resolve(
        facts(
            images=[_image(1, "aaa"), _image(2, "bbb")],
            clusters=[[1, 2]],
            valid=[1, 2],
        )
    )

    assert _clusters(result) == [["aaa", "bbb"]]


# --- positions (test_species_cluster_positions.py) -----------------------------
#
# "image i of N" is a property of the cluster, not of whichever subset is
# drawn: redraw 3 of a 7-image cluster as 1/3..3/3 and their siblings still
# read 4/7..7/7 at the same keys Label Studio presigns.

_IDS = (1, 2, 3)
_NAMES = {1: "aaa", 2: "bbb", 3: "ccc"}


def _flagged_facts(flagged):
    """Three frames in one cluster, each with a real completed row, so only
    the flag can reach them."""
    return facts(
        images=[_image(i, _NAMES[i]) for i in _IDS],
        clusters=[list(_IDS)],
        valid=list(_IDS),
        labels=[
            species(i, project=1, completed=True, needs_reprocess=i in flagged)
            for i in _IDS
        ],
    )


async def test_middle_image_alone_keeps_its_true_position():
    result = await _resolve(_flagged_facts({2}))

    members = [m for group in result.cluster_members for m in group]
    assert [m.capture_id for m in members] == [capture(2)]
    assert members[0].cluster_index == 2, "second of three, not first of one"
    assert members[0].cluster_size == 3


@pytest.mark.parametrize("flagged", [{1}, {2}, {3}, {1, 3}, {1, 2, 3}])
async def test_every_subset_agrees_with_the_full_cluster(flagged):
    """Whatever is redrawn, each frame reports the same i/N it would have had
    in a full pass. That is the invariant the siblings' JPEGs encode."""
    result = await _resolve(_flagged_facts(flagged))

    members = [m for group in result.cluster_members for m in group]
    assert {m.capture_id for m in members} == {capture(i) for i in flagged}
    for member in members:
        assert member.cluster_index == member.capture_id.int, "1-based position"
        assert member.cluster_size == 3


# --- the reprocess flag reaches its image (flag drains / resolvers honour) --------


async def test_flagged_orphan_is_resolved():
    """No prediction cluster contains it, so only the orphan branch can reach
    it -- and that branch ignored the flag entirely."""
    result = await _resolve(
        facts(
            images=[_image(1, "bbb")],
            valid=[1],
            labels=[species(1, project=1, completed=True, needs_reprocess=True)],
        )
    )

    assert _clusters(result) == [["bbb"]]


async def test_flagged_image_is_returned_though_it_is_already_labelled():
    result = await _resolve(
        facts(
            images=[_image(1, "aaa")],
            clusters=[[1]],
            valid=[1],
            labels=[species(1, project=1, completed=True, needs_reprocess=True)],
        )
    )

    assert _clusters(result) == [["aaa"]]


async def test_unflagged_labelled_image_is_still_excluded():
    result = await _resolve(
        facts(
            images=[_image(1, "aaa")],
            clusters=[[1]],
            valid=[1],
            labels=[species(1, project=1, completed=True)],
        )
    )

    assert result.cluster_members == []


async def test_a_flagged_image_needs_no_valid_laser():
    """The cohort's flag branch has no laser gate, so the resolver's must not
    either -- or a laser superseded after flagging wedges the dive."""
    result = await _resolve(
        facts(
            images=[_image(1, "aaa")],
            clusters=[[1]],
            valid=[],
            labels=[species(1, project=1, completed=True, needs_reprocess=True)],
        )
    )

    assert _clusters(result) == [["aaa"]]


# --- v2: the refs the orchestrator issues --------------------------------------------


async def test_each_member_names_its_staged_frame_and_its_jpeg_target():
    result = await _resolve(facts(images=[_image(1, "aaa")], clusters=[[1]], valid=[1]))

    (member,) = result.cluster_members[0]
    assert member.capture_id == capture(1)
    assert member.raw == LAYOUT.raw(TENANT, checksum_of("aaa"))
    assert member.jpeg == LAYOUT.processed_jpeg(
        TENANT, "preprocess_groups_jpeg", checksum_of("aaa")
    )


async def test_a_migrated_frame_is_redrawn_over_v1s_jpeg():
    """A migrated frame's JPEG stays where v1 wrote it -- Label Studio tasks
    and label image_urls hold that URL -- so its redraw overwrites it there."""
    result = await _resolve(
        facts(images=[_image(1, "aaa", from_v1=True)], clusters=[[1]], valid=[1])
    )

    (member,) = result.cluster_members[0]
    assert member.jpeg == LAYOUT.legacy_processed_jpeg(
        "preprocess_groups_jpeg", checksum_of("aaa")
    )
