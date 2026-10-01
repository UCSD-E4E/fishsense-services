"""Ported verbatim from fishsense-lite@a8b2c3bc
services/fishsense-api-workflow-worker/src/fishsense_api_workflow_worker/activities/
nas_errors.py.

Shared classification of Synology FileStation errors into retryable and
permanent.

Extracted from `stage_raw_bytes_for_dive_activity` when ingest needed the same
judgement. The rule it encodes is worth stating once: **retry/backoff belongs to
the Temporal retry policy on the activity call, never to an inner loop.** An
inner loop underneath Temporal's outer retry is what produced the
200×-per-file download storm that tripped the NAS auto-block (krg-infra#501).

So these helpers only *classify*. A permanent error becomes a non-retryable
`ApplicationError` so Temporal stops rescheduling something doomed; everything
else propagates untouched so the bounded jittered policy backs off and tries
again.

The code is recovered by string-parsing `DSMError`'s message because the
`synology-filestation` client doesn't expose it structurally yet (feedback
filed upstream). Delete `dsm_error_code` when it does.
"""

from __future__ import annotations

import re

from temporalio.exceptions import ApplicationError

# `type` on the non-retryable ApplicationError raised for a missing file. Must
# stay in step with `non_retryable_error_types` in `STAGE_RAW_RETRY_POLICY`
# (workflows/_retry_policies.py) — Temporal matches on this string, so a rename
# here silently restores retrying on doomed work.
NAS_FILE_NOT_FOUND_TYPE = "NasFileNotFound"

# FileStation codes that are *permanent*: retrying cannot help, so fail fast
# rather than burning the retry budget. 408 = "No such file or directory".
#
# Transient codes are deliberately absent and must stay that way — 502
# (shared download backend falling over), 407 (backend fail-closed) and 402
# (busy) are all routine and self-healing under backoff.
PERMANENT_DSM_CODES = frozenset({408})

__all__ = [
    "NAS_FILE_NOT_FOUND_TYPE",
    "PERMANENT_DSM_CODES",
    "dsm_error_code",
    "raise_if_permanent_dsm_error",
]


def dsm_error_code(exc: BaseException) -> int | None:
    """Best-effort extract the FileStation error code from a `DSMError`, whose
    message is `"Synology API error <code>"`. Returns None when the message
    doesn't carry one — an unrecognised error is treated as transient, which
    errs toward retrying rather than toward declaring a dive dead."""
    match = re.search(r"error\s+(\d+)", str(exc))
    return int(match.group(1)) if match else None


def raise_if_permanent_dsm_error(exc: BaseException, *, context: str) -> None:
    """Convert a permanent `DSMError` into a non-retryable `ApplicationError`.

    Returns normally when the error is transient (or unrecognised), leaving the
    caller to re-raise so Temporal's policy owns the backoff.

    `context` names what was being reached for — a path, a folder — because the
    resulting failure is what an operator reads in the Temporal UI, and
    "Synology 408" on its own doesn't say which file was missing.
    """
    code = dsm_error_code(exc)
    if code in PERMANENT_DSM_CODES:
        raise ApplicationError(
            f"NAS path not found (Synology {code}): {context}",
            type=NAS_FILE_NOT_FOUND_TYPE,
            non_retryable=True,
        ) from exc
