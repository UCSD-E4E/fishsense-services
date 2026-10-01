"""The slate projects' labeling config, for the labeling-config reconcile
(`ops.labeling_configs`): v1's `_CONFIG_BY_SUFFIX` entry for this kind."""

from fishsense_services_orchestrator.slates.populate import (
    DIVE_SLATE_PROJECT_TITLE_SUFFIX,
    DIVE_SLATE_LABELING_CONFIG_XML,
)
from fishsense_services_orchestrator.ops.labeling_configs.registry import (
    LabelingConfig,
)

LABELING_CONFIGS = [
    LabelingConfig(
        kind="slate",
        title_suffix=DIVE_SLATE_PROJECT_TITLE_SUFFIX,
        xml=DIVE_SLATE_LABELING_CONFIG_XML,
    )
]
