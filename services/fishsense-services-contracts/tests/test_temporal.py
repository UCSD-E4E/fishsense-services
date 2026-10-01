"""How both sides reach the shared Temporal (moved from the orchestrator's
test_worker.py when the processor became the second user)."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from temporalio.contrib.pydantic import pydantic_data_converter

from fishsense_services_contracts.temporal import TemporalConnection, connect_options


def test_the_namespace_is_required(monkeypatch):
    """Without it a worker would silently serve ``default`` -- v1's lesson."""
    monkeypatch.delenv("FISHSENSE_TEMPORAL_NAMESPACE", raising=False)

    with pytest.raises(ValidationError, match="namespace"):
        TemporalConnection()


def test_a_client_cert_without_its_key_fails_at_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("FISHSENSE_TEMPORAL_NAMESPACE", "fishsense")
    monkeypatch.setenv("FISHSENSE_TEMPORAL_CLIENT_CERT", str(tmp_path / "c.pem"))

    with pytest.raises(ValidationError, match="together"):
        TemporalConnection()


def test_connect_options_carry_the_namespace_and_the_pydantic_converter():
    options = connect_options(TemporalConnection(namespace="fishsense"))

    assert options["target_host"] == "localhost:7233"
    assert options["namespace"] == "fishsense"
    assert options["data_converter"] is pydantic_data_converter
    assert options["tls"] is False


def test_tls_is_built_from_the_certificate_files(tmp_path: Path):
    for name in ("cert.pem", "key.pem", "ca.pem"):
        (tmp_path / name).write_bytes(name.encode())
    settings = TemporalConnection(
        namespace="fishsense",
        client_cert=tmp_path / "cert.pem",
        client_private_key=tmp_path / "key.pem",
        server_root_ca_cert=tmp_path / "ca.pem",
        domain="temporal.example",
    )

    tls = connect_options(settings)["tls"]

    assert tls.client_cert == b"cert.pem"
    assert tls.client_private_key == b"key.pem"
    assert tls.server_root_ca_cert == b"ca.pem"
    assert tls.domain == "temporal.example"
