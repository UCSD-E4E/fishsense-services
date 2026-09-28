"""The species pre-annotation stage's switch and threshold.

New in v2 (no v1 counterpart). From ``FISHSENSE_SPECIES_PREDICTION_*``, read
when the worker starts:

* ``ENABLED`` (default **false**): the stage **ships disabled**. It is turned
  on only after an accuracy evaluation of BioCLIP on FishSense frames. Off,
  the predict schedule is not created and species populate seeds no model
  prediction, and the backfill attaches nothing; the workflows and
  activities are still registered, so the parent can be run by hand, storing
  predictions no labeler sees (an evaluation's mode);
* ``OTHER_THRESHOLD`` (default 0.5): below this top-1 probability the
  suggestion is "Other (Identifiable but Nontarget)" rather than the top-1
  species. Conservative on purpose -- a wrong target species is the costlier
  suggestion to show, since it anchors the labeler on a plausible name -- and
  **to be tuned by that evaluation**. It is applied when a task is seeded, not
  when the model runs, so a new value needs no re-prediction.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["DEFAULT_OTHER_THRESHOLD", "SpeciesPredictionSettings"]

#: See the module docstring: an even chance before a target species is named.
DEFAULT_OTHER_THRESHOLD = 0.5


class SpeciesPredictionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FISHSENSE_SPECIES_PREDICTION_")

    enabled: bool = False
    other_threshold: float = Field(default=DEFAULT_OTHER_THRESHOLD, gt=0.0, le=1.0)
