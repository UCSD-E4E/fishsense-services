"""How the orchestrator and the processor reach the shared Temporal.

Both sides must land in the same namespace to see each other's work, so the
connection's rules live here, with the queue names, rather than in each
service. From ``FISHSENSE_TEMPORAL_*``, validated at startup. Ported in shape
from fishsense-lite@a8b2c3bc fishsense_shared/temporal.py (`build_tls_config`,
`temporal_namespace`), with one rule tightened: the namespace is required.
"""

from pathlib import Path
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from temporalio.client import TLSConfig
from temporalio.contrib.pydantic import pydantic_data_converter

__all__ = ["TemporalConnection", "connect_options"]


class TemporalConnection(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FISHSENSE_TEMPORAL_")

    address: str = "localhost:7233"
    #: Required, deliberately. OSS Temporal mTLS does not pin a client to a
    #: namespace, so a worker that omits it silently serves ``default`` (v1
    #: defaulted it, and said so).
    namespace: str
    #: mTLS is on when a client certificate is configured.
    client_cert: Path | None = None
    client_private_key: Path | None = None
    server_root_ca_cert: Path | None = None
    domain: str | None = None

    @model_validator(mode="after")
    def _cert_and_key_together(self) -> "TemporalConnection":
        if (self.client_cert is None) != (self.client_private_key is None):
            raise ValueError("client_cert and client_private_key must be set together")
        return self


def connect_options(settings: TemporalConnection) -> dict[str, Any]:
    """Keyword arguments for `Client.connect`."""
    tls: TLSConfig | bool = False
    if settings.client_cert is not None:
        tls = TLSConfig(
            client_cert=settings.client_cert.read_bytes(),
            client_private_key=settings.client_private_key.read_bytes(),
            server_root_ca_cert=(
                settings.server_root_ca_cert.read_bytes()
                if settings.server_root_ca_cert
                else None
            ),
            domain=settings.domain,
        )
    return {
        "target_host": settings.address,
        "namespace": settings.namespace,
        "tls": tls,
        # The contracts are pydantic models carrying UUIDs and datetimes.
        "data_converter": pydantic_data_converter,
    }
