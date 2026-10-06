"""The automatic-results track's switch.

New in v2. From ``FISHSENSE_AUTOMATIC_RESULTS_*``, read when the worker
starts:

* ``ENABLED`` (default **false**): off, the backlog schedule is not created;
  the workflows and activities are still registered, so a dive can be run by
  hand (`AutomaticResultsForDiveWorkflow`) -- what the validation harness
  does on the paper's dives. Schedules are created if missing and never
  updated in place, so turning it off again means deleting
  `automatic-results` as well.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["AutomaticResultsSettings"]


class AutomaticResultsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FISHSENSE_AUTOMATIC_RESULTS_")

    enabled: bool = False
