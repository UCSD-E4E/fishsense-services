"""The species projects' labeling config, for the labeling-config reconcile
(`ops.labeling_configs`): v1's `_CONFIG_BY_SUFFIX` entry for this kind."""

from fishsense_services_orchestrator.species.labeling import (
    SPECIES_PROJECT_TITLE_SUFFIX,
    SPECIES_LABELING_CONFIG_XML,
)
from fishsense_services_orchestrator.ops.labeling_configs.registry import (
    LabelingConfig,
)

LABELING_CONFIGS = [
    LabelingConfig(
        kind="species",
        title_suffix=SPECIES_PROJECT_TITLE_SUFFIX,
        xml=SPECIES_LABELING_CONFIG_XML,
    )
]
