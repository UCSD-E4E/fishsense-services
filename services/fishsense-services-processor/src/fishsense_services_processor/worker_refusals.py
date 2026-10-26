"""Which of an activity's failures fail a processor workflow's whole run.

A worker's refusal (no GPU, weights that cannot load) fails the run; any
other failure leaves its frame out. #364 widened each workflow's list from
the no-GPU refusal alone to the weights failures too, and that decision is
workflow code: a run in flight at the deploy whose frame had already been
left out for, say, `Sam3WeightsCorrupt` would raise on replay instead
(review of #364). So the wider list holds only for runs that reached it
under the new code, behind one `workflow.patched` marker, recorded the first
time a run looks a failure up.
"""

from __future__ import annotations

from temporalio import workflow

__all__ = ["WIDENED_PATCH", "refusals"]

WIDENED_PATCH = "worker-refusals-include-weights"


def refusals(widened: tuple[str, ...], legacy: tuple[str, ...]) -> tuple[str, ...]:
    """`widened` for a run under the new code, `legacy` (the list before
    #364) for one replaying an older history."""
    return widened if workflow.patched(WIDENED_PATCH) else legacy
