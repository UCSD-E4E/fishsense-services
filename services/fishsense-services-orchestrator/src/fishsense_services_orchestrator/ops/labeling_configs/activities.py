"""Push each kind's current labeling config onto every per-dive project.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/reconcile_labeling_configs_activity.py.

The heal inside create-or-get only runs during populate, and populate stops
dispatching for a dive once it is fully populated: it reaches projects still
filling and never the finished ones, which are exactly the projects labelers
spend their time in. Observed 2026-07-21 after the Fish Model taxonomy swap:
the 11 species projects still in the populate cohort picked up the new choices,
while `082923_FishModels_FSL02 #58 - Species Labeling` (pid 274353, fully
populated, out of cohort) kept the old list.

So this walks the projects directly rather than riding a cohort: it lists the
workspace, matches each project's title suffix to the kind that owns it, and
delegates to the same `heal_labeling_config` populate uses -- drift compared
structurally (Label Studio reformats XML), no write when unchanged, a rejected
change logged and swallowed. No database access: every tenant's projects share
the one workspace, and a kind's config is the same for all of them.

v2 changes: the configs are declared by the kinds' slices
(`labeling_configs.registry`); Label Studio is reached through the one adapter,
whose calls back off through 429s; the heartbeat is `heartbeat_again`, never a
bare one (the adapter's rule).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from temporalio import activity

from fishsense_services_orchestrator.labels.label_studio import (
    LabelStudioClient,
    heartbeat_again,
)
from fishsense_services_orchestrator.labels.populate import heal_labeling_config
from fishsense_services_orchestrator.ops.labeling_configs.registry import (
    LabelingConfig,
    config_for_title,
)

__all__ = ["LabelingConfigActivities", "ReconcileLabelingConfigsResult"]


@dataclass
class ReconcileLabelingConfigsResult:
    """Counts for one reconcile pass."""

    scanned: int = 0
    healed: int = 0
    unchanged: int = 0
    unrecognized: int = 0


class LabelingConfigActivities:
    def __init__(
        self,
        *,
        label_studio_factory: Callable[[], LabelStudioClient],
        workspace: str,
        configs: Sequence[LabelingConfig],
    ) -> None:
        self._label_studio_factory = label_studio_factory
        self._workspace = workspace
        self._configs = tuple(configs)

    @activity.defn(name="reconcile_labeling_configs")
    async def reconcile_labeling_configs(self) -> ReconcileLabelingConfigsResult:
        """Converge every per-dive project onto its kind's current config."""
        ls = self._label_studio_factory()
        workspace_id = await ls.workspace_id(self._workspace)
        projects = await ls.projects(workspace_id)

        result = ReconcileLabelingConfigsResult()
        for project in projects:
            config = config_for_title(project.title, self._configs)
            if config is None:
                result.unrecognized += 1
                continue

            result.scanned += 1
            heartbeat_again()
            if await heal_labeling_config(ls, project, config.xml):
                result.healed += 1
                activity.logger.info(
                    "reconciled labeling config for project id=%s title=%r",
                    project.id,
                    project.title,
                )
            else:
                result.unchanged += 1

        activity.logger.info(
            "labeling-config reconcile: scanned=%d healed=%d unchanged=%d "
            "unrecognized=%d",
            result.scanned,
            result.healed,
            result.unchanged,
            result.unrecognized,
        )
        return result
