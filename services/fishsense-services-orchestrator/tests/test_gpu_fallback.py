"""The GPU -> CPU fallback state machine.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/
tests/test_gpu_fallback.py. Test names, bodies and reasons are v1's.

`decide` is pure so the whole policy can be exercised without a cluster: the
interesting behavior is a multi-hour sequence (wedge, count, flip, hold,
expire, probe) that is impractical to reproduce against a real NRP Deployment
and impossible to reproduce quickly.

The rule these tests exist to protect is the top-level requirement: **the
pipeline must never be blocked on a GPU being available.** A GPU worker that
cannot schedule must eventually hand its queue to a CPU one, and must
eventually give the GPU another chance so a transient NRP shortage doesn't
strand us on slow CPU inference forever.

v2 change: the state lives in a ConfigMap, not in annotations on the GPU
Deployment -- v2 deletes that Deployment when it's idle, which would erase the
count (see `nrp.gpu_fallback`). The keys are v1's annotation names without the
domain prefix, since a ConfigMap key cannot hold a `/`. The round-trip tests
below are v1's, over the ConfigMap's data.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from fishsense_services_orchestrator.nrp.gpu_fallback import (
    FAILURES_KEY,
    FALLBACK_UNTIL_KEY,
    MODE_CPU_FALLBACK,
    MODE_GPU,
    WEDGED_SINCE_KEY,
    FallbackPolicy,
    GpuState,
    decide,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
POLICY = FallbackPolicy(
    active_replicas=1,
    fallback_replicas=1,
    max_start_failures=3,
    wedge_grace=timedelta(minutes=5),
    fallback_window=timedelta(hours=3),
)


def _decide(state, *, now=NOW, ready=False, wedged=False, policy=POLICY):
    return decide(state, now=now, gpu_ready=ready, gpu_wedged=wedged, policy=policy)


# --- cold start / happy path -------------------------------------------------


def test_cold_deployment_scales_the_gpu_up_and_touches_no_state():
    """First firing of a quiet hour: in v2 the Deployment doesn't exist, so it
    is neither ready nor wedged. Stand it up and decide nothing."""
    decision = _decide(GpuState())
    assert decision.mode == MODE_GPU
    assert (decision.gpu_replicas, decision.fallback_replicas) == (1, 0)
    assert decision.state == GpuState()


def test_ready_gpu_clears_a_partial_failure_history():
    """A GPU that comes back must not carry old failures toward the threshold
    — otherwise unrelated blips across days eventually trip the fallback."""
    decision = _decide(
        GpuState(failures=2, wedged_since=NOW - timedelta(hours=1)), ready=True
    )
    assert decision.mode == MODE_GPU
    assert decision.gpu_replicas == 1
    assert decision.state == GpuState()


# --- counting failed starts --------------------------------------------------


def test_first_wedge_observation_only_starts_the_grace_clock():
    """A pod that is merely still pulling its image is not a failed start."""
    decision = _decide(GpuState(), wedged=True)
    assert decision.state.failures == 0
    assert decision.state.wedged_since == NOW
    assert decision.mode == MODE_GPU


def test_wedge_inside_the_grace_window_does_not_count():
    state = GpuState(wedged_since=NOW - timedelta(minutes=4))
    decision = _decide(state, wedged=True)
    assert decision.state.failures == 0
    assert decision.state.wedged_since == state.wedged_since


def test_wedge_past_the_grace_window_counts_once_and_restarts_the_clock():
    """The clock restart is what makes this once *per observation* rather than
    once per second of a continuous wedge."""
    decision = _decide(GpuState(wedged_since=NOW - timedelta(minutes=6)), wedged=True)
    assert decision.state.failures == 1
    assert decision.state.wedged_since == NOW
    assert decision.mode == MODE_GPU
    assert decision.gpu_replicas == 1


def test_below_the_threshold_the_gpu_keeps_being_retried():
    decision = _decide(
        GpuState(failures=1, wedged_since=NOW - timedelta(minutes=30)), wedged=True
    )
    assert decision.state.failures == 2
    assert decision.mode == MODE_GPU
    assert decision.state.fallback_until is None


# --- the flip ----------------------------------------------------------------


def test_reaching_the_threshold_flips_to_the_cpu_fallback():
    decision = _decide(
        GpuState(failures=2, wedged_since=NOW - timedelta(minutes=30)), wedged=True
    )
    assert decision.mode == MODE_CPU_FALLBACK
    assert decision.gpu_replicas == 0
    assert decision.fallback_replicas == 1
    assert decision.state.fallback_until == NOW + POLICY.fallback_window


def test_inside_the_fallback_window_the_gpu_stays_down():
    """Held even if the GPU Deployment would now report ready — the whole
    point of the window is not to thrash back and forth."""
    state = GpuState(failures=3, fallback_until=NOW + timedelta(hours=1))
    decision = _decide(state, ready=True)
    assert decision.mode == MODE_CPU_FALLBACK
    assert (decision.gpu_replicas, decision.fallback_replicas) == (0, 1)
    assert decision.state == state


def test_an_expired_window_probes_the_gpu_again_with_a_clean_slate():
    """Without this the first sustained GPU outage would strand the pipeline on
    slow CPU inference permanently."""
    decision = _decide(GpuState(failures=3, fallback_until=NOW - timedelta(minutes=1)))
    assert decision.mode == MODE_GPU
    assert (decision.gpu_replicas, decision.fallback_replicas) == (1, 0)
    assert decision.state == GpuState()


def test_the_probe_can_fail_all_the_way_back_into_fallback():
    """Full round trip: expire -> probe -> wedge repeatedly -> fallback again.

    Pins the real cost of a re-probe, which is one observation more than
    `max_start_failures`: from a clean slate the first sighting only starts the
    grace clock. At the hourly parent cadence a fresh GPU outage therefore
    takes ~4 firings to reach CPU inference.
    """
    state = GpuState(failures=3, fallback_until=NOW - timedelta(minutes=1))
    now = NOW
    state = _decide(state, now=now).state
    assert state == GpuState()

    observations = 0
    while True:
        now += timedelta(hours=1)
        observations += 1
        decision = _decide(state, now=now, wedged=True)
        state = decision.state
        if decision.mode == MODE_CPU_FALLBACK:
            break
        assert observations <= POLICY.max_start_failures + 1, "never fell back"

    assert observations == POLICY.max_start_failures + 1
    assert state.fallback_until == now + POLICY.fallback_window


def test_replica_counts_come_from_the_policy():
    policy = FallbackPolicy(
        active_replicas=3,
        fallback_replicas=2,
        max_start_failures=1,
        wedge_grace=timedelta(0),
        fallback_window=timedelta(hours=1),
    )
    assert _decide(GpuState(), policy=policy).gpu_replicas == 3
    flipped = _decide(
        GpuState(wedged_since=NOW - timedelta(minutes=1)),
        wedged=True,
        policy=policy,
    )
    assert flipped.mode == MODE_CPU_FALLBACK
    assert flipped.fallback_replicas == 2


# --- ConfigMap round trip (v1: annotation round trip) --------------------------


def test_state_round_trips_through_the_config_map():
    state = GpuState(
        failures=2,
        wedged_since=NOW - timedelta(minutes=10),
        fallback_until=NOW + timedelta(hours=2),
    )
    assert GpuState.from_config_map(state.to_config_map()) == state


def test_empty_state_clears_every_key():
    """Cleared, not written as "0"/"" — so `kubectl describe configmap` on a
    healthy cluster shows no fallback bookkeeping at all."""
    data = GpuState().to_config_map()
    assert set(data) == {FAILURES_KEY, WEDGED_SINCE_KEY, FALLBACK_UNTIL_KEY}
    assert all(value is None for value in data.values())


def test_the_keys_are_legal_config_map_keys():
    """v2: v1's annotation names carried a `domain/` prefix, which a
    ConfigMap key may not; the apiserver would refuse the write."""
    for key in (FAILURES_KEY, WEDGED_SINCE_KEY, FALLBACK_UNTIL_KEY):
        assert re.fullmatch(r"[-._a-zA-Z0-9]+", key), key


def test_missing_data_reads_as_the_empty_state():
    assert GpuState.from_config_map(None) == GpuState()
    assert GpuState.from_config_map({}) == GpuState()
    assert GpuState.from_config_map({"unrelated": "x"}) == GpuState()


@pytest.mark.parametrize(
    "data",
    [
        {FAILURES_KEY: "not-a-number"},
        {WEDGED_SINCE_KEY: "yesterday"},
        {FALLBACK_UNTIL_KEY: ""},
        {FAILURES_KEY: "-4"},
    ],
)
def test_malformed_data_degrades_to_the_default_rather_than_raising(data):
    """These are hand-editable by operators (`kubectl edit configmap`), so a
    typo must not take the predict stage down. It degrades to "no history",
    which costs at most a few extra GPU attempts."""
    assert GpuState.from_config_map(data) == GpuState()


def test_naive_timestamps_are_read_as_utc():
    """A hand edit won't always include the offset. Reading a naive stamp as
    local time would shift the window by hours."""
    state = GpuState.from_config_map({FALLBACK_UNTIL_KEY: "2026-08-25T15:00:00"})
    assert state.fallback_until == datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
