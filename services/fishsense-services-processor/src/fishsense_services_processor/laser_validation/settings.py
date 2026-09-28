"""The auto-accept gate's thresholds, from ``FISHSENSE_LASER_AUTO_ACCEPT_*``.

Ported from fishsense-lite@77e8f8e5 services/fishsense-data-processing-
workflow-worker/src/fishsense_data_processing_workflow_worker/config.py (the
`laser_auto_accept.*` validators) and evaluate_laser_auto_accept_activity.py
(`_config_from_settings`). Every default is v1's measured one, and v1's
reason for settings holds: `enabled=false` is both the kill switch and the dark
run, and neither needs a code change. Values are coerced and then validated by
`AutoAcceptConfig`, as v1's Dynaconf validators did.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from fishsense_services_processor.laser_validation.auto_accept import (
    DEFAULT_CONFIG,
    AutoAcceptConfig,
)

__all__ = ["LaserAutoAcceptSettings"]


class LaserAutoAcceptSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FISHSENSE_LASER_AUTO_ACCEPT_")

    enabled: bool = DEFAULT_CONFIG.enabled
    audit_sample_rate: float = DEFAULT_CONFIG.audit_sample_rate
    min_predictions: int = DEFAULT_CONFIG.min_predictions
    min_inlier_fraction: float = DEFAULT_CONFIG.min_inlier_fraction
    max_perpendicular_px: float = DEFAULT_CONFIG.max_perpendicular_px
    max_along_line_z: float = DEFAULT_CONFIG.max_along_line_z

    def config(self) -> AutoAcceptConfig:
        """The gate config; raises ValueError for a value v1 would refuse."""
        return AutoAcceptConfig(
            enabled=self.enabled,
            min_predictions=self.min_predictions,
            min_inlier_fraction=self.min_inlier_fraction,
            max_perpendicular_px=self.max_perpendicular_px,
            max_along_line_z=self.max_along_line_z,
            audit_sample_rate=self.audit_sample_rate,
        )
