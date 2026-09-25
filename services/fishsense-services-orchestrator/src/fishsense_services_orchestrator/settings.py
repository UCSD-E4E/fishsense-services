"""Runtime configuration, from ``FISHSENSE_*`` environment variables.

Validated at startup, so a misconfigured deployment fails to start rather than
failing on its first ingest. Secrets are ``SecretStr``. The NAS has its own
settings (`ingest.nas_frames.NasSettings`, ``FISHSENSE_NAS_*``).
"""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fishsense_services_contracts.temporal import TemporalConnection

__all__ = ["DEFAULT_TASK_QUEUE", "OrchestratorSettings", "TemporalSettings"]

#: Not v1's ``fishsense_api_queue``: Temporal is shared, and until cutover a v1
#: worker polling the same queue would take v2's tasks (and the reverse).
DEFAULT_TASK_QUEUE = "fishsense_orchestrator"


class TemporalSettings(TemporalConnection):
    """The shared Temporal connection (``FISHSENSE_TEMPORAL_*``, see
    ``fishsense_services_contracts.temporal``), plus the orchestrator's queue."""

    task_queue: str = DEFAULT_TASK_QUEUE


class OrchestratorSettings(BaseSettings):
    """The orchestrator's database identity, from ``FISHSENSE_*``."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_")

    #: DSN for the unprivileged app role -- the same role the API runs as.
    database_url: SecretStr
    #: The orchestrator's service principal. It acts for a tenant only through
    #: a membership granted to this ``sub`` (PLAN.md §9.11).
    orchestrator_sub: str
