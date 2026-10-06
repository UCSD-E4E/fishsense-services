"""`slate_presence_evaluation`: each slate prediction beside the human answer.

New in v2, for the paper (owner's decision, 2026-10-05): every canonical frame
is scored once per model version, and this view joins **every** prediction
row (every version; `is_current` marks the latest) to the frame's human
answer, labelled by 2026-10-03_slate_detector@95a77d95's manifest rules
(src/slate_detector/dataset.py `LABELLED_SQL`, `slate_label`):

* the answers are the frame's completed, live species labels with a
  `content_of_image`, plus `Slate (diveslatelabel)` for a completed, live
  slate label;
* **slate**: every answer starts with `Slate`; **no slate**: none does, and
  every one starts with `Fish` (so `Fish Model` too), `Calibration Targets`
  or `None`; **ambiguous**: anything else; no answer at all: NULL.

The source repo's own overrides and labelling-queue answers live in that
repo, not here. A completed slate label on a task the detector queued is an
answer to a question the model chose; `slate_label_from_detector` marks it
so an analysis can leave it out. Dive, image and camera are v1's numbers, for
dive-grouped metrics.

It is read by the research role (0032's lab-only binding), not the app.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from _slate_calibration_seed import (
    capture,
    device_with_camera,
    dive,
    run,
    slate_label,
    slate_presence,
    slate_template,
    species,
    tenant,
)
from research_seed import rows

SLATE = "Slate, Laser on slate"
FISH = "Fish, Hogfish (Lachnolaimus maximus)"


async def _dive(owner_engine, lab, **kwargs):
    device, _ = await device_with_camera(owner_engine, lab)
    return await dive(owner_engine, lab, device=device, **kwargs)


async def _evaluation(engine, **where):
    clause = " AND ".join(f"{k} = :{k}" for k in where) or "true"
    return await rows(
        engine,
        f"SELECT * FROM public.slate_presence_evaluation WHERE {clause} ORDER BY seq",
        **where,
    )


async def _label_of(owner_engine, capture_id):
    (row,) = await _evaluation(owner_engine, capture_id=capture_id)
    return row["human_label"], row["human_answers"]


@pytest.mark.parametrize(
    ("answers", "label"),
    [
        ([SLATE], "slate"),
        (["Slate, Laser not on slate", "Slate not in list"], "slate"),
        ([FISH, "None"], "no_slate"),
        (["Fish Model, Snapper", "Calibration Targets, Checkerboard"], "no_slate"),
        ([SLATE, FISH], "ambiguous"),
        (["Other (Identifiable but Nontarget)"], "ambiguous"),
        ([], None),
    ],
)
async def test_the_manifests_label_rules(owner_engine, answers, label):
    lab = await tenant(owner_engine)
    frame = await capture(owner_engine, lab, await _dive(owner_engine, lab))
    for project, answer in enumerate(answers, start=70):
        await species(owner_engine, lab, frame, content=answer, project=project)
    await slate_presence(owner_engine, lab, frame)

    assert (await _label_of(owner_engine, frame))[0] == label


async def test_a_completed_slate_label_is_a_slate_answer(owner_engine):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    done = await capture(owner_engine, lab, only)
    open_task = await capture(owner_engine, lab, only)
    await slate_label(owner_engine, lab, done, completed=True)
    await slate_label(owner_engine, lab, open_task, completed=False)
    for frame in (done, open_task):
        await slate_presence(owner_engine, lab, frame)

    assert await _label_of(owner_engine, done) == ("slate", "Slate (diveslatelabel)")
    assert await _label_of(owner_engine, open_task) == (None, None)


async def test_only_live_completed_answers_count(owner_engine):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    frame = await capture(owner_engine, lab, only)
    await species(owner_engine, lab, frame, content=SLATE, superseded=True)
    await species(owner_engine, lab, frame, content="", project=71)
    await species(owner_engine, lab, frame, content=FISH, project=72)
    await run(
        owner_engine,
        "UPDATE species_labels SET completed = false WHERE content_of_image = :f",
        f=FISH,
    )
    await slate_presence(owner_engine, lab, frame)

    assert await _label_of(owner_engine, frame) == (None, None)


async def test_every_prediction_row_with_the_latest_marked(owner_engine):
    """A later model version adds rows; both stay comparable."""
    lab = await tenant(owner_engine)
    frame = await capture(owner_engine, lab, await _dive(owner_engine, lab))
    await species(owner_engine, lab, frame, content=SLATE)
    first = await slate_presence(owner_engine, lab, frame, probability=0.4)
    second = await slate_presence(
        owner_engine, lab, frame, probability=0.99, model_version=2
    )

    got = await _evaluation(owner_engine, capture_id=frame)

    assert [
        (r["prediction_id"], r["model_version"], r["probability"],
         r["predicted_slate"], r["is_current"], r["human_label"])
        for r in got
    ] == [
        (first, 1, 0.4, False, False, "slate"),
        (second, 2, 0.99, True, True, "slate"),
    ]  # fmt: skip


async def test_it_carries_dive_image_camera_and_provenance(owner_engine):
    lab = await tenant(owner_engine)
    device, _ = await device_with_camera(owner_engine, lab)
    template = await slate_template(owner_engine)
    only = await dive(owner_engine, lab, device=device, slate=template, priority="low")
    frame = await capture(owner_engine, lab, only)
    await slate_presence(owner_engine, lab, frame, probability=None)

    (row,) = await _evaluation(owner_engine, capture_id=frame)
    numbers = (
        await rows(
            owner_engine,
            "SELECT d.number AS dive, c.number AS image, dev.number AS camera "
            "FROM captures c JOIN dives d ON d.id = c.dive_id "
            "JOIN devices dev ON dev.id = d.device_id WHERE c.id = :c",
            c=frame,
        )
    )[0]

    assert (row["dive_id"], row["image_id"], row["camera_id"]) == (
        numbers["dive"],
        numbers["image"],
        numbers["camera"],
    )
    assert (row["dive_uuid"], row["device_id"], row["is_canonical"]) == (
        only,
        device,
        True,
    )
    assert (row["dive_priority"], row["dive_has_slate_template"]) == ("low", True)
    assert (row["status"], row["probability"], row["predicted_slate"]) == (
        "decode_failed",
        None,
        None,
    )
    assert (row["model_name"], row["weights_sha256"][:8]) == (
        "slate-detector",
        "b8d377ba",
    )
    assert (row["core_version"], row["processor_version"]) == ("4.1.0", "0.1.2")
    assert (row["decode_config"], row["rectified"]) == ("production", True)
    assert (row["input_width"], row["input_height"]) == (1024, 768)
    assert row["predicted_at"] is not None and row["render"]["tta"] == "hflip"


async def test_a_detector_queued_answer_is_marked(owner_engine):
    lab = await tenant(owner_engine)
    only = await _dive(owner_engine, lab)
    queued = await capture(owner_engine, lab, only)
    person = await capture(owner_engine, lab, only)
    prediction = await slate_presence(owner_engine, lab, queued)
    await slate_label(owner_engine, lab, queued, completed=True, detected_by=prediction)
    await species(owner_engine, lab, person, content=SLATE)
    await slate_presence(owner_engine, lab, person)

    flags = {
        r["capture_id"]: r["slate_label_from_detector"]
        for r in await _evaluation(owner_engine)
    }

    assert flags == {queued: True, person: False}


# -- who reads it ------------------------------------------------------------------


async def test_a_research_login_reads_the_labs_evaluation_only(
    owner_engine, research_engine
):
    lab = await tenant(owner_engine, "lab")
    partner = await tenant(owner_engine, "partner")
    mine = await capture(owner_engine, lab, await _dive(owner_engine, lab))
    theirs = await capture(owner_engine, partner, await _dive(owner_engine, partner))
    for tenant_id, frame in ((lab, mine), (partner, theirs)):
        await species(owner_engine, tenant_id, frame, content=SLATE)
        await slate_presence(owner_engine, tenant_id, frame)

    async with research_engine.connect() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(partner)}
        )
        got = (
            await conn.execute(
                text("SELECT capture_id, human_label FROM slate_presence_evaluation")
            )
        ).all()
        predictions = (
            (
                await conn.execute(
                    text("SELECT capture_id FROM slate_presence_predictions")
                )
            )
            .scalars()
            .all()
        )
        current = (
            (await conn.execute(text("SELECT capture_id FROM current_slate_presence")))
            .scalars()
            .all()
        )

    assert [tuple(r) for r in got] == [(mine, "slate")]
    assert predictions == current == [mine]


async def test_a_research_login_cannot_write_predictions(owner_engine, research_engine):
    await tenant(owner_engine, "lab")

    with pytest.raises(DBAPIError, match="permission denied"):
        async with research_engine.begin() as conn:
            await conn.execute(text("DELETE FROM public.slate_presence_predictions"))


async def test_the_app_role_is_not_given_the_evaluation(app_engine):
    """A research view, not an API surface (0031's rule)."""
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM public.slate_presence_evaluation"))
