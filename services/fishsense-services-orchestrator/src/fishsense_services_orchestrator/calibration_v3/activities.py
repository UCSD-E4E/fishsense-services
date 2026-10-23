"""The replay's plan -- the tenant's missions with a current automatic dot --
and, for calibration v4, each mission's slate template. Read-only."""

from __future__ import annotations

from typing import Any, List, Optional

from temporalio import activity
from temporalio.exceptions import ApplicationError

from fishsense_services_orchestrator.automatic_results.activities import (
    AutomaticTarget,
)

__all__ = ["CalibrationV3Activities"]


class CalibrationV3Activities:
    def __init__(self, *, catalog: Any, slates: Any = None, layout: Any = None) -> None:
        #: `AutomaticResultsCatalog`.
        self._catalog = catalog
        #: `SlateCatalog`, and the `ObjectLayout` that says where a template's
        #: PDF is staged.
        self._slates = slates
        self._layout = layout

    @activity.defn(name="resolve_mission_slate_template")
    async def resolve_mission_slate_template(
        self, target: AutomaticTarget
    ) -> Optional[dict]:
        """The mission's slate template and where its PDF is staged, or None
        if it has none that can pose a slate (calibration v4)."""
        template = await self._slates.mission_slate_template(
            target.tenant_id, target.mission_id
        )
        if template is None:
            return None
        return {"slate_template_id": str(template.id), "dpi": template.dpi,
                "reference_points": [list(p) for p in template.reference_points],
                "slate_pdf": self._layout.slate_pdf(target.tenant_id, template.id)
                .model_dump(mode="json")}  # fmt: skip

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
