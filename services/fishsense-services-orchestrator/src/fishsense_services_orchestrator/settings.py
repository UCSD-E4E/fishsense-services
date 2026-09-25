"""Runtime configuration, from ``FISHSENSE_*`` environment variables.

Validated at startup, so a misconfigured deployment fails to start rather than
failing on its first ingest. Secrets are ``SecretStr``. The NAS has its own
settings (`ingest.nas_frames.NasSettings`, ``FISHSENSE_NAS_*``).
"""

from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["DEFAULT_TASK_QUEUE", "OrchestratorSettings", "TemporalSettings"]

#: Not v1's ``fishsense_api_queue``: Temporal is shared, and until cutover a v1
#: worker polling the same queue would take v2's tasks (and the reverse).
DEFAULT_TASK_QUEUE = "fishsense_orchestrator"


class TemporalSettings(BaseSettings):
    """The Temporal connection, from ``FISHSENSE_TEMPORAL_*``."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_TEMPORAL_")

    address: str = "localhost:7233"
    #: Required, deliberately. OSS Temporal mTLS does not pin a client to a
    #: namespace, so a worker that omits it silently serves ``default``.
    namespace: str
    task_queue: str = DEFAULT_TASK_QUEUE
    #: mTLS is on when a client certificate is configured.
    client_cert: Path | None = None
    client_private_key: Path | None = None
    server_root_ca_cert: Path | None = None
    domain: str | None = None

    @model_validator(mode="after")
    def _cert_and_key_together(self) -> "TemporalSettings":
        if (self.client_cert is None) != (self.client_private_key is None):
            raise ValueError("client_cert and client_private_key must be set together")
        return self


class OrchestratorSettings(BaseSettings):
    """The orchestrator's database identity, from ``FISHSENSE_*``."""

    model_config = SettingsConfigDict(env_prefix="FISHSENSE_")

    #: DSN for the unprivileged app role -- the same role the API runs as.
    database_url: SecretStr
    #: The orchestrator's service principal. It acts for a tenant only through
    #: a membership granted to this ``sub`` (PLAN.md §9.11).
    orchestrator_sub: str
