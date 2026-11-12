"""Every fish on the Fish page, each with how much we trust it (the owner,
2026-11-11: "we should show all fish on that page, automatic or otherwise.
we should just indicate how much we trust a measurement. otherwise, there is
no point in the auto measurements if they aren't available to the scientists
that need them").

The list (`observation_store.observation_page`) holds three kinds of fish:

* a person's (``trust = person``): every observation, as before;
* a Lite frame's automatic length (``automatic``): the current one, with the
  calibration's uncertainty, never on a frame the slate detector calls a
  slate (as the automatic lengths list leaves those out);
* a phone capture's server length (``automatic``): the current one, measured.

A frame a person measured is their fish alone: its automatic length is not
listed again. A result with no length (a refusal, a withdrawal, no fish) is
no fish.
"""

from __future__ import annotations

from datetime import timedelta

from depth_measure_seed import (  # noqa: F401  (forget_identities: a fixture)
    T0,
    calibrate,
    exec_,
    fish,
    forget_identities,
    measurement,
    tenant,
)
from test_automatic_results_store import _in, _measured
from test_phone_measurement_store import _measured as _phone_result
from test_phone_measurement_store import _phone
from test_slate_presence_store import _row as _slate_row

from fishsense_services_api.db import tenant_transaction
from fishsense_services_api.observation_store import observation_page
from fishsense_services_api.phone_measurement_store import record_phone_measurements
from fishsense_services_api.slate_presence_store import persist_slate_presence


async def _page(app_engine, tenant_id, **kwargs):
    async with tenant_transaction(app_engine, tenant_id) as conn:
        return await observation_page(
            conn, tenant_id, limit=kwargs.pop("limit", 50), **kwargs
        )


async def _record(app_engine, lab, *results):
    async with tenant_transaction(app_engine, lab) as conn:
        await record_phone_measurements(conn, lab, list(results))


async def test_a_phone_captures_server_length_is_an_automatic_fish(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    c, _ = await _phone(app_engine, lab)
    await _record(app_engine, lab, _phone_result(c, length_m=0.284))

    page = await _page(app_engine, lab)

    (found,) = page.observations
    assert (found.capture_id, found.length_m) == (c, 0.284)
    assert (found.trust, found.method) == ("automatic", "lidar")
    assert found.mission_number is not None
    assert page.total == 1


async def test_a_phone_result_without_a_length_is_no_fish(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    c, _ = await _phone(app_engine, lab)
    await _record(app_engine, lab, _phone_result(
        c, status="no_fish", length_m=None, snout_x=None, snout_y=None,
        fork_x=None, fork_y=None, snout_depth_m=None, fork_depth_m=None, mask=None,
    ))  # fmt: skip

    assert (await _page(app_engine, lab)).observations == []


async def test_a_phone_capture_a_person_measured_is_their_fish_alone(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    c, _ = await _phone(app_engine, lab, measured=True)
    await _record(app_engine, lab, _phone_result(c, length_m=0.5))

    (found,) = (await _page(app_engine, lab)).observations

    assert (found.trust, found.length_m) == ("person", 0.3)


async def test_a_lite_frames_automatic_length_is_an_automatic_fish(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    _, c = await _measured(owner_engine, app_engine, lab)

    (found,) = (await _page(app_engine, lab)).observations

    assert (found.capture_id, found.length_m) == (c, 0.42)
    assert (found.trust, found.method) == ("automatic", "laser")


async def test_a_lite_frame_a_person_measured_is_their_fish_alone(
    owner_engine, app_engine
):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    cal = await calibrate(owner_engine, lab, mission_id)
    await measurement(owner_engine, lab, c, await fish(owner_engine, lab), cal,
                      length_m=0.39)  # fmt: skip

    (found,) = (await _page(app_engine, lab)).observations

    assert (found.trust, found.length_m) == ("person", 0.39)


async def test_a_withdrawn_automatic_length_is_no_fish(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    await _measured(owner_engine, app_engine, lab)
    await exec_(owner_engine, "UPDATE automatic_measurements "
                "SET length_m = NULL, refusal = 'fish_model'")  # fmt: skip

    assert (await _page(app_engine, lab)).observations == []


async def test_a_slate_frame_is_no_fish(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    mission_id, c = await _measured(owner_engine, app_engine, lab)
    await _in(app_engine, lab, persist_slate_presence, mission_id, [_slate_row(c)])

    assert (await _page(app_engine, lab)).observations == []


async def test_all_three_are_listed_newest_first_and_counted(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    _, lite = await _measured(owner_engine, app_engine, lab)
    phone, _ = await _phone(app_engine, lab, at=T0 + timedelta(days=400))
    await _record(app_engine, lab, _phone_result(phone))
    person, _ = await _phone(app_engine, lab, measured=True,
                             at=T0 + timedelta(days=500))  # fmt: skip

    page = await _page(app_engine, lab)

    assert [(o.capture_id, o.trust) for o in page.observations][:2] == [
        (person, "person"),
        (phone, "automatic"),
    ]
    assert {o.capture_id for o in page.observations} == {lite, phone, person}
    assert page.total == 3
    assert (await _page(app_engine, lab, device="mobile")).total == 2


async def test_a_page_after_a_cursor_continues_across_kinds(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    phone, _ = await _phone(app_engine, lab, at=T0 + timedelta(days=400))
    await _record(app_engine, lab, _phone_result(phone))
    person, _ = await _phone(app_engine, lab, measured=True,
                             at=T0 + timedelta(days=500))  # fmt: skip

    (first,) = (await _page(app_engine, lab, limit=1)).observations
    (second,) = (
        await _page(app_engine, lab, limit=1, before=first.cursor)
    ).observations

    assert (first.capture_id, second.capture_id) == (person, phone)


async def test_a_trust_keeps_only_its_fish(owner_engine, app_engine):
    lab = await tenant(owner_engine)
    _, lite = await _measured(owner_engine, app_engine, lab)
    phone, _ = await _phone(app_engine, lab, at=T0 + timedelta(days=400))
    await _record(app_engine, lab, _phone_result(phone))
    person, _ = await _phone(app_engine, lab, measured=True,
                             at=T0 + timedelta(days=500))  # fmt: skip

    people = await _page(app_engine, lab, trust="person")
    models = await _page(app_engine, lab, trust="automatic")

    assert [o.capture_id for o in people.observations] == [person]
    assert {o.capture_id for o in models.observations} == {lite, phone}
    assert (people.total, models.total) == (1, 2)
