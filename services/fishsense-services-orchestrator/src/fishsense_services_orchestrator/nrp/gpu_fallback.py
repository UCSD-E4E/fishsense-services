"""GPU -> CPU fallback policy for the processor's GPU queue.

Ported from fishsense-lite@77e8f8e5 services/fishsense-api-workflow-worker/src/
fishsense_api_workflow_worker/activities/gpu_fallback.py. The policy is v1's,
verbatim; what changed is where its state lives.

`fishsense_processor_gpu` is served by **two** Deployments running the same
image in the same ``gpu`` role: one that requests ``nvidia.com/gpu: 1`` and one
that requests none and runs the same torch checkpoint on the CPU. Exactly one
of them is up at a time. This module decides which.

It exists because the GPU one can fail to start for reasons entirely outside
our control — NRP has no free Turing-or-newer card, our quota is exhausted, the
node pool is drained, the image tag is bad — and when it does, the queue never
drains. fishsense-core's models pick ``"cuda"`` when torch sees a GPU and
``"cpu"`` otherwise, so the CPU Deployment produces the *same predictions*,
just slowly. Slow predictions beat none: nothing in the pipeline may ever be
blocked on a GPU being available.

**The state lives in a ConfigMap** (v2 change). v1 kept it in annotations on
the GPU Deployment, but v2 *deletes* that Deployment when its queue is idle
(NRP deletes Deployments older than two weeks, so none is kept at zero), and
deleting it would erase the count -- the very count that must accumulate
across a multi-hour outage. A ConfigMap is its own object: deleting the
Deployment leaves it alone. It keeps what v1 chose annotations for:

* it outlives the orchestrator process (a counter that reset on every restart
  or slot converge would never reach the threshold);
* it is operator-visible and -editable, next to the thing it describes. Force
  a fallback with ``kubectl patch configmap fishsense-processor-gpu-fallback
  -p '{"data":{"gpu-start-failures":"3"}}'``, or end one early by removing
  ``gpu-fallback-until``.

The keys are v1's annotation names without the ``fishsense.e4e.ucsd.edu/``
prefix, which a ConfigMap key may not contain. A missing ConfigMap (the first
run, or one deleted by hand) reads as "no history", exactly as v1's missing
annotations did.

Why not a table: the state describes NRP, not a tenant, and the schema audit
allows the app role to write only tenant-scoped tables; a tenant-independent
table the orchestrator writes would be the schema's first exception to that
rule, for bookkeeping whose worst-case loss costs a few extra GPU attempts.

`decide` is pure, and deliberately so: the interesting behavior is a sequence
spanning hours (wedge, count, flip, hold, expire, probe) that cannot be
reproduced quickly against a real cluster. `tests/test_gpu_fallback.py` walks
the whole state machine in milliseconds.

Two asymmetries worth keeping:

* Counting is per *observation*, not per second of wedge — the wedge clock
  restarts each time a failure is counted, so one continuous outage counts once
  per parent firing (hourly) rather than instantly exhausting the budget.
* The fallback window **expires**. Without it, one bad afternoon on NRP would
  strand the pipeline on CPU inference permanently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Final, Mapping

_log = logging.getLogger(__name__)

#: The ConfigMap's data keys: v1's annotation names, less the domain prefix.
FAILURES_KEY: Final = "gpu-start-failures"
WEDGED_SINCE_KEY: Final = "gpu-wedged-since"
FALLBACK_UNTIL_KEY: Final = "gpu-fallback-until"

MODE_GPU: Final = "gpu"
MODE_CPU_FALLBACK: Final = "cpu_fallback"
#: Neither Deployment came up inside the start timeout. The caller should skip
#: this firing entirely rather than dispatch a child onto an unserved queue.
MODE_UNAVAILABLE: Final = "unavailable"


def _parse_timestamp(raw: str | None) -> datetime | None:
    """Parse a stored timestamp, treating a naive one as UTC.

    Naive matters: these are hand-editable, and a hand edit rarely includes an
    offset. Reading a naive stamp as local time would shift the fallback window
    by however far the box is from UTC.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        _log.warning("ignoring unparseable GPU-fallback timestamp %r", raw)
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class GpuState:
    """Fallback bookkeeping, as read from / written to its ConfigMap.

    The default is "no history": no failed starts recorded, no wedge in
    progress, not in fallback. Every malformed value degrades to this, which
    costs at most a few extra GPU attempts and never a crash.
    """

    failures: int = 0
    wedged_since: datetime | None = None
    fallback_until: datetime | None = None

    @classmethod
    def from_config_map(cls, data: Mapping[str, str] | None) -> GpuState:
        """Read state out of the ConfigMap's data, leniently.

        It is operator-editable by design, so a typo must not take the predict
        stage down — every unreadable field falls back to its default with a
        warning rather than raising.
        """
        data = data or {}
        raw_failures = data.get(FAILURES_KEY)
        failures = 0
        if raw_failures:
            try:
                failures = max(0, int(raw_failures))
            except ValueError:
                _log.warning("ignoring unparseable %s=%r", FAILURES_KEY, raw_failures)
        return cls(
            failures=failures,
            wedged_since=_parse_timestamp(data.get(WEDGED_SINCE_KEY)),
            fallback_until=_parse_timestamp(data.get(FALLBACK_UNTIL_KEY)),
        )

    def to_config_map(self) -> dict[str, str | None]:
        """The ConfigMap data patch for this state.

        A ``None`` value **removes** the key in a strategic-merge patch, which
        is why an empty state clears all three rather than writing "0" and two
        empty strings: on a healthy cluster the ConfigMap holds no fallback
        bookkeeping at all, so the presence of any of these keys is itself the
        signal that something went wrong.
        """
        return {
            FAILURES_KEY: str(self.failures) if self.failures else None,
            WEDGED_SINCE_KEY: _format_timestamp(self.wedged_since),
            FALLBACK_UNTIL_KEY: _format_timestamp(self.fallback_until),
        }


@dataclass(frozen=True)
class FallbackPolicy:
    """Tunables, resolved from the orchestrator's ``FISHSENSE_NRP_*``."""

    active_replicas: int = 1
    fallback_replicas: int = 1
    max_start_failures: int = 3
    wedge_grace: timedelta = timedelta(minutes=5)
    fallback_window: timedelta = timedelta(hours=3)


@dataclass(frozen=True)
class GpuDecision:
    """What to do now, and the state to write back."""

    mode: str
    gpu_replicas: int
    fallback_replicas: int
    state: GpuState
    reason: str


def _decide_wedged(
    state: GpuState, *, now: datetime, policy: FallbackPolicy
) -> GpuDecision:
    """The GPU Deployment wants pods and has none Ready. Count it, or flip.

    Split out of `decide` so each function reads as one decision rather than a
    ladder; the ordering here is the whole policy.
    """
    if state.wedged_since is None:
        # First sighting. A pod still pulling its image is not a failed start,
        # so only start the clock.
        return GpuDecision(
            MODE_GPU,
            policy.active_replicas,
            0,
            replace(state, wedged_since=now),
            "GPU worker has no ready pod yet; starting the grace clock",
        )

    if now - state.wedged_since < policy.wedge_grace:
        return GpuDecision(
            MODE_GPU,
            policy.active_replicas,
            0,
            state,
            "GPU worker still starting, inside the grace window",
        )

    failures = state.failures + 1
    if failures >= policy.max_start_failures:
        return GpuDecision(
            MODE_CPU_FALLBACK,
            0,
            policy.fallback_replicas,
            GpuState(
                failures=failures,
                wedged_since=None,
                fallback_until=now + policy.fallback_window,
            ),
            f"GPU worker failed to start {failures} times; "
            f"falling back to CPU inference for {policy.fallback_window}",
        )

    # Restart the clock so a continuous wedge counts once per observation
    # rather than on every poll.
    return GpuDecision(
        MODE_GPU,
        policy.active_replicas,
        0,
        GpuState(failures=failures, wedged_since=now),
        f"GPU worker failed to start ({failures}/{policy.max_start_failures}); "
        "retrying on the GPU",
    )


def decide(
    state: GpuState,
    *,
    now: datetime,
    gpu_ready: bool,
    gpu_wedged: bool,
    policy: FallbackPolicy,
) -> GpuDecision:
    """Choose which Deployment serves the GPU queue, and update the state.

    ``gpu_ready`` / ``gpu_wedged`` come from a single read of the GPU
    Deployment (see `scaling.readiness`). Both are False for a Deployment that
    doesn't exist -- in v2 the ordinary cold start, as zero replicas was in
    v1's -- which is neither success nor failure.
    """
    if state.fallback_until is not None:
        if now < state.fallback_until:
            return GpuDecision(
                MODE_CPU_FALLBACK,
                0,
                policy.fallback_replicas,
                state,
                f"in the CPU fallback window until {_format_timestamp(state.fallback_until)}",
            )
        # Expired. Clean slate, so the GPU gets a full budget of attempts
        # again — a transient NRP shortage must not strand us on CPU forever.
        return GpuDecision(
            MODE_GPU,
            policy.active_replicas,
            0,
            GpuState(),
            "CPU fallback window expired; probing the GPU worker again",
        )

    if gpu_ready:
        return GpuDecision(
            MODE_GPU,
            policy.active_replicas,
            0,
            GpuState(),
            "GPU worker is ready",
        )

    if gpu_wedged:
        return _decide_wedged(state, now=now, policy=policy)

    # Not there yet, or there with pods still being counted: neither a success
    # nor a failure. Stand it up and leave the bookkeeping alone.
    return GpuDecision(
        MODE_GPU,
        policy.active_replicas,
        0,
        state,
        "standing the GPU worker up",
    )
