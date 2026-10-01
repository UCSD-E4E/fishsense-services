"""The laser projects' labeling config, for the labeling-config reconcile
(`ops.labeling_configs`): v1's `_CONFIG_BY_SUFFIX` entry for this kind."""

from fishsense_services_orchestrator.laser.annotations import (
    LASER_PROJECT_TITLE_SUFFIX,
    LASER_LABELING_CONFIG_XML,
)
from fishsense_services_orchestrator.ops.labeling_configs.registry import (
    LabelingConfig,
)

LABELING_CONFIGS = [
    LabelingConfig(
        kind="laser",
        title_suffix=LASER_PROJECT_TITLE_SUFFIX,
        xml=LASER_LABELING_CONFIG_XML,
    )
]
