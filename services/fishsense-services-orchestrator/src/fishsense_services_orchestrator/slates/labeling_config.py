"""The slate projects' labeling config, for the labeling-config reconcile
(`ops.labeling_configs`): v1's `_CONFIG_BY_SUFFIX` entry for this kind."""

from fishsense_services_orchestrator.slates.populate import (
    MISSION_SLATE_PROJECT_TITLE_SUFFIX,
    MISSION_SLATE_LABELING_CONFIG_XML,
)
from fishsense_services_orchestrator.ops.labeling_configs.registry import (
    LabelingConfig,
)

LABELING_CONFIGS = [
    LabelingConfig(
        kind="slate",
        title_suffix=MISSION_SLATE_PROJECT_TITLE_SUFFIX,
        xml=MISSION_SLATE_LABELING_CONFIG_XML,
    )
]
