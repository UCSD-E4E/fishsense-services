"""The automatic-results track's switch.

New in v2. From ``FISHSENSE_AUTOMATIC_RESULTS_*``, read when the worker
starts:

* ``ENABLED`` (default **false**): off, the backlog schedule is not created;
  the workflows and activities are still registered, so a dive can be run by
  hand (`AutomaticResultsForMissionWorkflow`) -- what the validation harness
  does on the paper's dives. Schedules are created if missing and never
  updated in place, so turning it off again means deleting
  `automatic-results` as well;
* ``SPECIES_ENABLED`` (default **true**, decided 2026-10-15): BioCLIP
  zero-shot species on the automatic masks. It runs after lengths and its
  failure never costs a dive its lengths; off, the cohort holds no dive for
  species work. Needs the BioCLIP weights pinned in the processor's secret
  (``FISHSENSE_BIOCLIP_*``), which the converge's processor-secret sync
  writes (``ops.processor_secret_sync``).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["AutomaticResultsSettings"]


class AutomaticResultsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FISHSENSE_AUTOMATIC_RESULTS_")

    enabled: bool = False
    species_enabled: bool = True
