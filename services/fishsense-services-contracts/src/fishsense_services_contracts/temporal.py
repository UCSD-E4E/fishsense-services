"""How the orchestrator and the processor reach the shared Temporal.

Both sides must land in the same namespace to see each other's work, so the
connection's rules live here, with the queue names, rather than in each
service. From ``FISHSENSE_TEMPORAL_*``, validated at startup. Ported in shape
from fishsense-lite@a8b2c3bc fishsense_shared/temporal.py (`build_tls_config`,
`temporal_namespace`), with one rule tightened: the namespace is required.
"""

import dataclasses
import functools
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter, model_validator
from pydantic_core import to_json
from pydantic_settings import BaseSettings, SettingsConfigDict
from temporalio.api.common.v1 import Payload
from temporalio.client import TLSConfig
from temporalio.contrib.pydantic import PydanticJSONPlainPayloadConverter
from temporalio.converter import (
    CompositePayloadConverter,
    DataConverter,
    DefaultPayloadConverter,
    JSONPlainPayloadConverter,
)

__all__ = ["DATA_CONVERTER", "TemporalConnection", "connect_options"]


def _by_alias(value: Any) -> Any:
    """`value` with every dataclass and model dumped under its aliases. A
    pydantic model's serializer honors aliases itself; a stdlib dataclass's
    does not (pydantic_core infers its fields), so each one goes through a
    `TypeAdapter` of its own type, which reads its `Annotated` aliases."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _adapter(type(value)).dump_python(value, mode="json", by_alias=True)
    if isinstance(value, (list, tuple)):
        return [_by_alias(v) for v in value]
    if isinstance(value, dict):
        return {k: _by_alias(v) for k, v in value.items()}
    return value


@functools.lru_cache(maxsize=1024)
def _adapter(cls: type) -> TypeAdapter:
    return TypeAdapter(cls)


class _WireNamesJSONConverter(PydanticJSONPlainPayloadConverter):
    """temporalio's pydantic JSON converter, writing each field under its
    alias. Phase 2a renamed dives to missions (PLAN.md §9.26), and a renamed
    field's alias is its old name (`renamed.py`): Temporal keeps every payload
    it has seen, a processor deploys days apart from the orchestrator, and a
    rollback must read what this version wrote, so the JSON does not change --
    models and dataclasses alike. Reading takes either name. Only the wire
    uses the aliases: inside the code `model_dump()` is unchanged."""

    def to_payload(self, value: Any) -> Payload | None:
        return Payload(
            metadata={"encoding": self.encoding.encode()},
            data=to_json(_by_alias(value), by_alias=True),
        )


class _WireNamesPayloadConverter(CompositePayloadConverter):
    def __init__(self) -> None:
        wire = _WireNamesJSONConverter()
        super().__init__(
            *(
                wire if isinstance(c, JSONPlainPayloadConverter) else c
                for c in DefaultPayloadConverter.default_encoding_payload_converters
            )
        )


#: The one data converter every client and worker uses.
DATA_CONVERTER = DataConverter(payload_converter_class=_WireNamesPayloadConverter)


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
        # The contracts are pydantic models carrying UUIDs and datetimes, and
        # renamed fields keep their old names on the wire.
        "data_converter": DATA_CONVERTER,
    }
