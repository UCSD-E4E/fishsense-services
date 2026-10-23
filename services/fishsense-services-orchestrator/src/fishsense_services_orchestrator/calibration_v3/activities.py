"""The replay's plan: the tenant's missions with a current automatic dot."""

from __future__ import annotations

from typing import Any, List, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

__all__ = ["CalibrationV3Activities"]


class CalibrationV3Activities:  # pylint: disable=too-few-public-methods
    def __init__(self, *, catalog: Any) -> None:
        #: `AutomaticResultsCatalog`.
        self._catalog = catalog

    @activity.defn(name="plan_calibration_v3_replay")
    async def plan_calibration_v3_replay(
        self, tenant: str, numbers: Optional[List[int]] = None
    ) -> dict:
        tenant_id = None
        for t in await self._catalog.member_tenants():
            if await self._catalog.tenant_slug(t) == tenant:
                tenant_id = t
        if tenant_id is None:
            raise ApplicationError(f"no tenant {tenant!r}", type="NoSuchTenant",
                                   non_retryable=True)  # fmt: skip
        missions = await self._catalog.missions_with_automatic_dots(tenant_id)
        if numbers:
            missions = [(m, n) for m, n in missions if n in set(numbers)]
        return {"tenant_id": str(tenant_id),
                "missions": [{"mission_id": str(m), "number": n} for m, n in missions]}  # fmt: skip
