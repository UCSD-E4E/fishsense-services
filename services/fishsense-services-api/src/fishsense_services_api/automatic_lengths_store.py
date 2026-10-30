"""Automatic lengths, a page at a time (the owner, 2026-10-24).

New in v2 (no v1 counterpart). The automatic chain's lengths were readable
from the API only one capture at a time (`automatic_review_store.capture_frame`)
or as a mission's counts; the list lived in ``automatic_results_export``, for
the research role. This reads that list for a tenant, or one mission of it,
with what a reader needs to judge each length:

* the export's exclusions, unchanged: only current lengths (0064: on a
  label-free calibration), and no frame the slate detector calls a slate
  (0043, `slate_presence_store.detector_slate_frame`);
* the human length on the same frame, when the frame has exactly one
  laser-measured fish (as the mission run page compares them), and whether the
  mission has any current human length (the export's ``dive_human_measured``);
* each kind's review (0063): the latest review that counts -- the one that
  decided the frame's label -- and whether a newer one waits to be backed.

Why not read the export itself: it is a view over
``current_automatic_measurements``, whose ``DISTINCT ON (tenant_id,
capture_id)`` Postgres can narrow by capture id but not by mission. In prod
(2026-10-24, 15,401 lengths in 64 missions) a mission's rows took 4.5 s through
the export and 12.9 s by capture id, against 0.27 s from the current view by
capture id. So a page is chosen first as capture ids, cheaply, from the
append-only tables, and only those captures are read through the current
views. A chosen capture whose current length turns out to be gone (refused,
superseded, a slate) is skipped, so a page can hold fewer rows than its limit;
``next`` is where the next page starts, None at the end.

Runs in a tenant transaction, under RLS.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.laser_colour_store import (
    dot_colour_columns,
    dot_colour_join,
)
from fishsense_services_api.review_store import COUNTS_SQL
from fishsense_services_api.slate_presence_store import detector_slate_frame
from fishsense_services_api.species_length import (
    SpeciesReference,
    species_length_flag,
)

__all__ = [
    "AutomaticLength",
    "AutomaticLengthPage",
    "KindReview",
    "automatic_lengths",
    "parse_cursor",
]

#: The scientific name in a species choice, "Fish, Hogfish (Lachnolaimus
#: maximus)": the last parenthesised part.
_SCIENTIFIC = r"substring(s.predicted_choice from '\(([^()]*)\)\s*$')"

#: A page's captures: cheap, from the append-only tables (any length ever
#: written; the current views then decide).
_CHOOSE_SQL = """
    SELECT c.id, d.number AS mission_number, c.number AS capture_number
    FROM captures c
    JOIN missions d ON d.tenant_id = c.tenant_id AND d.id = c.mission_id
    WHERE c.tenant_id = :t
      AND (CAST(:mission AS uuid) IS NULL OR c.mission_id = :mission)
      AND (CAST(:after_m AS bigint) IS NULL
           OR (d.number, c.number) > (:after_m, :after_c))
      AND EXISTS (
          SELECT 1 FROM automatic_measurements am
          WHERE am.tenant_id = c.tenant_id AND am.capture_id = c.id
            AND am.length_m IS NOT NULL)
      AND (CAST(:species AS text) IS NULL OR EXISTS (
          SELECT 1 FROM automatic_species_predictions sp
          WHERE sp.tenant_id = c.tenant_id AND sp.capture_id = c.id
            AND sp.predicted_choice LIKE :species_like))
    ORDER BY d.number, c.number
    LIMIT :limit
"""

#: The chosen captures' current lengths, read through the current views by
#: capture id, which they can narrow by.
_ROWS_SQL = f"""
    WITH m AS MATERIALIZED (
        SELECT * FROM current_automatic_measurements
        WHERE tenant_id = :t AND capture_id = ANY(CAST(:ids AS uuid[]))
          AND length_m IS NOT NULL
    ),
    s AS MATERIALIZED (
        SELECT * FROM current_automatic_species_predictions
        WHERE tenant_id = :t AND capture_id = ANY(CAST(:ids AS uuid[]))
    ),
    -- The page's missions with a current human length. current_measurements
    -- is narrowed by capture id (its DISTINCT ON key), not by mission, so it
    -- is handed the missions' measured captures, from the raw table.
    human_missions AS MATERIALIZED (
        SELECT DISTINCT hc.mission_id
        FROM current_measurements hm
        JOIN captures hc ON hc.tenant_id = hm.tenant_id AND hc.id = hm.capture_id
        WHERE hm.tenant_id = :t AND hm.capture_id = ANY(ARRAY(
            SELECT mm.capture_id FROM measurements mm
            JOIN captures mc ON mc.tenant_id = mm.tenant_id AND mc.id = mm.capture_id
            WHERE mm.tenant_id = :t AND mc.mission_id IN (
                SELECT pc.mission_id FROM captures pc
                WHERE pc.tenant_id = :t AND pc.id = ANY(CAST(:ids AS uuid[])))))
    )
    SELECT d.number AS mission_number, c.id AS capture_id,
           c.number AS capture_number, c.captured_at,
           m.length_m, m.depth_m, m.calibration_source,
           m.length_m * calibration_sd_at(alc.scale_sd_by_range, alc.scale_sd, m.depth_m)
                           AS length_sd_m,
           h.laser_confidence, {dot_colour_columns("h")}, h.sam_score,
           s.predicted_choice AS species, {_SCIENTIFIC} AS scientific_name,
           s.top1_probability AS species_probability, s.margin AS species_margin,
           sr.max_length_m AS species_max_length_m, sr.log_median AS species_log_median,
           sr.log_mad AS species_log_mad,
           (SELECT CASE WHEN count(*) = 1 THEN max(o.length_m) END
            FROM observations o
            WHERE o.tenant_id = c.tenant_id AND o.capture_id = c.id
              AND o.method = 'laser') AS human_length_m,
           d.id IN (SELECT mission_id FROM human_missions) AS mission_human_measured
    FROM m
    JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
    JOIN missions d ON d.tenant_id = c.tenant_id AND d.id = c.mission_id
    JOIN automatic_head_tail_predictions h
      ON h.tenant_id = m.tenant_id AND h.id = m.automatic_head_tail_prediction_id
    LEFT JOIN automatic_laser_calibrations alc
      ON alc.tenant_id = m.tenant_id AND alc.id = m.automatic_laser_calibration_id
    {dot_colour_join("h")}
    LEFT JOIN s ON s.capture_id = m.capture_id
     AND s.automatic_head_tail_prediction_id = m.automatic_head_tail_prediction_id
    LEFT JOIN species_length_references sr ON sr.scientific_name = {_SCIENTIFIC}
    WHERE NOT {detector_slate_frame()}
      AND (CAST(:species AS text) IS NULL OR {_SCIENTIFIC} = :species)
    ORDER BY d.number, c.number
"""

#: Each capture's reviews by kind: the latest that counts (0063's rule,
#: `review_store.COUNTS_SQL`), and whether a newer one is pending.
_REVIEWS_SQL = f"""
    WITH r AS (
        SELECT r.capture_id, r.kind, r.action, r.reason, r.seq,
               {COUNTS_SQL} AS counts
        FROM reviews r
        WHERE r.tenant_id = :t AND r.capture_id = ANY(CAST(:ids AS uuid[]))
    ),
    decided AS (
        SELECT DISTINCT ON (capture_id, kind) capture_id, kind, action, reason, seq
        FROM r WHERE counts ORDER BY capture_id, kind, seq DESC
    )
    SELECT k.capture_id, k.kind, d.action, d.reason,
           EXISTS (SELECT 1 FROM r p
                   WHERE p.capture_id = k.capture_id AND p.kind = k.kind
                     AND NOT p.counts AND p.seq > coalesce(d.seq, 0)) AS pending
    FROM (SELECT DISTINCT capture_id, kind FROM r) k
    LEFT JOIN decided d ON d.capture_id = k.capture_id AND d.kind = k.kind
"""


@dataclass(frozen=True)
class KindReview:
    #: The latest review that counts: it decided the frame's label of this
    #: kind. None when none counts yet.
    action: str | None
    reason: str | None
    #: A newer review waits for a Trusted reviewer, or a second learner.
    pending: bool


@dataclass(frozen=True)
class AutomaticLength:
    mission_number: int
    capture_id: uuid.UUID
    capture_number: int
    captured_at: datetime | None
    length_m: float
    depth_m: float | None
    calibration_source: str
    #: The length's calibration error, 1 SD (0079); None where its
    #: calibration has none.
    length_sd_m: float | None
    laser_confidence: float | None
    #: The dot's colour as the laser kernel read it (0072); None when it gave
    #: no reading, and on rows from before.
    laser_color: str | None
    laser_color_margin: float | None
    #: ``dot``, ``render`` (backfilled, 0076), or None.
    laser_color_source: str | None
    sam_score: float | None
    species: str | None
    scientific_name: str | None
    species_probability: float | None
    species_margin: float | None
    #: The species' published maximum (0084), and the length judged against
    #: it and the species' typical range (`species_length`): ``over_max``,
    #: ``long``, ``short``, or None.
    species_max_length_m: float | None
    species_length_flag: str | None
    #: The frame's human length, when it has exactly one laser-measured fish.
    human_length_m: float | None
    #: The mission has a current human length somewhere (the export's
    #: ``dive_human_measured``).
    mission_human_measured: bool
    #: By kind: ``laser``, ``head_tail``, ``species``; a kind never reviewed
    #: is absent.
    reviews: dict[str, KindReview]


@dataclass(frozen=True)
class AutomaticLengthPage:
    lengths: list[AutomaticLength]
    #: ``"<mission number>:<capture number>"``: the next page starts after it.
    next: str | None


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def parse_cursor(after: str | None) -> tuple[int, int] | None:
    """``"<mission>:<capture>"`` as numbers; ValueError if it isn't one."""
    if after is None:
        return None
    mission, sep, capture = after.partition(":")
    if not sep or not mission.isdigit() or not capture.isdigit():
        raise ValueError(f"not a cursor: {after!r}")
    return int(mission), int(capture)


async def automatic_lengths(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    mission_id: uuid.UUID | None = None,
    species: str | None = None,
    after: tuple[int, int] | None = None,
    limit: int = 200,
) -> AutomaticLengthPage:
    """The tenant's current automatic lengths, by mission and capture number;
    ``mission_id`` keeps one mission's, ``species`` one species' (by
    scientific name, as the Fish page filters)."""
    chosen = (
        await conn.execute(
            text(_CHOOSE_SQL),
            {
                "t": tenant_id,
                "mission": mission_id,
                "after_m": after[0] if after else None,
                "after_c": after[1] if after else None,
                "species": species,
                # The choice ends "(<scientific name>)"; LIKE's own wildcards in
                # a name are escaped, so a name matches only itself.
                "species_like": None if species is None else f"%({_like(species)})",
                "limit": limit,
            },
        )
    ).all()
    if not chosen:
        return AutomaticLengthPage([], None)
    ids = [r.id for r in chosen]
    found = (
        await conn.execute(
            text(_ROWS_SQL), {"t": tenant_id, "ids": ids, "species": species}
        )
    ).all()
    reviews = await _reviews(conn, tenant_id, [r.capture_id for r in found])
    last = chosen[-1]
    return AutomaticLengthPage(
        [_length(r._mapping, reviews.get(r.capture_id, {})) for r in found],
        (
            f"{last.mission_number}:{last.capture_number}"
            if len(chosen) == limit
            else None
        ),
    )


def _length(row, reviews: dict[str, KindReview]) -> AutomaticLength:
    columns = dict(row)
    median, mad = columns.pop("species_log_median"), columns.pop("species_log_mad")
    maximum = columns["species_max_length_m"]
    reference = (None if maximum is None
                 else SpeciesReference(maximum, log_median=median, log_mad=mad))  # fmt: skip
    return AutomaticLength(
        **columns, reviews=reviews,
        species_length_flag=species_length_flag(columns["length_m"], reference),
    )  # fmt: skip


async def _reviews(
    conn: AsyncConnection, tenant_id: uuid.UUID, capture_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, KindReview]]:
    if not capture_ids:
        return {}
    found: dict[uuid.UUID, dict[str, KindReview]] = {}
    for row in await conn.execute(
        text(_REVIEWS_SQL), {"t": tenant_id, "ids": capture_ids}
    ):
        found.setdefault(row.capture_id, {})[row.kind] = KindReview(
            action=row.action, reason=row.reason, pending=row.pending
        )
    return found
