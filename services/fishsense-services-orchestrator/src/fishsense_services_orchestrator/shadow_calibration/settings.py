"""The shadow calibration's switch (docs/shadow-size-constancy.md §3).

From ``FISHSENSE_SHADOW_CALIBRATION_*``, read when the worker starts:
``ENABLED`` (default **false**). Off, the hourly schedule is not created; the
workflows and activities stay registered, so one mission can be run by hand
(`ShadowCalibrationForMissionWorkflow`). Enabling it in production, and any
backfill, is the author's call. Schedules are created if missing and never
updated in place, so turning it off again means deleting
``shadow-calibration`` in Temporal as well.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ShadowCalibrationSettings"]


class ShadowCalibrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FISHSENSE_SHADOW_CALIBRATION_")

    enabled: bool = False
