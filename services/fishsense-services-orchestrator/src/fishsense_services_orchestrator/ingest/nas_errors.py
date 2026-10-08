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

**What "missing" looks like.** synology-filestation 0.10.0 raises typed errors,
not `DSMError`, for a path that isn't there: `NoSuchFile` (over SMB, and for
FileStation codes 403/414/415), and `PermissionDenied` for code 408 -- the code
v1 saw for a missing file, so it still counts. Only unmapped codes stay
`DSMError`. So callers catch the base `FileStationError` and ask
`is_nas_not_found`; catching `DSMError` alone let a missing file through as an
outage, retried until the budget ran out. Every error carries its code as
`.code`; the message is parsed only as a fallback.
"""

from __future__ import annotations

import re

from synology_filestation import NoSuchFile
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
    "is_nas_not_found",
    "raise_if_permanent_dsm_error",
]


def dsm_error_code(exc: BaseException) -> int | None:
    """The FileStation error code: the error's own `.code`, else parsed from a
    message like `"Synology API error <code>"`. Returns None when neither
    carries one — an unrecognised error is treated as transient, which
    errs toward retrying rather than toward declaring a dive dead."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    match = re.search(r"error\s+(\d+)", str(exc))
    return int(match.group(1)) if match else None


def is_nas_not_found(exc: BaseException) -> bool:
    """True iff ``exc`` says the path isn't on the NAS: a `NoSuchFile`, or any
    error carrying a permanent code (408)."""
    return isinstance(exc, NoSuchFile) or dsm_error_code(exc) in PERMANENT_DSM_CODES


def raise_if_permanent_dsm_error(exc: BaseException, *, context: str) -> None:
    """Convert a missing-path error into a non-retryable `ApplicationError`.

    Returns normally when the error is transient (or unrecognised), leaving the
    caller to re-raise so Temporal's policy owns the backoff.

    `context` names what was being reached for — a path, a folder — because the
    resulting failure is what an operator reads in the Temporal UI, and
    "Synology 408" on its own doesn't say which file was missing.
    """
    if is_nas_not_found(exc):
        code = dsm_error_code(exc)
        reason = f"Synology {code}" if code is not None else type(exc).__name__
        raise ApplicationError(
            f"NAS path not found ({reason}): {context}",
            type=NAS_FILE_NOT_FOUND_TYPE,
            non_retryable=True,
        ) from exc
