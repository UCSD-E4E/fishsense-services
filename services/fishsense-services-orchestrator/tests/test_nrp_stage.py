"""The NRP stage as the worker builds it: off unless configured.

The stage's activities are built at the worker's startup from
``FISHSENSE_NRP_*``. Without a kubeconfig -- locally, in CI, in e2e -- they are
no-ops and the processor is whatever compose runs (v1's rule); with one, the
config (and the manifests) resolve at startup, so a bad one fails the worker
there rather than at the first wake.
"""

from __future__ import annotations

import pytest

from fishsense_services_orchestrator.nrp.stage import STAGE
from fishsense_services_orchestrator.registry import Deps

from ._nrp import MANIFEST_DIR

DEPS = Deps(engine=None, sub="service:fishsense-orchestrator")


def _config_of(activities):
    (instance,) = {a.__self__ for a in activities}
    return instance.config


def test_without_a_kubeconfig_the_activities_are_no_ops(monkeypatch):
    monkeypatch.delenv("FISHSENSE_NRP_KUBECONFIG_PATH", raising=False)
    assert _config_of(STAGE.build_activities(DEPS)) is None


def test_with_one_the_config_resolves_at_startup(monkeypatch):
    monkeypatch.setenv("FISHSENSE_NRP_KUBECONFIG_PATH", "/run/secrets/nrp")
    monkeypatch.setenv("FISHSENSE_NRP_NAMESPACE", "e4e-fishsense")
    monkeypatch.setenv("FISHSENSE_NRP_IMAGE_TAG", "v2.0.0")
    monkeypatch.setenv("FISHSENSE_NRP_MANIFEST_DIR", str(MANIFEST_DIR))

    config = _config_of(STAGE.build_activities(DEPS))

    assert config.namespace == "e4e-fishsense"


def test_a_bad_config_fails_the_startup(monkeypatch):
    monkeypatch.setenv("FISHSENSE_NRP_KUBECONFIG_PATH", "/run/secrets/nrp")
    monkeypatch.delenv("FISHSENSE_NRP_NAMESPACE", raising=False)
    with pytest.raises(ValueError):
        STAGE.build_activities(DEPS)


def test_the_stage_builds_every_wake_and_the_sweeper(monkeypatch):
    monkeypatch.delenv("FISHSENSE_NRP_KUBECONFIG_PATH", raising=False)
    names = {
        a.__temporal_activity_definition.name for a in STAGE.build_activities(DEPS)
    }
    assert names == {
        "ensure_per_image_processor_running",
        "ensure_light_processor_running",
        "ensure_gpu_processor_running",
        "tear_down_idle_processors",
    }
