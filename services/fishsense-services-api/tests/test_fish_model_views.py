"""v1's three fish views, v1-shaped, read as a research login.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api/tests/
test_fish_model_reference.py (the accuracy-view tests),
test_fish_length_estimate_view.py and test_fish_model_mislabel_suspects.py.
v1 ran them on SQLite over its ORM; here they run on Postgres, seeded as the
schema owner and read through RLS by a member of `fishsense_research`, so every
test also proves the role can read the view and everything under it.

The seed-data tests of test_fish_model_reference.py (`KNOWN_FISH_MODELS`, the
Box/Ruler/Weasly notes) are not ported: v2 takes the live reference rows through
migrate-v1 instead of seeding constants (PLAN.md §6.4).

v2 changes, each pinned below:

* **ids are numbers** -- `measurement_id`, `image_id`, `dive_id` and `fish_id`
  are the rows' `number`s, which are v1's ids for migrated rows (docs/
  port-plan.md, "Integer ids");
* **one reference per model** -- references are versioned (`valid_from`), and
  both the accuracy join and the mislabel cross join go through the current
  version, so a corrected length neither doubles a frame nor lingers as a
  candidate;
* **v1's rows, not `current_measurements`** -- the views read v1's
  `measurement` (v1.measurement: a capture's latest server measurement), which
  keeps a measurement whose calibration has since changed, as v1's views did.
  Research compares these numbers against v1's (PLAN.md §9.13 governs the
  pipeline's "current"; these views reproduce v1's rows on migrated data).
"""

from __future__ import annotations

import pytest

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    calibrate,
    forget_identities,
    tenant,
)
from research_seed import Lab, references, rows  # noqa: F401  (references: a fixture)

# Known lengths used by the mislabel tests (mirrors prod; v1's `_KNOWN`).
_KNOWN = {
    "Snook": 0.455,
    "Grouper": 0.360,
    "Shark": 0.605,
    "Purple Angel": 0.192,
    "Gray Anthias": 0.195,
    "Yellow Anthias": 0.200,
}


@pytest.fixture
async def lab(owner_engine) -> Lab:
    return Lab(owner_engine, await tenant(owner_engine, "lab"))


# ── the accuracy view (test_fish_model_reference.py) ──────────────────


async def _accuracy(research_engine):
    return await rows(
        research_engine,
        "SELECT dive_id, model_name, known_length_m, length_m, error_m, pct_error "
        "FROM fish_model_measurement_accuracy ORDER BY image_id",
    )


async def test_accuracy_view_computes_error_against_known_length(
    lab, references, research_engine
):
    await references("Grouper", 0.360)
    # Measured 10% long.
    await lab.measure(dive=1, model="Grouper", length_m=0.396)

    got = await _accuracy(research_engine)

    assert len(got) == 1
    assert got[0]["model_name"] == "Grouper"
    assert got[0]["known_length_m"] == pytest.approx(0.360)
    assert got[0]["error_m"] == pytest.approx(0.036, abs=1e-6)
    assert got[0]["pct_error"] == pytest.approx(10.0, abs=1e-4)


async def test_accuracy_view_excludes_real_fish(
    lab, references, research_engine, owner_engine
):
    """Real (wild) fish carry no model name and have no reference row -- they
    must not appear, or the view stops being a model-accuracy view."""
    from depth_measure_seed import capture, fish, measurement

    await references("Grouper", 0.360)
    dive_id, calibration_id = await lab.dive(1)
    wild = await fish(owner_engine, lab.tenant_id)
    frame = await capture(owner_engine, lab.tenant_id, dive_id)
    await measurement(
        owner_engine, lab.tenant_id, frame, wild, calibration_id, length_m=0.5
    )

    assert await _accuracy(research_engine) == []


async def test_accuracy_view_excludes_models_without_a_reference(
    lab, references, research_engine
):
    """A model nobody has measured with calipers yet can't be graded -- it must
    be absent rather than silently compared against NULL."""
    await lab.measure(dive=1, model="Unmeasured Model", length_m=0.4)

    assert await _accuracy(research_engine) == []


async def test_accuracy_view_names_rows_by_their_numbers(
    lab, references, research_engine
):
    """v2: v1's integer ids are the rows' numbers, so research joins still work."""
    await references("Grouper", 0.360)
    capture_id, measurement_id = await lab.measure(
        dive=1, model="Grouper", length_m=0.396
    )
    dive_id, _ = await lab.dive(1)

    got = await rows(
        research_engine,
        "SELECT measurement_id, image_id, dive_id, fish_id "
        "FROM fish_model_measurement_accuracy",
    )

    assert got == [
        {
            "measurement_id": await lab.number("measurements", measurement_id),
            "image_id": await lab.number("captures", capture_id),
            "dive_id": await lab.number("dives", dive_id),
            "fish_id": await lab.number("fish", await lab.model_fish("Grouper")),
        }
    ]


async def test_accuracy_view_grades_against_the_current_reference_only(
    lab, references, research_engine
):
    """v2: a corrected length is a new reference version. The frame is graded
    once, against the correction -- not once per version."""
    await references("Weasly Fish", 0.310, valid_from="2026-08-04")
    await references("Weasly Fish", 0.313, valid_from="2026-09-12")
    await lab.measure(dive=1, model="Weasly Fish", length_m=0.313)

    got = await _accuracy(research_engine)

    assert [r["known_length_m"] for r in got] == [pytest.approx(0.313)]


async def test_accuracy_view_keeps_a_measurement_whose_calibration_changed(
    lab, references, research_engine, owner_engine
):
    """v2 decision: v1's rows, stale ones included. v1's view never filtered on
    calibration freshness, and research compares against it -- so a dive's
    recalibration does not remove its old measurements here, although
    `current_measurements` (the pipeline's "current", §9.13) no longer counts
    them."""
    await references("Grouper", 0.360)
    await lab.measure(dive=1, model="Grouper", length_m=0.396)
    dive_id, _ = await lab.dive(1)
    await calibrate(owner_engine, lab.tenant_id, dive_id, position=(0.11, 0, 0))

    assert [r["length_m"] for r in await _accuracy(research_engine)] == [0.396]
    assert await rows(owner_engine, "SELECT 1 FROM current_measurements") == []


async def test_accuracy_view_reads_a_captures_latest_measurement(
    lab, references, research_engine, owner_engine
):
    """v2: measurements are appended. v1 upserted one row per (image, fish) and
    deleted the image's other bindings when it re-measured
    (measure_fish_activity.py), so v1 held one row per image: the latest."""
    from depth_measure_seed import measurement

    await references("Grouper", 0.360)
    await references("Snook", 0.455)
    capture_id, _ = await lab.measure(dive=1, model="Grouper", length_m=0.30)
    _, calibration_id = await lab.dive(1)
    await measurement(
        owner_engine,
        lab.tenant_id,
        capture_id,
        await lab.model_fish("Snook"),
        calibration_id,
        length_m=0.45,
    )

    got = await _accuracy(research_engine)

    assert [(r["model_name"], r["length_m"]) for r in got] == [("Snook", 0.45)]


# ── the length estimate (test_fish_length_estimate_view.py) ───────────


async def _estimate(lab: Lab, research_engine, *, model: str, dive: int):
    dive_id, _ = await lab.dive(dive)
    got = await rows(
        research_engine,
        "SELECT * FROM fish_length_estimate WHERE fish_id = :f AND dive_id = :d",
        f=await lab.number("fish", await lab.model_fish(model)),
        d=await lab.number("dives", dive_id),
    )
    return got[0] if got else None


async def _frames(lab: Lab, lengths, *, dive: int = 1, model: str = "model1"):
    for length in lengths:
        await lab.measure(dive=dive, model=model, length_m=length)


async def test_p90_rejects_the_one_sided_foreshortening_tail(lab, research_engine):
    """THE motivating case. Nine good frames plus one badly foreshortened one:
    the mean is dragged down by the tail, p90 is not."""
    await _frames(lab, [1.00] * 9 + [0.50])

    row = await _estimate(lab, research_engine, model="model1", dive=1)
    assert row["n_frames"] == 10
    assert row["length_p90_m"] == pytest.approx(1.00)
    assert row["length_mean_m"] == pytest.approx(0.95)
    assert row["length_min_m"] == pytest.approx(0.50), "the bad frame is still there"


async def test_p90_is_not_simply_the_max_when_there_are_enough_frames(
    lab, research_engine
):
    """p90 must reject the top frame's label noise too, or it is just `max`
    under another name. 20 frames -> rank ceil(0.9*20) = 18, not 20."""
    await _frames(lab, [0.90] * 17 + [1.00, 1.10, 1.20])

    row = await _estimate(lab, research_engine, model="model1", dive=1)
    assert row["n_frames"] == 20
    assert row["length_max_m"] == pytest.approx(1.20)
    assert row["length_p90_m"] == pytest.approx(1.00), "rank 18 of 20"


async def test_p90_degenerates_to_max_for_small_n(lab, research_engine):
    """Nearest-rank p90 on n<=8 IS the max -- inherent, and why `n_frames` is
    exposed so consumers can filter. Kept in v2: `percentile_cont` would change
    numbers research compares against v1's."""
    await _frames(lab, [0.90, 0.95, 1.00])

    row = await _estimate(lab, research_engine, model="model1", dive=1)
    assert row["n_frames"] == 3
    assert row["length_p90_m"] == pytest.approx(1.00) == row["length_max_m"]


async def test_median_is_the_nearest_rank_middle(lab, research_engine):
    await _frames(lab, [0.80, 0.90, 1.00, 1.10, 1.20])

    row = await _estimate(lab, research_engine, model="model1", dive=1)
    assert row["length_median_m"] == pytest.approx(1.00)


async def test_median_of_an_even_count_is_the_lower_middle_not_an_average(
    lab, research_engine
):
    """v2 pin: nearest rank (n+1)/2 in integer division is rank 2 of 4 -- a
    frame's length, never percentile_cont's interpolated 0.95."""
    await _frames(lab, [0.80, 0.90, 1.00, 1.10])

    row = await _estimate(lab, research_engine, model="model1", dive=1)
    assert row["length_median_m"] == pytest.approx(0.90)


async def test_one_row_per_fish_and_dive(lab, research_engine):
    """A model Fish is shared across dives, so grouping must include dive_id or
    two dives' frames would be pooled into one estimate."""
    await _frames(lab, [1.00, 1.00, 1.00], dive=1)
    await _frames(lab, [2.00, 2.00, 2.00], dive=2)

    one = await _estimate(lab, research_engine, model="model1", dive=1)
    two = await _estimate(lab, research_engine, model="model1", dive=2)
    assert one["length_p90_m"] == pytest.approx(1.00)
    assert two["length_p90_m"] == pytest.approx(2.00)


async def test_measurements_without_a_length_are_excluded(lab, research_engine):
    """v2 allows a missing length only on a migrated row (v1 recorded some)."""
    await _frames(lab, [1.00, 1.00])
    await lab.measure(dive=1, model="model1", length_m=None, v1_id=990_001)

    row = await _estimate(lab, research_engine, model="model1", dive=1)
    assert row["n_frames"] == 2


async def test_the_estimate_names_the_model_and_leaves_species_empty(
    lab, research_engine
):
    """v1's columns: a model's `species_id` is NULL, a wild fish's `model_name`."""
    await _frames(lab, [1.00])

    row = await _estimate(lab, research_engine, model="model1", dive=1)
    assert (row["model_name"], row["species_id"]) == ("model1", None)


# ── the mislabel suspects (test_fish_model_mislabel_suspects.py) ──────


@pytest.fixture
async def known(references):
    for name, length in _KNOWN.items():
        await references(name, length)
    return references


async def _suspects(research_engine):
    return await rows(
        research_engine,
        "SELECT image_id, dive_id, labeled_model, best_fit_model, confidence "
        "FROM fish_model_species_mislabel_suspects ORDER BY image_id",
    )


async def test_correctly_labeled_models_are_not_flagged(lab, known, research_engine):
    for name, length in _KNOWN.items():
        await lab.measure(dive=1, model=name, length_m=length)

    assert await _suspects(research_engine) == []


async def test_mild_foreshortening_is_not_flagged(lab, known, research_engine):
    """Frames a few percent short are normal projection loss, not mislabels."""
    for pct in (0.0, -0.02, -0.05, -0.08):
        await lab.measure(dive=1, model="Snook", length_m=0.455 * (1 + pct))

    assert await _suspects(research_engine) == []


async def test_over_measured_frame_is_high_confidence(lab, known, research_engine):
    """Foreshortening cannot lengthen a fish, so a frame measuring far LONGER
    than its label is a mislabel regardless of geometry. (Prod: dive 84 image
    4868, labeled Purple Angel, measured 351mm = Grouper, +83%.)"""
    await lab.measure(dive=1, model="Purple Angel", length_m=0.351)

    got = await _suspects(research_engine)

    assert len(got) == 1
    assert got[0]["labeled_model"] == "Purple Angel"
    assert got[0]["best_fit_model"] == "Grouper"
    assert got[0]["confidence"] == "high"


async def test_short_frame_matching_another_model_is_medium(
    lab, known, research_engine
):
    """A Snook reading 330mm fits Grouper -- but a Snook angled ~21 deg reads
    exactly that, so length alone cannot decide. Surface for review, don't
    accuse. (Prod: the 16 Snook->Grouper frames, later confirmed real.)"""
    await lab.measure(dive=1, model="Snook", length_m=0.330)

    got = await _suspects(research_engine)

    assert len(got) == 1
    assert got[0]["best_fit_model"] == "Grouper"
    assert got[0]["confidence"] == "medium"


async def test_near_identical_lengths_do_not_produce_flags(lab, known, research_engine):
    """Purple Angel 0.192 / Gray Anthias 0.195 / Yellow Anthias 0.200 are
    within 4%, so length cannot discriminate them. Regression: an earlier
    group-maximum rule condemned 15 correct dive-59 Purple Angels."""
    for length in (0.192, 0.196, 0.199, 0.200):
        await lab.measure(dive=1, model="Purple Angel", length_m=length)

    assert await _suspects(research_engine) == []


async def test_one_bad_frame_does_not_condemn_its_neighbours(
    lab, known, research_engine
):
    """Regression: the group-maximum rule let prod dive 60's single 601mm frame
    inflate the Grouper group's max, flagging all 19 correct Groupers as Shark."""
    for _ in range(6):
        await lab.measure(dive=1, model="Grouper", length_m=0.355)
    bad, _ = await lab.measure(dive=1, model="Grouper", length_m=0.601)

    got = await _suspects(research_engine)

    assert [r["image_id"] for r in got] == [await lab.number("captures", bad)]
    assert got[0]["best_fit_model"] == "Shark"
    assert got[0]["confidence"] == "high"


async def test_a_provisional_reference_is_not_offered_as_a_best_fit(
    lab, known, research_engine
):
    """A reference length that is an ESTIMATE must never re-label a real frame.
    A Shark (605 mm) measured at 290 mm: a provisional 300 mm sits 3.3% away
    and would flag it."""
    await known("Weasly Fish", 0.30, provisional=True)
    await lab.measure(dive=1, model="Shark", length_m=0.29)

    assert await _suspects(research_engine) == []


async def test_a_non_provisional_reference_still_attracts(lab, known, research_engine):
    """The control: same frame, reference marked measured -- the flag appears."""
    await known("Weasly Fish", 0.30, provisional=False)
    await lab.measure(dive=1, model="Shark", length_m=0.29)

    got = await _suspects(research_engine)
    assert [r["best_fit_model"] for r in got] == ["Weasly Fish"]


async def test_a_provisional_model_is_still_graded_in_the_accuracy_view(
    lab, known, research_engine
):
    """Excluding it from re-labeling must not un-grade it."""
    await known("Weasly Fish", 0.30, provisional=True)
    await lab.measure(dive=1, model="Weasly Fish", length_m=0.29)

    got = await rows(
        research_engine, "SELECT model_name FROM fish_model_measurement_accuracy"
    )
    assert [r["model_name"] for r in got] == ["Weasly Fish"]


async def test_a_calibration_target_is_not_offered_as_a_best_fit(
    lab, known, research_engine
):
    """The Box (0.150 m) must never be the answer to "which model is this?": a
    correctly-labelled Gray Anthias at 0.160 m (own -17.9%, box fit 6.7%)."""
    await known("Box", 0.15)
    await lab.measure(dive=1, model="Gray Anthias", length_m=0.160)

    assert await _suspects(research_engine) == []


async def test_the_foreshortened_anthias_would_flag_against_a_real_model(
    lab, known, research_engine
):
    """The control for the test above: a genuine model at the box's length."""
    await known("Tiny Model", 0.15)
    await lab.measure(dive=1, model="Gray Anthias", length_m=0.160)

    got = await _suspects(research_engine)
    assert [s["best_fit_model"] for s in got] == ["Tiny Model"]


async def test_a_calibration_target_frame_is_not_itself_a_suspect(
    lab, known, research_engine
):
    """A badly-measured ruler is a calibration problem the accuracy view already
    reports. Own error +32.7% and landing exactly on Snook: both gates pass."""
    await known("Ruler", 0.3429)
    await lab.measure(dive=1, model="Ruler", length_m=0.455)

    assert await _suspects(research_engine) == []


async def test_a_superseded_reference_version_is_not_offered_as_a_best_fit(
    lab, known, research_engine
):
    """v2: only a model's current reference is a candidate. Its earlier,
    non-provisional length -- since corrected to a provisional estimate --
    would otherwise still attract the Shark frame v1 kept quiet about."""
    await known("Weasly Fish", 0.30, provisional=False, valid_from="2026-08-04")
    await known("Weasly Fish", 0.30, provisional=True, valid_from="2026-09-12")
    await lab.measure(dive=1, model="Shark", length_m=0.29)

    assert await _suspects(research_engine) == []
