"""The database side of stage 9 and the dive-slate Label Studio project.

Ported from fishsense-lite@77e8f8e5:

* services/fishsense-api/tests/test_select_next_dive_endpoints.py (the stage-9
  slate-preprocessing cohort) and test_cohort_needs_reprocess_all_kinds.py
  (its dive-slate rows);
* services/fishsense-api-workflow-worker/tests/
  test_resolve_slate_preprocess_inputs_activity.py,
  test_resolvers_honour_needs_reprocess.py and test_reprocess_flag_drains.py
  (their slate cases) -- the resolver is a query here, not an SDK walk;
* services/fishsense-api/tests/test_needs_reprocess_clear_scope.py and
  test_needs_reprocess_scoping.py (the dive-slate kind);
* test_populate_dive_slate_label_studio_project_activity.py and
  test_sync_dive_slate_labels_activity.py: the target selection, the row a
  task anchors, the supersede pass, and what the sync writes.

Names and reasons are v1's. v2 changes, each pinned by a test that says why:

* per tenant, oldest first (`created_at`), so the orchestrator can take the
  oldest candidate across the tenants it serves;
* **the cohort ignores a superseded species label**, as the resolver always
  did (v1's per-dive getter filtered it; the cohort did not, so a
  dead-lettered marker selected a dive that resolved nothing, hourly);
* populate targets **canonical** captures only;
* recording a task re-anchors the row without wiping what the labeler did;
* the sync writes only the columns it owns, and skips a superseded row (v1:
  its lookup by task filtered superseded rows out).

`superseded` is `NOT NULL` in v2, so v1's NULL-superseded cases
(test_needs_reprocess_null_superseded.py) cannot arise and are not ported.
"""

import uuid

import pytest
from sqlalchemy import text

from _slate_calibration_seed import (
    DISTORTION,
    K,
    SLATE_MARKER,
    TEMPLATE_POINTS,
    T0,
    capture,
    device_with_camera,
    dive,
    later,
    slate_label,
    slate_template,
    species,
    tenant,
)
from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.slate_store import (
    SlateCatalog,
    SlateInputsUnavailable,
    SlateSync,
    apply_slate_sync,
    clear_slate_reprocess_flags,
    dive_has_slate_labels_in_project,
    flag_slate_labels_for_reprocess,
    next_dive_for_slate_preprocessing,
    record_slate_label,
    slate_label_studio_projects,
    slate_populate_candidates,
    slate_preprocess_inputs,
    slate_template_for_task,
    supersede_stale_slate_labels,
)

ORCHESTRATOR = "service:fishsense-orchestrator"


async def _next(app_engine, lab):
    async with tenant_transaction(app_engine, lab) as conn:
        candidate = await next_dive_for_slate_preprocessing(conn, lab)
    return None if candidate is None else candidate.dive_id


#: `_slate_dive`'s default: a device of its own with a camera calibration.
_A_CAMERA = object()


async def _slate_dive(owner_engine, lab, *, device=_A_CAMERA, template=None, **kwargs):
    """A dive with a slate template (built from `template`'s overrides) whose
    device has a camera calibration, unless `device` says otherwise."""
    if device is _A_CAMERA:
        device, _ = await device_with_camera(owner_engine, lab)
    return await dive(
        owner_engine,
        lab,
        slate=await slate_template(owner_engine, **(template or {})),
        device=device,
        **kwargs,
    )


async def _row(owner_engine, label_id):
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT * FROM slate_labels WHERE id = :i"), {"i": label_id}
            )
        ).one()


# ---------- stage 9: the cohort ----------


async def test_slate_preprocessing_requires_dive_slate_id_and_marker(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    # dive 1: HIGH but no slate template -> excluded.
    # dive 2: HIGH + slate but no slate-marked species label -> excluded.
    # dive 3: HIGH + slate + slate-marked + no slate label -> picked.
    no_slate = await dive(owner_engine, lab, created_at=T0)
    unmarked = await _slate_dive(owner_engine, lab, created_at=later(1))
    marked = await _slate_dive(owner_engine, lab, created_at=later(2))
    await species(owner_engine, lab, await capture(owner_engine, lab, no_slate))
    await species(
        owner_engine, lab, await capture(owner_engine, lab, unmarked), content="Fish"
    )
    await species(owner_engine, lab, await capture(owner_engine, lab, marked))

    assert await _next(app_engine, lab) == marked


async def test_slate_preprocessing_skips_when_every_slate_image_labeled(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    first = await _slate_dive(owner_engine, lab, created_at=T0)
    second = await _slate_dive(owner_engine, lab, created_at=later(1))
    labeled = await capture(owner_engine, lab, first)
    await species(owner_engine, lab, labeled)
    await species(owner_engine, lab, await capture(owner_engine, lab, second))
    await slate_label(owner_engine, lab, labeled, completed=True)

    assert await _next(app_engine, lab) == second


async def test_slate_preprocessing_excludes_dive_with_only_incomplete_slate_labels(
    owner_engine, app_engine
):
    """Once populate seeds an incomplete slate label (with a real project) for
    every slate-marked image, the dive drops out of the cohort."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await species(owner_engine, lab, frame)
    await slate_label(owner_engine, lab, frame, completed=False)

    assert await _next(app_engine, lab) is None


async def test_slate_preprocessing_excludes_dive_when_sentinel_coexists_with_real_label(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await species(owner_engine, lab, frame)
    await slate_label(owner_engine, lab, frame, project=None, source="import")
    await slate_label(owner_engine, lab, frame, completed=False)

    assert await _next(app_engine, lab) is None


async def test_slate_preprocessing_ignores_null_project_sentinels(
    owner_engine, app_engine
):
    """NULL-project slate labels are legacy sentinels. They must NOT drop a
    dive from the slate cohort."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await species(owner_engine, lab, frame)
    await slate_label(owner_engine, lab, frame, project=None, source="import")

    assert await _next(app_engine, lab) == only


async def test_a_dead_lettered_slate_label_does_not_count_as_done(
    owner_engine, app_engine
):
    """v1's `superseded == False` on the slate-label side of the gate, kept so
    the three preprocess gates are spelled alike."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await species(owner_engine, lab, frame)
    await slate_label(owner_engine, lab, frame, superseded=True)

    assert await _next(app_engine, lab) == only


async def test_a_superseded_species_marker_does_not_select_the_dive(
    owner_engine, app_engine
):
    """v2 fix. v1's cohort read every species label, but its resolver read them
    through the per-dive getter, which drops superseded rows -- so a dive whose
    only marker was dead-lettered was selected, resolved nothing, and was
    selected again next hour, ahead of every newer dive. The two now agree."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    await species(
        owner_engine, lab, await capture(owner_engine, lab, only), superseded=True
    )

    assert await _next(app_engine, lab) is None


async def test_non_canonical_frames_do_not_count(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    await species(
        owner_engine, lab, await capture(owner_engine, lab, only, canonical=False)
    )

    assert await _next(app_engine, lab) is None


async def test_only_high_priority_dives_are_offered(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    low = await _slate_dive(owner_engine, lab, priority="low")
    await species(owner_engine, lab, await capture(owner_engine, lab, low))

    assert await _next(app_engine, lab) is None


async def test_another_tenants_dive_is_never_offered(owner_engine, app_engine):
    lab, reef = await tenant(owner_engine), await tenant(owner_engine, "reef")
    theirs = await _slate_dive(owner_engine, reef)
    await species(owner_engine, reef, await capture(owner_engine, reef, theirs))

    assert await _next(app_engine, lab) is None


_UNRESOLVABLE = [
    pytest.param({"device": None}, id="no camera calibration"),
    pytest.param({"template": {"dpi": None}}, id="no dpi"),
    pytest.param({"template": {"reference_points": []}}, id="no reference points"),
    pytest.param({"template": {"source_path": None}}, id="no NAS path"),
]


@pytest.mark.parametrize("unresolvable", _UNRESOLVABLE)
async def test_a_dive_stage_9_cannot_resolve_is_not_offered(
    owner_engine, app_engine, unresolvable
):
    """v2 fix. The resolver refuses these (and `stage_slate_pdf` a template
    with no NAS path) non-retryably, with nothing written, so a cohort that
    offered the dive would hand it back every hour, ahead of every newer
    one. Each is fixed in reference data, not by a rerun: the dive leaves the
    cohort until then, and comes back by itself."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab, **unresolvable)
    await species(owner_engine, lab, await capture(owner_engine, lab, only))

    assert await _next(app_engine, lab) is None


@pytest.mark.parametrize("unresolvable", _UNRESOLVABLE)
async def test_an_unresolvable_dive_does_not_hold_up_another_tenants(
    owner_engine, app_engine, seed_memberships, unresolvable
):
    """The orchestrator takes the oldest candidate across every tenant it
    serves; an older dive left in the cohort that can never resolve would be
    that candidate every hour, and no other tenant's dive would be drawn."""
    tenants = await seed_memberships(
        {ORCHESTRATOR: {"lab": "member", "reef": "member"}}
    )
    lab, reef = tenants["lab"], tenants["reef"]
    older = await _slate_dive(owner_engine, lab, created_at=T0, **unresolvable)
    await species(owner_engine, lab, await capture(owner_engine, lab, older))
    younger = await _slate_dive(owner_engine, reef, created_at=later(1))
    await species(owner_engine, reef, await capture(owner_engine, reef, younger))
    catalog = SlateCatalog(app_engine, sub=ORCHESTRATOR)

    offered = [
        candidate.dive_id
        for tenant_id in await catalog.member_tenants()
        if (candidate := await catalog.next_dive_for_slate_preprocessing(tenant_id))
    ]
    assert offered == [younger]


# ---------- stage 9: the reprocess flag is the second way in ----------


async def _labelled_dive(owner_engine, lab, *, flagged, canonical=True):
    """A dive whose slate frame is fully labelled, optionally flagged."""
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only, canonical=canonical)
    await species(owner_engine, lab, frame)
    await slate_label(owner_engine, lab, frame, completed=True, needs_reprocess=flagged)
    return only, frame


async def test_fully_labelled_dive_is_not_selected_without_a_flag(
    owner_engine, app_engine
):
    """The control. If this ever fails the test below proves nothing."""
    lab = await tenant(owner_engine)
    await _labelled_dive(owner_engine, lab, flagged=False)

    assert await _next(app_engine, lab) is None


async def test_flagged_dive_is_selected(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only, _ = await _labelled_dive(owner_engine, lab, flagged=True)

    assert await _next(app_engine, lab) == only


async def test_flag_on_a_non_canonical_image_does_not_select(owner_engine, app_engine):
    """Only the canonical copy is ever preprocessed, so a flag on a duplicate
    would select a dive the resolver finds no work for -- and the dive would
    re-stage its raw bytes from the NAS every hour forever."""
    lab = await tenant(owner_engine)
    await _labelled_dive(owner_engine, lab, flagged=True, canonical=False)

    assert await _next(app_engine, lab) is None


async def test_flag_on_a_superseded_row_does_not_select(owner_engine, app_engine):
    """The resolver never sees a superseded row, so neither may the cohort."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await species(owner_engine, lab, frame)
    await slate_label(owner_engine, lab, frame, completed=True)
    await slate_label(
        owner_engine, lab, frame, project=2, superseded=True, needs_reprocess=True
    )

    assert await _next(app_engine, lab) is None


# ---------- stage 9: the resolver ----------


async def _resolve(app_engine, lab, dive_id):
    async with tenant_transaction(app_engine, lab) as conn:
        return await slate_preprocess_inputs(conn, lab, dive_id)


async def _resolver_scene(owner_engine, lab, **template):
    device, calibration = await device_with_camera(owner_engine, lab)
    slate = await slate_template(owner_engine, **template)
    only = await dive(owner_engine, lab, slate=slate, device=device)
    return only, slate, calibration


async def test_returns_only_slate_marked_without_any_slate_label(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only, slate, calibration = await _resolver_scene(owner_engine, lab)
    frames = {}
    for n, content in ((1, SLATE_MARKER), (2, "Fish"), (3, SLATE_MARKER),
                       (4, SLATE_MARKER)):  # fmt: skip
        frames[n] = await capture(
            owner_engine, lab, only, checksum=f"{n:032x}", v1_id=None
        )
        await species(owner_engine, lab, frames[n], content=content)
    await slate_label(owner_engine, lab, frames[1], completed=True)
    await slate_label(owner_engine, lab, frames[4], completed=False)

    inputs = await _resolve(app_engine, lab, only)

    # 1: slate-marked but slate label exists (completed) -> dropped.
    # 2: not slate-marked -> dropped.
    # 3: slate-marked, no slate label row -> kept.
    # 4: slate-marked but slate label exists (incomplete) -> dropped
    #    (any row excludes -- matches the cohort).
    assert [c.checksum for c in inputs.captures] == [f"{3:032x}"]
    assert inputs.captures[0].capture_id == frames[3]
    assert inputs.dive_id == only
    assert inputs.slate_template.id == slate
    assert inputs.slate_template.dpi == 300
    assert inputs.slate_template.reference_points == [tuple(p) for p in TEMPLATE_POINTS]
    assert inputs.camera_matrix == K
    assert inputs.distortion_coefficients == DISTORTION
    assert inputs.camera_calibration_id == calibration


async def test_image_with_only_null_project_sentinel_treated_as_unlabeled(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only, _, _ = await _resolver_scene(owner_engine, lab)
    sentinel = await capture(owner_engine, lab, only, checksum="a" * 32)
    real = await capture(owner_engine, lab, only, checksum="b" * 32)
    for frame in (sentinel, real):
        await species(owner_engine, lab, frame)
    await slate_label(owner_engine, lab, sentinel, project=None, source="import")
    await slate_label(owner_engine, lab, real, completed=False)

    inputs = await _resolve(app_engine, lab, only)

    assert [c.checksum for c in inputs.captures] == ["a" * 32]


async def test_raises_for_dive_without_a_slate_template(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    device, _ = await device_with_camera(owner_engine, lab)
    only = await dive(owner_engine, lab, device=device)

    try:
        await _resolve(app_engine, lab, only)
    except SlateInputsUnavailable as exc:
        assert "no slate template" in str(exc)
    else:
        raise AssertionError("resolved a dive with no slate template")


async def test_raises_when_the_template_has_no_dpi_or_reference_points(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    for template in ({"dpi": None}, {"reference_points": []}):
        only, _, _ = await _resolver_scene(owner_engine, lab, **template)
        try:
            await _resolve(app_engine, lab, only)
        except SlateInputsUnavailable as exc:
            assert "missing dpi or reference_points" in str(exc)
        else:
            raise AssertionError(f"resolved a template with {template}")


async def test_raises_when_the_camera_has_no_calibration(owner_engine, app_engine):
    """v1: `camera_id=... has no intrinsics`. v2 reads the device's current
    camera calibration."""
    lab = await tenant(owner_engine)
    only = await dive(owner_engine, lab, slate=await slate_template(owner_engine))

    try:
        await _resolve(app_engine, lab, only)
    except SlateInputsUnavailable as exc:
        assert "camera calibration" in str(exc)
    else:
        raise AssertionError("resolved a dive with no camera calibration")


async def test_a_flagged_image_resolves_without_the_content_marker(
    owner_engine, app_engine
):
    """The cohort's flag branch applies no marker gate, so neither may this: a
    flag that resolves to nothing re-stages the dive from the NAS hourly."""
    lab = await tenant(owner_engine)
    only, _, _ = await _resolver_scene(owner_engine, lab)
    frame = await capture(owner_engine, lab, only, checksum="c" * 32)
    await species(owner_engine, lab, frame, content="Fish")
    await slate_label(owner_engine, lab, frame, completed=True, needs_reprocess=True)

    inputs = await _resolve(app_engine, lab, only)

    assert [c.checksum for c in inputs.captures] == ["c" * 32]


async def test_resolved_frames_are_canonical_and_listed_once(owner_engine, app_engine):
    """Canonical only, as every cohort; and a frame both marked and flagged is
    still one frame (v1 listed the marked ones, then the flagged ones not
    already seen)."""
    lab = await tenant(owner_engine)
    only, _, _ = await _resolver_scene(owner_engine, lab)
    both = await capture(owner_engine, lab, only, checksum="d" * 32)
    await species(owner_engine, lab, both)
    await slate_label(owner_engine, lab, both, project=None, source="import",
                      needs_reprocess=True)  # fmt: skip
    duplicate = await capture(owner_engine, lab, only, canonical=False)
    await species(owner_engine, lab, duplicate)

    inputs = await _resolve(app_engine, lab, only)

    assert [c.checksum for c in inputs.captures] == ["d" * 32]


async def test_a_capture_says_whether_it_came_from_v1(owner_engine, app_engine):
    """So the orchestrator can redraw a migrated frame's JPEG where v1 wrote
    it, and keep the URL its Label Studio task holds."""
    lab = await tenant(owner_engine)
    only, _, _ = await _resolver_scene(owner_engine, lab)
    await species(owner_engine, lab, await capture(owner_engine, lab, only, v1_id=4411))

    inputs = await _resolve(app_engine, lab, only)

    assert [c.from_v1 for c in inputs.captures] == [True]


# ---------- the reprocess flags: raise and clear ----------


async def _flag_scene(owner_engine, lab):
    """(open canonical, answered canonical, open non-canonical)."""
    only = await _slate_dive(owner_engine, lab)
    rows = []
    for n, (completed, canonical) in enumerate(
        ((False, True), (True, True), (False, False))
    ):
        frame = await capture(
            owner_engine, lab, only, canonical=canonical, checksum=f"{n + 10:032x}"
        )
        rows.append(await slate_label(owner_engine, lab, frame, completed=completed))
    return only, rows


async def _flags(owner_engine, rows):
    return [(await _row(owner_engine, r)).needs_reprocess for r in rows]


async def test_default_flags_only_incomplete_canonical_labels(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only, rows = await _flag_scene(owner_engine, lab)

    async with tenant_transaction(app_engine, lab) as conn:
        n = await flag_slate_labels_for_reprocess(conn, lab, only)

    assert n == 1, "only the open canonical label should be flagged"
    assert await _flags(owner_engine, rows) == [True, False, False]


async def test_only_incomplete_false_flags_completed_too(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only, rows = await _flag_scene(owner_engine, lab)

    async with tenant_transaction(app_engine, lab) as conn:
        n = await flag_slate_labels_for_reprocess(
            conn, lab, only, only_incomplete=False
        )

    assert n == 2, "both canonical labels, still never the non-canonical one"
    assert await _flags(owner_engine, rows) == [True, True, False]


async def test_clearing_lowers_every_canonical_flag_regardless_of_completion(
    owner_engine, app_engine
):
    """The parent clears after its child completes. If clear inherited the
    incomplete-only scope, a label completed *between* the flag being raised
    and the redraw finishing would keep its flag up and hold the dive in the
    cohort forever."""
    lab = await tenant(owner_engine)
    only, rows = await _flag_scene(owner_engine, lab)
    async with tenant_transaction(app_engine, lab) as conn:
        await flag_slate_labels_for_reprocess(conn, lab, only, only_incomplete=False)
        n = await clear_slate_reprocess_flags(conn, lab, only, checksums=None)

    assert n == 2
    assert await _flags(owner_engine, rows) == [False, False, False]


async def test_a_superseded_row_is_never_flagged(owner_engine, app_engine):
    """The resolver cannot see it, so a flag on it would select a dive that
    resolves nothing."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    row = await slate_label(
        owner_engine, lab, await capture(owner_engine, lab, only), superseded=True
    )

    async with tenant_transaction(app_engine, lab) as conn:
        assert await flag_slate_labels_for_reprocess(conn, lab, only) == 0
    assert (await _row(owner_engine, row)).needs_reprocess is False


async def _two_flagged(owner_engine, lab):
    """Two flagged frames: one the run redrew, one flagged while it ran."""
    only = await _slate_dive(owner_engine, lab)
    rows = []
    for n in (10, 11):
        frame = await capture(owner_engine, lab, only, checksum=f"{n:032d}")
        rows.append(await slate_label(owner_engine, lab, frame, needs_reprocess=True))
    return only, rows


async def test_scoped_clear_leaves_a_flag_raised_during_the_run(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only, rows = await _two_flagged(owner_engine, lab)

    async with tenant_transaction(app_engine, lab) as conn:
        n = await clear_slate_reprocess_flags(conn, lab, only, checksums=[f"{10:032d}"])

    assert n == 1
    assert await _flags(owner_engine, rows) == [False, True]


async def test_unscoped_clear_still_lowers_everything(owner_engine, app_engine):
    """The no-work backstop. Without this the dive can never drain."""
    lab = await tenant(owner_engine)
    only, rows = await _two_flagged(owner_engine, lab)

    async with tenant_transaction(app_engine, lab) as conn:
        n = await clear_slate_reprocess_flags(conn, lab, only, checksums=None)

    assert n == 2
    assert await _flags(owner_engine, rows) == [False, False]


async def test_an_empty_scope_is_not_read_as_no_scope(owner_engine, app_engine):
    """`[]` means "this run redrew nothing", which must clear nothing."""
    lab = await tenant(owner_engine)
    only, rows = await _two_flagged(owner_engine, lab)

    async with tenant_transaction(app_engine, lab) as conn:
        n = await clear_slate_reprocess_flags(conn, lab, only, checksums=[])

    assert n == 0
    assert await _flags(owner_engine, rows) == [True, True]


async def test_a_dive_with_no_labels_clears_zero(owner_engine, app_engine):
    """The parent clears unconditionally; that must not be an error."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await clear_slate_reprocess_flags(conn, lab, only, checksums=None) == 0


# ---------- populate: targets ----------


async def _candidates(app_engine, lab, dive_id):
    async with tenant_transaction(app_engine, lab) as conn:
        return await slate_populate_candidates(conn, lab, dive_id)


async def test_select_targets_filters_by_slate_marker_and_completion(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frames = {}
    for n, content in ((1, SLATE_MARKER), (2, "Fish"), (3, SLATE_MARKER)):
        frames[n] = await capture(owner_engine, lab, only, captured_at=later(n))
        await species(owner_engine, lab, frames[n], content=content)
    await slate_label(owner_engine, lab, frames[1], completed=True)

    candidates = await _candidates(app_engine, lab, only)

    assert [c.capture_id for c in candidates] == [frames[3]]


async def test_an_incomplete_slate_label_keeps_its_frame_a_target(
    owner_engine, app_engine
):
    """Populate targets frames with no COMPLETED slate label (the cohort's rule
    is "no live label at all"): an incomplete row is re-anchored, not
    skipped."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await species(owner_engine, lab, frame)
    await slate_label(owner_engine, lab, frame, completed=False)

    assert [c.capture_id for c in await _candidates(app_engine, lab, only)] == [frame]


async def test_populate_targets_canonical_frames_only(owner_engine, app_engine):
    """v2: v1 took every marked image. A duplicate frame shares its canonical
    twin's checksum and so its JPEG and task URL, and two rows cannot anchor
    one Label Studio task (v2's labels are unique per task)."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    await species(
        owner_engine, lab, await capture(owner_engine, lab, only, canonical=False)
    )

    assert await _candidates(app_engine, lab, only) == []


async def test_a_superseded_marker_is_not_a_target(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    await species(
        owner_engine, lab, await capture(owner_engine, lab, only), superseded=True
    )

    assert await _candidates(app_engine, lab, only) == []


async def test_a_candidate_carries_what_its_task_needs(owner_engine, app_engine):
    """Its number (v1's image id, the task's `image_id`), its capture time
    (the task's `taken`), and where to look for its JPEG."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(
        owner_engine, lab, only, v1_id=77001, checksum="e" * 32, captured_at=later(3)
    )
    await species(owner_engine, lab, frame)

    (candidate,) = await _candidates(app_engine, lab, only)

    assert (candidate.number, candidate.checksum, candidate.from_v1) == (
        77001,
        "e" * 32,
        True,
    )
    assert candidate.captured_at == later(3)


# ---------- populate: the row a task anchors ----------


async def test_recording_a_task_writes_an_incomplete_human_row(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)

    async with tenant_transaction(app_engine, lab) as conn:
        await record_slate_label(
            conn, lab, frame, ls_project_id=66, ls_task_id=4001, image_url="s3://b/k"
        )

    async with owner_engine.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM slate_labels"))).one()
    assert (row.capture_id, row.ls_project_id, row.ls_task_id) == (frame, 66, 4001)
    assert (row.completed, row.superseded, row.source) == (False, False, "human")
    assert row.image_url == "s3://b/k"


async def test_re_recording_revives_the_row_and_keeps_what_the_labeler_did(
    owner_engine, app_engine
):
    """v2 change. v1 re-PUT a whole fresh row, so an incomplete row's geometry
    -- written by a sync whose cursor has since moved past it -- was wiped and
    never re-sent. The row is re-anchored and revived (v1 un-superseded it
    too); the labeler's work and the reprocess flag stay."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    label = await slate_label(
        owner_engine, lab, frame, task=4001, superseded=True, needs_reprocess=True,
        reference_points=[[1.0, 2.0]],
    )  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        await record_slate_label(
            conn, lab, frame, ls_project_id=66, ls_task_id=4001, image_url="s3://b/k"
        )

    row = await _row(owner_engine, label)
    assert row.superseded is False
    assert row.reference_points == [[1.0, 2.0]]
    assert row.needs_reprocess is True
    assert row.image_url == "s3://b/k"


async def test_a_task_cannot_anchor_another_tenants_capture(owner_engine, app_engine):
    lab, reef = await tenant(owner_engine), await tenant(owner_engine, "reef")
    theirs = await capture(owner_engine, reef, await _slate_dive(owner_engine, reef))

    try:
        async with tenant_transaction(app_engine, lab) as conn:
            await record_slate_label(
                conn, lab, theirs, ls_project_id=66, ls_task_id=9, image_url="s3://x"
            )
    except Exception as exc:  # pylint: disable=broad-except
        assert "foreign key" in str(exc)
    else:
        raise AssertionError("anchored a task on another tenant's capture")


# ---------- populate: the supersede pass ----------


async def test_supersedes_incomplete_rows_this_project_no_longer_owns(
    owner_engine, app_engine
):
    """Legacy-project rows and this project's rows for frames no longer
    targeted are dead-lettered; this project's rows for this run's candidates
    are kept (the "same project AND refreshed" rule -- dive 341 oscillated
    without both halves); completed rows are never touched."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    kept_frame = await capture(owner_engine, lab, only)
    gone_frame = await capture(owner_engine, lab, only)
    kept = await slate_label(owner_engine, lab, kept_frame, project=66)
    legacy = await slate_label(owner_engine, lab, kept_frame, project=5)
    stale = await slate_label(owner_engine, lab, gone_frame, project=66)
    answered = await slate_label(owner_engine, lab, gone_frame, project=7,
                                 completed=True)  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        n = await supersede_stale_slate_labels(
            conn, lab, only, ls_project_id=66, keep_capture_ids=[kept_frame]
        )

    assert n == 2
    superseded = {
        r: (await _row(owner_engine, r)).superseded
        for r in (kept, legacy, stale, answered)
    }
    assert superseded == {kept: False, legacy: True, stale: True, answered: False}


async def test_the_supersede_pass_stays_in_its_dive(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mine, other = await _slate_dive(owner_engine, lab), await _slate_dive(
        owner_engine, lab
    )
    elsewhere = await slate_label(
        owner_engine, lab, await capture(owner_engine, lab, other), project=5
    )

    async with tenant_transaction(app_engine, lab) as conn:
        await supersede_stale_slate_labels(
            conn, lab, mine, ls_project_id=66, keep_capture_ids=[]
        )

    assert (await _row(owner_engine, elsewhere)).superseded is False


async def test_whether_a_project_already_holds_the_dives_rows(owner_engine, app_engine):
    """Publish needs "the project holds tasks": live rows of this dive in it."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await slate_label(owner_engine, lab, frame, project=66)
    await slate_label(owner_engine, lab, frame, project=67, superseded=True)

    async with tenant_transaction(app_engine, lab) as conn:
        assert await dive_has_slate_labels_in_project(conn, lab, only, 66)
        assert not await dive_has_slate_labels_in_project(conn, lab, only, 67)
        assert not await dive_has_slate_labels_in_project(conn, lab, only, 68)


# ---------- the sync ----------


def _sync(**overrides) -> SlateSync:
    values = {
        "completed": True,
        "reference_points": [(50.0, 25.0)],
        "slate_rectangle": [(1.0, 2.0), (3.0, 4.0)],
        "skipped_points": [0, 2],
        "ls_labeler_id": 141592,
        "ls_updated_at": later(5),
        "ls_payload": {"id": 1},
    }
    values.update(overrides)
    return SlateSync(**values)


async def test_projects_are_the_distinct_live_slate_label_projects(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    for project, superseded in ((66, False), (66, False), (88, True)):
        await slate_label(
            owner_engine, lab, await capture(owner_engine, lab, only),
            project=project, superseded=superseded,
        )  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        assert await slate_label_studio_projects(conn, lab) == [66]


async def test_a_task_updates_its_slate_label(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    label = await slate_label(
        owner_engine, lab, await capture(owner_engine, lab, only), task=7001
    )

    async with tenant_transaction(app_engine, lab) as conn:
        assert await apply_slate_sync(conn, lab, 7001, _sync()) is True

    row = await _row(owner_engine, label)
    assert row.completed is True
    assert row.reference_points == [[50.0, 25.0]]
    assert row.slate_rectangle == [[1.0, 2.0], [3.0, 4.0]]
    assert row.skipped_points == [0, 2]
    assert (row.ls_labeler_id, row.ls_updated_at) == (141592, later(5))
    assert row.ls_payload == {"id": 1}


async def test_absent_fields_keep_the_last_ones(owner_engine, app_engine):
    """v1 overwrote skipped points and geometry only when the annotation held
    them; `completed` always follows the task."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    label = await slate_label(
        owner_engine, lab, await capture(owner_engine, lab, only), task=7002
    )
    async with tenant_transaction(app_engine, lab) as conn:
        await apply_slate_sync(conn, lab, 7002, _sync())
        await apply_slate_sync(
            conn, lab, 7002,
            _sync(completed=False, reference_points=None, slate_rectangle=None,
                  skipped_points=None, ls_labeler_id=None),
        )  # fmt: skip

    row = await _row(owner_engine, label)
    assert row.completed is False
    assert row.reference_points == [[50.0, 25.0]]
    assert row.skipped_points == [0, 2]
    assert row.ls_labeler_id == 141592


async def test_the_sync_never_touches_the_reprocess_flag(owner_engine, app_engine):
    """The flag is the cohort's, not Label Studio's (v2 writes named columns;
    v1 PUT the whole row back and could clear a flag raised in between)."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    label = await slate_label(
        owner_engine, lab, await capture(owner_engine, lab, only), task=7003,
        needs_reprocess=True,
    )  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        await apply_slate_sync(conn, lab, 7003, _sync())

    assert (await _row(owner_engine, label)).needs_reprocess is True


async def test_a_task_with_no_live_label_is_skipped(owner_engine, app_engine):
    """v1 looked a label up by task among live rows only, and skipped a task
    with none -- a dead-lettered row is not revived by its old task."""
    lab = await tenant(owner_engine)
    only = await _slate_dive(owner_engine, lab)
    label = await slate_label(
        owner_engine, lab, await capture(owner_engine, lab, only), task=7004,
        superseded=True,
    )  # fmt: skip

    async with tenant_transaction(app_engine, lab) as conn:
        assert await apply_slate_sync(conn, lab, 404, _sync()) is False
        assert await apply_slate_sync(conn, lab, 7004, _sync()) is False

    assert (await _row(owner_engine, label)).completed is False


async def test_a_task_resolves_to_its_dives_slate_template(owner_engine, app_engine):
    """The sync needs the template to remove the composite's panel offset."""
    lab = await tenant(owner_engine)
    slate = await slate_template(owner_engine)
    only = await dive(owner_engine, lab, slate=slate)
    frame = await capture(owner_engine, lab, only)
    await slate_label(owner_engine, lab, frame, task=7005)
    bare = await dive(owner_engine, lab)
    await slate_label(
        owner_engine, lab, await capture(owner_engine, lab, bare), task=7006
    )

    async with tenant_transaction(app_engine, lab) as conn:
        found = await slate_template_for_task(conn, lab, 7005)
        unresolvable = await slate_template_for_task(conn, lab, 7006)
        missing = await slate_template_for_task(conn, lab, 404)

    assert (found.capture_id, found.slate_template_id) == (frame, slate)
    assert unresolvable.slate_template_id is None
    assert missing is None


# ---------- the catalog, as the orchestrator's principal ----------


async def test_the_catalog_works_within_a_served_tenant(
    owner_engine, app_engine, seed_memberships
):
    lab = (await seed_memberships({ORCHESTRATOR: {"lab": "member"}}))["lab"]
    only = await _slate_dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await species(owner_engine, lab, frame)
    catalog = SlateCatalog(app_engine, sub=ORCHESTRATOR)

    assert await catalog.member_tenants() == [lab]
    candidate = await catalog.next_dive_for_slate_preprocessing(lab)
    assert candidate.dive_id == only
    await catalog.record_slate_label(
        lab, frame, ls_project_id=66, ls_task_id=8001, image_url="s3://b/k"
    )
    assert await catalog.slate_label_studio_projects(lab) == [66]
    assert await catalog.apply_slate_sync(lab, 8001, _sync()) is True
    # v1 counts the rows it touched, flagged or not (its idempotency test).
    assert await catalog.clear_slate_reprocess_flags(lab, only, checksums=None) == 1
