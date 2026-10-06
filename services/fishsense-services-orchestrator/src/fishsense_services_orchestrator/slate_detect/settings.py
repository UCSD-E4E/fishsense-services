"""The slate detector's switch.

New in v2. From ``FISHSENSE_SLATE_DETECTION_*``, read when the worker
starts:

* ``ENABLED`` (default **false**): the stage **ships disabled**. Off, the
  detect schedule is not created; the workflow and activities are still
  registered, so the parent can be run by hand. Turning it on also needs the
  processor's `FISHSENSE_SLATE_DETECTOR_SHA256`/`_SIZE` pin and the weights
  in `model-weights/slate-detector/q1/`.

The operating point (P(slate) >= 0.5) is the contract's
`SLATE_PRESENCE_THRESHOLD`, not a setting: stage 9's queue and the
automatic chain read the same frames as slate.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["SlateDetectionSettings"]


class SlateDetectionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FISHSENSE_SLATE_DETECTION_")

    enabled: bool = False
