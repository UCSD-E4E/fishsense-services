"""Reviews of the automatic results, and reviewers' skills (0062; the owner,
2026-10-21).

A member reviews one kind of a frame's automatic result: the laser dot, the
head and tail, or the species. They accept it, fix it (head and tail snapped
to the mask by the web, as the app does; the dot not), or reject it with a
reason. Their skill in that kind (``current_reviewer_skills``; none is
Learning) decides what happens:

* a **Trusted or Expert** review counts at once;
* a **Learning** review waits until a Trusted or Expert reviewer confirms it,
  or a second Learning reviewer agrees: the same points within
  :data:`AGREE_PX`, the same species, the same reason. A second review that
  disagrees is kept as a review of its own, also waiting.
  Nobody backs their own review.

When a dot or head/tail review counts, it becomes the frame's human label
(``source = 'review'``, naming the review and its render). The frame's other
live labels of that kind are superseded, never deleted
(``superseded_reason = 'review'``), so the measurement uses the review's.
What becomes the label is the backed review's points exactly as its reviewer
placed them: never an average, never a derived position. A species review is
recorded only, until frames are grouped into fish, which another agent owns.

The points must be on the frame's render under its device's **current**
camera calibration, because the measurement projects them with that camera.
A review on an older render is refused.

Runs in a tenant transaction, under RLS.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "AGREE_PX",
    "KINDS",
    "LEVELS",
    "REASONS",
    "RecordedReview",
    "ReviewInput",
    "ReviewRefused",
    "ReviewView",
    "capture_reviews",
    "record_review",
    "reviewer_level",
    "set_skill",
]

KINDS = ("laser", "head_tail", "species")
LEVELS = ("learning", "trusted", "expert")
REASONS = ("no_fish", "fish_unmeasurable", "wrong_fish", "bad_mask", "dot_wrong")
#: Two Learning reviewers agree when every point is within this many render
#: pixels of the other's (about 0.4% of a 4014-px frame's width).
AGREE_PX = 15.0
_COUNTS_ALONE = ("trusted", "expert")

Point = tuple[float, float]


class ReviewRefused(ValueError):
    """A review that can't be recorded as asked; the message says why."""


@dataclass(frozen=True)
class ReviewInput:
    capture_id: uuid.UUID
    reviewer_id: uuid.UUID
    kind: str
    action: str
    automatic_head_tail_prediction_id: uuid.UUID | None = None
    reason: str | None = None
    #: The laser dot (``kind = 'laser'``), in render pixels.
    point: Point | None = None
    head: Point | None = None
    tail: Point | None = None
    #: A fix's points where the reviewer dropped them, before the snap.
    raw: dict | None = None
    render_id: uuid.UUID | None = None
    scientific_name: str | None = None
    confirms_review_id: uuid.UUID | None = None


@dataclass(frozen=True)
class RecordedReview:
    review_id: uuid.UUID
    #: The reviewer's level in this kind when they reviewed.
    level: str
    #: Whether this review counts now.
    counts: bool
    #: The label a review made count, if any.
    label_id: uuid.UUID | None


@dataclass(frozen=True)
class ReviewView:
    review_id: uuid.UUID
    reviewer_id: uuid.UUID
    kind: str
    action: str
    level: str
    counts: bool
    reason: str | None
    point: Point | None
    head: Point | None
    tail: Point | None
    scientific_name: str | None
    confirms_review_id: uuid.UUID | None
    created_at: datetime


async def reviewer_level(
    conn: AsyncConnection, tenant_id: uuid.UUID, user_id: uuid.UUID, kind: str
) -> str:
    """The person's current level in ``kind``; none recorded is Learning."""
    level = (
        await conn.execute(
            text("""
                SELECT level FROM current_reviewer_skills
                WHERE tenant_id = :t AND user_id = :u AND kind = :k
                """),
            {"t": tenant_id, "u": user_id, "k": kind},
        )
    ).scalar_one_or_none()
    return level or "learning"


async def set_skill(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    kind: str,
    level: str,
    *,
    set_by: uuid.UUID | None = None,
    note: str | None = None,
) -> None:
    """Record a level: an admin's (``set_by``) or one earned (none)."""
    if kind not in KINDS or level not in LEVELS:
        raise ReviewRefused(f"no {kind!r} skill at level {level!r}")
    await conn.execute(
        text("""
            INSERT INTO reviewer_skills (tenant_id, user_id, kind, level, source,
                set_by, note)
            VALUES (:t, :u, :k, :l, :s, :by, :note)
            """),
        {
            "t": tenant_id,
            "u": user_id,
            "k": kind,
            "l": level,
            "s": "admin" if set_by else "earned",
            "by": set_by,
            "note": note,
        },
    )


async def record_review(
    conn: AsyncConnection, tenant_id: uuid.UUID, review: ReviewInput
) -> RecordedReview:
    if review.kind not in KINDS:
        raise ReviewRefused(f"no review of kind {review.kind!r}")
    if review.action not in ("accept", "fix", "reject", "confirm"):
        raise ReviewRefused(f"no review action {review.action!r}")
    level = await reviewer_level(conn, tenant_id, review.reviewer_id, review.kind)
    # Reviews of one frame are decided one at a time.
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"reviews:{tenant_id}:{review.capture_id}"},
    )

    target = None
    if review.confirms_review_id is not None:
        target = await _review(conn, tenant_id, review.confirms_review_id)
        if (
            target is None
            or target.capture_id != review.capture_id
            or target.kind != review.kind
        ):
            raise ReviewRefused("that review isn't of this frame and kind")
        if target.reviewer_id == review.reviewer_id:
            raise ReviewRefused("nobody backs their own review")
        if await _counts(conn, tenant_id, target):
            raise ReviewRefused("that review already counts: it needs no confirm")

    if review.action == "confirm":
        if target is None:
            raise ReviewRefused("a confirm names the review it confirms")
        if level not in _COUNTS_ALONE:
            raise ReviewRefused("only a Trusted or Expert reviewer confirms")
    elif review.action == "reject":
        if review.reason not in REASONS:
            raise ReviewRefused(f"a reject gives one of: {', '.join(REASONS)}")
    else:
        await _check_points(conn, tenant_id, review)

    # A second Learning reviewer backs the first only by agreeing with it.
    agrees = (
        review.action != "confirm"
        and target is not None
        and level not in _COUNTS_ALONE
        and _agree(target, review)
    )
    backs = review.confirms_review_id if review.action == "confirm" or agrees else None
    review_id = await _insert(conn, tenant_id, review, level, backs)

    applied = None
    if review.action == "confirm" or agrees:
        applied = target
    elif level in _COUNTS_ALONE and review.action in ("accept", "fix"):
        applied = await _review(conn, tenant_id, review_id)
    label_id = await _apply(conn, tenant_id, applied) if applied is not None else None
    return RecordedReview(
        review_id=review_id,
        level=level,
        counts=level in _COUNTS_ALONE or backs is not None,
        label_id=label_id,
    )


async def capture_reviews(
    conn: AsyncConnection, tenant_id: uuid.UUID, capture_id: uuid.UUID
) -> list[ReviewView]:
    """Every review of the frame, oldest first, with whether it counts."""
    rows = (
        await conn.execute(
            text(f"""
                SELECT r.*, {_COUNTS_SQL} AS counts
                FROM reviews r WHERE r.tenant_id = :t AND r.capture_id = :c
                ORDER BY r.seq
                """),
            {"t": tenant_id, "c": capture_id},
        )
    ).all()
    return [
        ReviewView(
            review_id=r.id,
            reviewer_id=r.reviewer_id,
            kind=r.kind,
            action=r.action,
            level=r.level,
            counts=r.counts,
            reason=r.reason,
            point=_point(r.x, r.y),
            head=_point(r.head_x, r.head_y),
            tail=_point(r.tail_x, r.tail_y),
            scientific_name=r.scientific_name,
            confirms_review_id=r.confirms_review_id,
            created_at=r.created_at,
        )
        for r in rows
    ]


#: Whether review ``r`` counts: its reviewer's level, a confirm, or backed by
#: (or itself backing) another review.
_COUNTS_SQL = """(
    r.level IN ('trusted', 'expert') OR r.confirms_review_id IS NOT NULL
    OR EXISTS (SELECT 1 FROM reviews b
               WHERE b.tenant_id = r.tenant_id AND b.confirms_review_id = r.id)
)"""


def _point(x, y) -> Point | None:
    return None if x is None or y is None else (x, y)


async def _review(conn, tenant_id, review_id):
    return (
        await conn.execute(
            text("SELECT * FROM reviews WHERE tenant_id = :t AND id = :r"),
            {"t": tenant_id, "r": review_id},
        )
    ).one_or_none()


async def _counts(conn, tenant_id, row) -> bool:
    return (
        await conn.execute(
            text(
                f"SELECT {_COUNTS_SQL} FROM reviews r WHERE r.tenant_id = :t AND r.id = :r"
            ),
            {"t": tenant_id, "r": row.id},
        )
    ).scalar_one()


def _near(a: Point | None, b: Point | None) -> bool:
    return a is not None and b is not None and math.dist(a, b) <= AGREE_PX


def _agree(target, review: ReviewInput) -> bool:
    if target.action != review.action and not (
        {target.action, review.action} <= {"accept", "fix"}
    ):
        return False
    if review.action == "reject":
        return target.reason == review.reason
    if review.kind == "laser":
        return _near(_point(target.x, target.y), review.point)
    if review.kind == "head_tail":
        return _near(_point(target.head_x, target.head_y), review.head) and _near(
            _point(target.tail_x, target.tail_y), review.tail
        )
    return (target.scientific_name or "").strip().lower() == (
        review.scientific_name or ""
    ).strip().lower()


async def _check_points(conn, tenant_id, review: ReviewInput) -> None:
    if review.kind == "species":
        if not (review.scientific_name or "").strip():
            raise ReviewRefused("a species review names the species")
        return
    if review.kind == "laser" and review.point is None:
        raise ReviewRefused("a dot review places the dot")
    if review.kind == "head_tail" and (review.head is None or review.tail is None):
        raise ReviewRefused("a head/tail review places both")
    if review.render_id is None:
        raise ReviewRefused("points name the render they are on")
    current = (
        await conn.execute(
            text("""
                SELECT r.camera_calibration_id = cc.id
                FROM capture_renders r
                JOIN captures c ON c.tenant_id = r.tenant_id AND c.id = r.capture_id
                JOIN missions m ON m.tenant_id = c.tenant_id AND m.id = c.mission_id
                LEFT JOIN current_camera_calibrations cc
                  ON cc.tenant_id = c.tenant_id
                 AND cc.device_id = coalesce(c.device_id, m.device_id)
                 AND cc.camera_model = 'pinhole'
                WHERE r.tenant_id = :t AND r.id = :r AND r.capture_id = :c
                  AND r.status = 'rendered'
                """),
            {"t": tenant_id, "r": review.render_id, "c": review.capture_id},
        )
    ).scalar_one_or_none()
    if current is None:
        raise ReviewRefused("that render isn't this frame's")
    if not current:
        raise ReviewRefused(
            "that render was made under an older camera calibration: review the "
            "frame's current render"
        )


async def _insert(conn, tenant_id, review: ReviewInput, level: str, backs) -> uuid.UUID:
    point, head, tail = review.point, review.head, review.tail
    return (
        await conn.execute(
            text("""
                INSERT INTO reviews (tenant_id, capture_id, reviewer_id, kind, action,
                    level, automatic_head_tail_prediction_id, reason, x, y, head_x,
                    head_y, tail_x, tail_y, raw, render_id, scientific_name,
                    confirms_review_id)
                VALUES (:t, :c, :u, :kind, :action, :level, :prediction, :reason,
                    :x, :y, :hx, :hy, :tx, :ty, CAST(:raw AS jsonb), :render,
                    :species, :backs)
                RETURNING id
                """),
            {
                "t": tenant_id,
                "c": review.capture_id,
                "u": review.reviewer_id,
                "kind": review.kind,
                "action": review.action,
                "level": level,
                "prediction": review.automatic_head_tail_prediction_id,
                "reason": review.reason,
                "x": point[0] if point else None,
                "y": point[1] if point else None,
                "hx": head[0] if head else None,
                "hy": head[1] if head else None,
                "tx": tail[0] if tail else None,
                "ty": tail[1] if tail else None,
                "raw": None if review.raw is None else json.dumps(review.raw),
                "render": review.render_id,
                "species": (review.scientific_name or "").strip() or None,
                "backs": backs,
            },
        )
    ).scalar_one()


async def _apply(conn, tenant_id, review) -> uuid.UUID | None:
    """Make a counting dot or head/tail review the frame's human label."""
    if review.kind == "species" or review.action not in ("accept", "fix"):
        return None
    table = "laser_labels" if review.kind == "laser" else "head_tail_labels"
    await conn.execute(
        text(f"""
            UPDATE {table} SET superseded = true, superseded_reason = 'review'
            WHERE tenant_id = :t AND capture_id = :c AND NOT superseded
            """),
        {"t": tenant_id, "c": review.capture_id},
    )
    points = (
        ("x, y", ":x, :y", {"x": review.x, "y": review.y})
        if review.kind == "laser"
        else (
            "head_x, head_y, tail_x, tail_y",
            ":hx, :hy, :tx, :ty",
            {"hx": review.head_x, "hy": review.head_y,
             "tx": review.tail_x, "ty": review.tail_y},
        )
    )  # fmt: skip
    columns, values, params = points
    return (
        await conn.execute(
            text(f"""
                INSERT INTO {table} (tenant_id, capture_id, source, completed,
                    review_id, render_id, {columns})
                VALUES (:t, :c, 'review', true, :review, :render, {values})
                RETURNING id
                """),
            {
                "t": tenant_id,
                "c": review.capture_id,
                "review": review.id,
                "render": review.render_id,
                **params,
            },
        )
    ).scalar_one()
