"""Reviews of the automatic results, and reviewers' skills (0063; the owner,
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

A reject that counts supersedes too (the code review of #293, 2026-10-22):

* ``no_fish`` and ``fish_unmeasurable`` are said of the frame: every live
  label of the kind is superseded, whoever placed it;
* ``wrong_fish``, ``bad_mask`` and ``dot_wrong`` are said of the automatic
  result: only the labels an accept of that result made are superseded. A
  labeller's points are not judged by them.

Only the latest word counts. A Learning review can't be backed once a newer
review of the frame and kind counts (which it would otherwise undo), and
backing one checks its render as well as the backer's.

The points must be on the frame's render under its device's **current**
camera calibration, because the measurement projects them with that camera.
A review on an older render is refused.

A frame with a review of a kind takes its labels of that kind from the app:
:func:`reviewed_in_app` keeps it out of Label Studio's preprocess cohorts, so
it is never imported again (a superseded row would otherwise make it work every
wake).

Runs in a tenant transaction, under RLS.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "AGREE_PX",
    "FrameNotFound",
    "KINDS",
    "LEVELS",
    "MemberSkills",
    "NotAMember",
    "REASONS",
    "RecordedReview",
    "ReviewInput",
    "ReviewRefused",
    "ReviewView",
    "capture_reviews",
    "member_skills",
    "record_review",
    "reviewed_in_app",
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
#: Reject reasons said of the frame rather than of the automatic result.
_OF_THE_FRAME = ("no_fish", "fish_unmeasurable")

Point = tuple[float, float]


class ReviewRefused(ValueError):
    """A review that can't be recorded as asked; the message says why."""


class FrameNotFound(ReviewRefused):
    """No such frame in the tenant."""


class NotAMember(ReviewRefused):
    """A skill is a member's: the person isn't one of the tenant's."""


def reviewed_in_app(kind: str) -> str:
    """Capture ``c`` has a review of ``kind``: its labels of that kind come
    from the app, so it isn't Label Studio work."""
    assert kind in ("laser", "head_tail"), kind
    return f"""EXISTS (
    SELECT 1 FROM reviews rv
    WHERE rv.tenant_id = c.tenant_id AND rv.capture_id = c.id AND rv.kind = '{kind}'
)"""


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


@dataclass(frozen=True)
class MemberSkills:
    user_id: uuid.UUID
    sub: str
    role: str
    name: str | None
    email: str | None
    #: Their current level per kind; Learning where none is recorded.
    skills: dict[str, str]


async def member_skills(
    conn: AsyncConnection, tenant_id: uuid.UUID
) -> list[MemberSkills]:
    """The active tenant's members by name (0065), with their levels. For its
    admins: the API shows it to no one else."""
    members = (await conn.execute(text("SELECT * FROM tenant_members()"))).all()
    levels: dict[uuid.UUID, dict[str, str]] = {}
    for r in await conn.execute(
        text(
            "SELECT user_id, kind, level FROM current_reviewer_skills WHERE tenant_id = :t"
        ),
        {"t": tenant_id},
    ):
        levels.setdefault(r.user_id, {})[r.kind] = r.level
    return [
        MemberSkills(
            user_id=m.user_id, sub=m.sub, role=m.role, name=m.name, email=m.email,
            skills={k: levels.get(m.user_id, {}).get(k, "learning") for k in KINDS},
        )
        for m in members
    ]  # fmt: skip


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
    # A member's memberships are hidden from the others (RLS), so the
    # membership is the foreign key's to check.
    try:
        async with conn.begin_nested():
            await conn.execute(
                text("""
                    INSERT INTO reviewer_skills (tenant_id, user_id, kind, level,
                        source, set_by, note)
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
    except IntegrityError as error:
        if "reviewer_skills_membership_fk" in str(error.orig):
            raise NotAMember(f"{user_id} isn't a member of this tenant") from None
        raise


async def record_review(
    conn: AsyncConnection, tenant_id: uuid.UUID, review: ReviewInput
) -> RecordedReview:
    if review.kind not in KINDS:
        raise ReviewRefused(f"no review of kind {review.kind!r}")
    if review.action not in ("accept", "fix", "reject", "confirm"):
        raise ReviewRefused(f"no review action {review.action!r}")
    await _check_frame(conn, tenant_id, review)
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
        if await _newer_counts(conn, tenant_id, target):
            raise ReviewRefused(
                "a newer review of this frame already counts: review what it shows now"
            )
        if _has_points(target):
            await _check_render(conn, tenant_id, target.capture_id, target.render_id)

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
    elif level in _COUNTS_ALONE and review.action in ("accept", "fix", "reject"):
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


def _has_points(review) -> bool:
    return review.kind != "species" and review.action in ("accept", "fix")


async def _newer_counts(conn, tenant_id, target) -> bool:
    """A review of the target's frame and kind, after it, counts."""
    return (
        await conn.execute(
            text(f"""
                SELECT EXISTS (
                    SELECT 1 FROM reviews r
                    WHERE r.tenant_id = :t AND r.capture_id = :c AND r.kind = :k
                      AND r.seq > :seq AND {_COUNTS_SQL}
                )
                """),
            {"t": tenant_id, "c": target.capture_id, "k": target.kind,
             "seq": target.seq},
        )
    ).scalar_one()  # fmt: skip


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
    await _check_render(conn, tenant_id, review.capture_id, review.render_id)


async def _check_frame(conn, tenant_id, review: ReviewInput) -> None:
    """The frame is the tenant's, and a prediction named is the frame's."""
    found = (
        await conn.execute(
            text("""
                SELECT c.id, p.capture_id AS predicted
                FROM captures c
                LEFT JOIN automatic_head_tail_predictions p
                  ON p.tenant_id = c.tenant_id AND p.id = :p
                WHERE c.tenant_id = :t AND c.id = :c
                """),
            {"t": tenant_id, "c": review.capture_id,
             "p": review.automatic_head_tail_prediction_id},
        )
    ).one_or_none()  # fmt: skip
    if found is None:
        raise FrameNotFound("no such frame")
    if (
        review.automatic_head_tail_prediction_id is not None
        and found.predicted != review.capture_id
    ):
        raise ReviewRefused("that prediction isn't this frame's")


async def _check_render(conn, tenant_id, capture_id, render_id) -> None:
    """The render is the frame's, under its device's current pinhole camera."""
    if render_id is None:
        raise ReviewRefused("points name the render they are on")
    found = (
        await conn.execute(
            text("""
                SELECT r.camera_calibration_id, cc.id AS current_id, cc.camera_model
                FROM capture_renders r
                JOIN captures c ON c.tenant_id = r.tenant_id AND c.id = r.capture_id
                JOIN missions m ON m.tenant_id = c.tenant_id AND m.id = c.mission_id
                LEFT JOIN current_camera_calibrations cc
                  ON cc.tenant_id = c.tenant_id
                 AND cc.device_id = coalesce(c.device_id, m.device_id)
                WHERE r.tenant_id = :t AND r.id = :r AND r.capture_id = :c
                  AND r.status = 'rendered'
                """),
            {"t": tenant_id, "r": render_id, "c": capture_id},
        )
    ).first()
    if found is None:
        raise ReviewRefused("that render isn't this frame's")
    if found.current_id is None or found.camera_model != "pinhole":
        raise ReviewRefused(
            "the frame's device has no current pinhole camera calibration to "
            "measure with"
        )
    if found.camera_calibration_id != found.current_id:
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
    """Make a counting dot or head/tail review the frame's human label, or a
    counting reject supersede what it rejects."""
    if review.kind == "species":
        return None
    table = "laser_labels" if review.kind == "laser" else "head_tail_labels"
    only_accepted = """AND source = 'review' AND review_id IN (
        SELECT a.id FROM reviews a WHERE a.tenant_id = :t AND a.action = 'accept')"""
    await conn.execute(
        text(f"""
            UPDATE {table} SET superseded = true, superseded_reason = 'review'
            WHERE tenant_id = :t AND capture_id = :c AND NOT superseded
            {only_accepted if review.action == "reject"
             and review.reason not in _OF_THE_FRAME else ""}
            """),
        {"t": tenant_id, "c": review.capture_id},
    )  # fmt: skip
    if review.action == "reject":
        return None
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
