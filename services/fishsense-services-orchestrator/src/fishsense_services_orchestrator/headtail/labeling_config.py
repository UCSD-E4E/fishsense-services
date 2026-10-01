"""The head_tail projects' labeling config, for the labeling-config reconcile
(`ops.labeling_configs`): v1's `_CONFIG_BY_SUFFIX` entry for this kind."""

from fishsense_services_orchestrator.headtail.labeling import (
    HEADTAIL_PROJECT_TITLE_SUFFIX,
    HEADTAIL_LABELING_CONFIG_XML,
)
from fishsense_services_orchestrator.ops.labeling_configs.registry import (
    LabelingConfig,
)

LABELING_CONFIGS = [
    LabelingConfig(
        kind="head_tail",
        title_suffix=HEADTAIL_PROJECT_TITLE_SUFFIX,
        xml=HEADTAIL_LABELING_CONFIG_XML,
    )
]
