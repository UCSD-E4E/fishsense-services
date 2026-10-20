"""Only the corrective optic enters the stages that rectify (PLAN.md §4.9, 2b-2).

Every stage that rectifies a raw frame -- laser 0.1 and prediction, species 2,
head/tail 5.1, slate presence, the automatic track -- does it with pinhole
maths, and selects its missions through `camera_sql.RECTIFIABLE_MISSION`. The
owner's decision (2026-10-20): that holds only behind the corrective optic. A
flat port is axial; a dome is pinhole-like only when centred, which nothing
checks; an unknown port is not yet any of them. So the one predicate admits a
mission only when its port is the corrective optic, and every one of those
stages inherits it.
"""

import pytest
from sqlalchemy import text

from depth_measure_seed import device, mission, tenant
from fishsense_services_api.camera_sql import RECTIFIABLE_MISSION


async def _rectifiable(owner_engine, mission_id) -> bool:
    async with owner_engine.connect() as conn:
        return (
            await conn.execute(
                text(f"SELECT {RECTIFIABLE_MISSION} FROM missions d WHERE d.id = :m"),
                {"m": mission_id},
            )
        ).scalar_one()


@pytest.mark.parametrize(
    "port, admitted",
    [("corrective_optic", True), ("flat", False), ("dome", False), ("unknown", False)],
)
async def test_only_the_corrective_optic_is_rectifiable(owner_engine, port, admitted):
    lab = await tenant(owner_engine)
    pinhole = await device(owner_engine, lab)
    m = await mission(owner_engine, lab, device_id=pinhole, port=port)

    assert await _rectifiable(owner_engine, m) is admitted


async def test_the_corrective_optic_still_needs_a_pinhole_camera(owner_engine):
    lab = await tenant(owner_engine)
    axial = await device(owner_engine, lab, model="axial_refractive")
    m = await mission(owner_engine, lab, device_id=axial)

    assert await _rectifiable(owner_engine, m) is False
