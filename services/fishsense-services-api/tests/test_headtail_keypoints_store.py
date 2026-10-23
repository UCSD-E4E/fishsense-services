"""The SimCC keypoint replay's database side, read-only, on real Postgres:
the work is each canonical capture's current automatic prediction that found
a fish (``predicted``) with its SAM mask box and the render it was made on;
an abstention, a failed head/tail, a duplicate capture, a superseded
prediction, or one with no render is not; pages follow `seq`; a
tenant sees only its own (RLS)."""

from __future__ import annotations

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    capture,
    device,
    forget_identities,
    mission,
    tenant,
)
from test_automatic_results_store import BOX, _camera, _in, _predict, _predicted
from fishsense_services_api.headtail_keypoints_store import (
    keypoint_replay_frames,
    keypoint_replay_missions,
)


async def _mission(owner_engine, lab):
    return await mission(owner_engine, lab, device_id=await device(owner_engine, lab))


async def test_the_work_is_each_current_predicted_frame_with_its_box_and_render(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    fish, empty, failed, redone, unrendered = [
        await capture(owner_engine, lab, m) for _ in range(5)
    ]
    duplicate = await capture(owner_engine, lab, m, canonical=False)
    await _predict(app_engine, lab, m, _predicted(redone, mask_bbox=[1, 2, 3, 4]))
    await _predict(app_engine, lab, m, _predicted(fish), _predicted(redone),
                   _predicted(empty, status="no_detections", mask_bbox=None),
                   # a mask, so a box, but no head/tail: still not work
                   _predicted(failed, status="headtail_failed"),
                   _predicted(duplicate))  # fmt: skip
    await _predict(app_engine, lab, m, _predicted(unrendered), rendered=False)
    camera = await _camera(app_engine, lab, m)

    (work,) = await _in(app_engine, lab, keypoint_replay_missions)
    assert (work.mission_id, work.frames) == (m, 2)
    page = await _in(app_engine, lab, keypoint_replay_frames, m)
    by = {f.capture_id: f for f in page}
    assert set(by) == {fish, redone}
    assert by[redone].mask_bbox == list(BOX)  # the current prediction's box
    assert (by[fish].render_version, by[fish].camera_calibration_id) == (1, camera)


async def test_pages_follow_seq(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    m = await _mission(owner_engine, lab)
    caps = [await capture(owner_engine, lab, m) for _ in range(5)]
    await _predict(app_engine, lab, m, *[_predicted(c) for c in caps])
    first = await _in(app_engine, lab, keypoint_replay_frames, m, limit=3)
    rest = await _in(app_engine, lab, keypoint_replay_frames, m,
                     after_seq=first[-1].seq, limit=3)  # fmt: skip
    assert [f.capture_id for f in first + rest] == caps


async def test_a_tenant_sees_only_its_own(owner_engine, app_engine):
    lab, reef = await tenant(owner_engine), await tenant(owner_engine, "reef")
    m = await _mission(owner_engine, reef)
    await _predict(
        app_engine, reef, m, _predicted(await capture(owner_engine, reef, m))
    )
    assert await _in(app_engine, lab, keypoint_replay_missions) == []
    assert await _in(app_engine, lab, keypoint_replay_frames, m) == []
