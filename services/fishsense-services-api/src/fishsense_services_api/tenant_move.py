"""Moving missions between tenants (the owner, 2026-11-11).

"most of the data in the e4e lab actually belongs to reef. it should move to
the reef tenant": 481 of lab's 525 missions are the REEF data dumps (by the
top folder of their source path); the other 44 are the lab's own pool tests.

A move takes the missions asked for and everything that hangs off them, found
by following the foreign keys on ``tenant_id`` rather than a list of tables,
so a table added later moves too:

* **down**: a row whose parent moved moves (a capture of a moving mission,
  a label of a moving capture, a mission that takes its calibration from a
  moving one);
* **up**: a parent that a moved row uses moves (a deployment, a fish), but
  only if nothing staying uses it too; otherwise the move is refused;
* **cameras** are the exception (the owner's choice, 2026-11-11): a device a
  moving mission or capture names moves, with its camera calibrations, and if
  a staying mission uses it too, the source keeps a copy of the device and its
  calibrations (same name, serial and matrices; new ids and numbers) and the
  staying rows are pointed at the copy;
* Label Studio sync cursors, which name a project by its Label Studio id
  rather than a key, follow their project.

Rows change ``tenant_id`` and nothing else: numbers are kept (they come from
one sequence per table, across tenants), and no label's coordinates change.
Nothing is deleted.

Refused, with nothing written: a mission number the source doesn't have; a
parent shared with what stays; a mission that would have to come along
without being asked for; and any stored object of a moving capture whose copy
is not yet in the target's bucket (each tenant reads only its own bucket, so
a move before the copy would leave the target's pages without images).

The keys pairing ``tenant_id`` with a parent's are set aside while the rows
move and added back, from their own definitions, before the plan is returned:
adding a key checks every row in one pass, so anything this misses is refused
by the database. Deferring them instead (the first version) checked
row by row at commit: on a copy of prod, the 481 REEF missions were still
being checked after two hours (2026-11-11). Setting the keys aside locks the
tables until the transaction ends, so the move waits at most ``_LOCK_WAIT``
for them rather than queue in front of everyone else. The plan is
the whole move, run and rolled back: it names exactly what the apply writes,
and its fingerprint covers that. The apply re-runs it and keeps it only if
the fingerprint is the one reviewed. Runs as the schema owner, across both
tenants.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from fishsense_services_api.mission_backfill import PlanChanged, plan_fingerprint

__all__ = ["MovePlan", "MoveRefused", "apply_move", "plan_move"]

#: Moved explicitly, and split when shared: never pulled along a key.
_CAMERAS = frozenset({"devices", "camera_calibrations"})
#: Stored objects, and the column naming each one's key in its bucket.
_OBJECTS = {
    "capture_raws": "key",
    "capture_renders": "key",
    "capture_thumbnails": "key",
    "prediction_masks": "object_key",
}


#: How long the move waits for a table's lock before giving up.
_LOCK_WAIT = "30s"


class MoveRefused(ValueError):
    """The missions can't move as asked; the message says why."""


@dataclass(frozen=True)
class MovePlan:
    #: The missions moving, by number.
    missions: tuple[int, ...]
    #: Devices moving whole, by name (or serial).
    devices_moved: tuple[str, ...]
    #: Devices moving, with a copy staying for the source's missions.
    devices_split: tuple[str, ...]
    #: Rows moving, by table.
    rows: tuple[tuple[str, int], ...]
    #: Staying rows pointed at a device's or calibration's copy, by table.
    repointed: tuple[tuple[str, int], ...]
    fingerprint: str


@dataclass(frozen=True)
class _Key:
    child: str
    parent: str
    #: (child column, parent column) pairs, without tenant_id.
    pairs: tuple[tuple[str, str], ...]

    @property
    def child_cols(self) -> str:
        return ", ".join(c for c, _ in self.pairs)

    @property
    def parent_cols(self) -> str:
        return ", ".join(p for _, p in self.pairs)


async def _tenant_keys(conn: AsyncConnection) -> list[_Key]:
    """Every foreign key that pairs ``tenant_id`` with the parent's."""
    rows = await conn.execute(text("""
        SELECT k.conrelid::regclass::text AS child,
               k.confrelid::regclass::text AS parent,
               ARRAY(SELECT a.attname::text FROM unnest(k.conkey) WITH ORDINALITY u(n, i)
                     JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.n
                     ORDER BY u.i) AS child_cols,
               ARRAY(SELECT a.attname::text FROM unnest(k.confkey) WITH ORDINALITY u(n, i)
                     JOIN pg_attribute a ON a.attrelid = k.confrelid AND a.attnum = u.n
                     ORDER BY u.i) AS parent_cols
        FROM pg_constraint k
        WHERE k.contype = 'f' AND k.connamespace = 'public'::regnamespace
        ORDER BY 1, 2, k.conname
        """))  # fmt: skip
    keys = []
    for row in rows:
        pairs = list(zip(row.child_cols, row.parent_cols))
        if ("tenant_id", "tenant_id") not in pairs or len(pairs) < 2:
            continue
        keys.append(_Key(row.child, row.parent,
                         tuple(p for p in pairs if p != ("tenant_id", "tenant_id"))))  # fmt: skip
    return keys


async def _set_keys_aside(conn: AsyncConnection) -> list[tuple[str, str, str]]:
    """Drop every key pairing ``tenant_id`` with other columns, and return
    each one's (table, name, definition) to add it back."""
    saved = [tuple(r) for r in await conn.execute(text("""
        SELECT k.conrelid::regclass::text, quote_ident(k.conname),
               pg_get_constraintdef(k.oid)
        FROM pg_constraint k
        WHERE k.contype = 'f' AND k.connamespace = 'public'::regnamespace
          AND cardinality(k.conkey) > 1
          AND EXISTS (SELECT 1 FROM pg_attribute a
                      WHERE a.attrelid = k.conrelid AND a.attnum = ANY (k.conkey)
                        AND a.attname = 'tenant_id')
        ORDER BY 1, 2
        """))]  # fmt: skip
    for table, name, _ in saved:
        await conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {name}"))
    return saved


async def _put_keys_back(conn: AsyncConnection, saved) -> None:
    """Add the keys back; each checks every row as it is added."""
    for table, name, definition in saved:
        try:
            await conn.execute(
                text(f"ALTER TABLE {table} ADD CONSTRAINT {name} {definition}")
            )
        except IntegrityError as error:
            detail = str(error.orig).strip().splitlines()
            raise MoveRefused(
                "the database refused the move: " + " ".join(detail[:2])
            ) from error


async def _count(conn: AsyncConnection, sql: str, params: dict) -> int:
    return (await conn.execute(text(sql), params)).scalar_one()


async def _update(conn: AsyncConnection, sql: str, params: dict) -> int:
    return (await conn.execute(text(sql), params)).rowcount


def _bump(tally: dict[str, int], table: str, n: int) -> None:
    if n:
        tally[table] = tally.get(table, 0) + n


async def _missions(conn, source, numbers) -> dict[uuid.UUID, int]:
    wanted = sorted(set(numbers))
    if not wanted:
        raise MoveRefused("no missions asked for")
    found = {
        r.id: r.number
        for r in await conn.execute(
            text("SELECT id, number FROM missions "
                 "WHERE tenant_id = :s AND number = ANY(:n)"),
            {"s": source, "n": wanted},
        )
    }  # fmt: skip
    missing = sorted(set(wanted) - set(found.values()))
    if missing:
        raise MoveRefused(
            "not missions of the source tenant: " + ", ".join(map(str, missing))
        )
    return found


async def _check_objects(conn, source, mission_ids, target_bucket) -> None:
    """Every stored object of a moving capture has its copy, same key, in the
    target's bucket."""
    owed = []
    for table, key in _OBJECTS.items():
        n = await _count(conn, f"""
            SELECT count(*) FROM {table} o
            JOIN captures c ON c.tenant_id = o.tenant_id AND c.id = o.capture_id
            WHERE o.tenant_id = :s AND c.mission_id = ANY(:m)
              AND o.{key} IS NOT NULL AND o.bucket <> :b
              AND NOT EXISTS (
                  SELECT 1 FROM {table} w
                  WHERE w.tenant_id = o.tenant_id AND w.capture_id = o.capture_id
                    AND w.{key} = o.{key} AND w.bucket = :b)
            """, {"s": source, "m": list(mission_ids), "b": target_bucket})  # fmt: skip
        if n:
            owed.append(f"{table}: {n}")
    if owed:
        raise MoveRefused(
            f"objects not yet copied into {target_bucket} ({'; '.join(owed)}); "
            "copy them first"
        )


async def _cameras(conn, source, mission_ids):
    """The devices the moving missions and their captures name: whole (no
    staying mission or capture names them) or split."""
    rows = await conn.execute(text("""
        WITH named AS (
            SELECT device_id FROM missions
            WHERE tenant_id = :s AND id = ANY(:m) AND device_id IS NOT NULL
            UNION
            SELECT device_id FROM captures
            WHERE tenant_id = :s AND mission_id = ANY(:m) AND device_id IS NOT NULL)
        SELECT d.id, coalesce(d.name, d.serial) AS label,
               EXISTS (SELECT 1 FROM missions x WHERE x.tenant_id = :s
                         AND x.device_id = d.id AND x.id <> ALL(:m))
               OR EXISTS (SELECT 1 FROM captures x WHERE x.tenant_id = :s
                         AND x.device_id = d.id AND x.mission_id <> ALL(:m)) AS shared
        FROM devices d JOIN named n ON n.device_id = d.id
        WHERE d.tenant_id = :s ORDER BY 2
        """), {"s": source, "m": list(mission_ids)})  # fmt: skip
    rows = rows.all()
    return [r for r in rows if not r.shared], [r for r in rows if r.shared]


async def _copyable(conn, table: str) -> list[str]:
    """A table's columns a copy carries: not its id, number or identity."""
    return [
        r.column_name
        for r in await conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :t
              AND column_name NOT IN ('id', 'number')
              AND is_identity = 'NO' AND is_generated = 'NEVER'
            ORDER BY ordinal_position
            """), {"t": table})  # fmt: skip
    ]


async def _copy_cameras(conn, source, split) -> tuple[dict, dict]:
    """Copies of the split devices and their calibrations, in the source,
    under a placeholder serial and name until the originals have left."""
    devices: dict[uuid.UUID, uuid.UUID] = {}
    calibrations: dict[uuid.UUID, uuid.UUID] = {}
    device_cols = await _copyable(conn, "devices")
    camera_cols = await _copyable(conn, "camera_calibrations")
    for row in split:
        copy = uuid.uuid4()
        values = [
            {"serial": "serial || :tag", "name": "name || :tag",
             "v1_id": "NULL"}.get(c, c)
            for c in device_cols
        ]  # fmt: skip
        await conn.execute(text(f"""
            INSERT INTO devices (id, {', '.join(device_cols)})
            SELECT :copy, {', '.join(values)} FROM devices
            WHERE tenant_id = :s AND id = :d
            """), {"copy": copy, "tag": f" {copy}", "s": source, "d": row.id})  # fmt: skip
        devices[row.id] = copy
        originals = (await conn.execute(text(
            "SELECT id FROM camera_calibrations WHERE tenant_id = :s AND device_id = :d"
        ), {"s": source, "d": row.id})).scalars().all()  # fmt: skip
        for original in originals:
            twin = uuid.uuid4()
            values = [{"device_id": ":copy", "v1_id": "NULL"}.get(c, c)
                      for c in camera_cols]  # fmt: skip
            await conn.execute(text(f"""
                INSERT INTO camera_calibrations (id, {', '.join(camera_cols)})
                SELECT :twin, {', '.join(values)} FROM camera_calibrations
                WHERE tenant_id = :s AND id = :c
                """), {"twin": twin, "copy": copy, "s": source, "c": original})  # fmt: skip
            calibrations[original] = twin
    return devices, calibrations


async def _shared_parent(conn, keys, key: _Key, source, target) -> str | None:
    """Why the parents moved rows use along `key` can't come along: another
    staying row uses them too. None when nothing does."""
    for other in keys:
        if other.parent != key.parent:
            continue
        n = await _count(conn, f"""
            SELECT count(*) FROM {other.child}
            WHERE tenant_id = :s AND ({other.child_cols}) IN (
                SELECT {key.parent_cols} FROM {key.parent}
                WHERE tenant_id = :s AND ({key.parent_cols}) IN (
                    SELECT {key.child_cols} FROM {key.child} WHERE tenant_id = :t))
            """, {"s": source, "t": target})  # fmt: skip
        if n:
            return (
                f"{key.parent} used by moving {key.child} are also used by "
                f"{n} staying {other.child}"
            )
    return None


async def _follow(conn, keys, source, target, moved: dict[str, int]) -> None:
    """Down and up the keys until nothing more moves."""
    while True:
        changed = 0
        # Down, to a fixed point: rows whose parent moved.
        while True:
            step = 0
            for key in keys:
                if key.parent in _CAMERAS:
                    continue
                n = await _update(conn, f"""
                    UPDATE {key.child} SET tenant_id = :t
                    WHERE tenant_id = :s AND ({key.child_cols}) IN (
                        SELECT {key.parent_cols} FROM {key.parent} WHERE tenant_id = :t)
                    """, {"s": source, "t": target})  # fmt: skip
                _bump(moved, key.child, n)
                step += n
            n = await _update(conn, """
                UPDATE label_studio_sync_cursors SET tenant_id = :t
                WHERE tenant_id = :s AND (kind, ls_project_id) IN (
                    SELECT kind, ls_project_id FROM label_studio_projects
                    WHERE tenant_id = :t)
                """, {"s": source, "t": target})  # fmt: skip
            _bump(moved, "label_studio_sync_cursors", n)
            step += n
            changed += step
            if not step:
                break
        # Up: parents only moved rows use.
        pulled = 0
        for key in keys:
            if key.parent in _CAMERAS:
                continue
            why = await _shared_parent(conn, keys, key, source, target)
            if why:
                raise MoveRefused(why)
            n = await _update(conn, f"""
                UPDATE {key.parent} SET tenant_id = :t
                WHERE tenant_id = :s AND ({key.parent_cols}) IN (
                    SELECT {key.child_cols} FROM {key.child} WHERE tenant_id = :t)
                """, {"s": source, "t": target})  # fmt: skip
            _bump(moved, key.parent, n)
            pulled += n
        if not pulled:
            return


async def _repoint(conn, keys, source, parent: str, mapping: dict) -> dict[str, int]:
    """Point the source's rows that name a moved `parent` at its copy."""
    repointed: dict[str, int] = {}
    if not mapping:
        return repointed
    for key in keys:
        if key.parent != parent or len(key.pairs) != 1:
            continue
        ((column, _),) = key.pairs
        n = await _update(conn, f"""
            UPDATE {key.child} x SET {column} = m.copy
            FROM unnest(CAST(:old AS uuid[]), CAST(:new AS uuid[])) AS m(original, copy)
            WHERE x.tenant_id = :s AND x.{column} = m.original
            """, {"s": source, "old": list(mapping), "new": list(mapping.values())})  # fmt: skip
        _bump(repointed, key.child, n)
    return repointed


async def _move(
    conn: AsyncConnection,
    source: uuid.UUID,
    target: uuid.UUID,
    numbers: Sequence[int],
    *,
    source_bucket: str,
    target_bucket: str,
) -> MovePlan:
    if source == target:
        raise MoveRefused("the source and the target are the same tenant")
    asked = await _missions(conn, source, numbers)
    await _check_objects(conn, source, asked, target_bucket)
    in_source = set((await conn.execute(text(
        "SELECT id FROM missions WHERE tenant_id = :s"), {"s": source})).scalars())  # fmt: skip
    keys = await _tenant_keys(conn)
    await conn.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_WAIT}'"))
    saved = await _set_keys_aside(conn)

    whole, split = await _cameras(conn, source, asked)
    device_copies, calibration_copies = await _copy_cameras(conn, source, split)
    moved: dict[str, int] = {}
    _bump(moved, "missions", await _update(conn,
        "UPDATE missions SET tenant_id = :t WHERE tenant_id = :s AND id = ANY(:m)",
        {"s": source, "t": target, "m": list(asked)}))  # fmt: skip
    devices = [r.id for r in (*whole, *split)]
    _bump(moved, "devices", await _update(conn,
        "UPDATE devices SET tenant_id = :t WHERE tenant_id = :s AND id = ANY(:d)",
        {"s": source, "t": target, "d": devices}))  # fmt: skip
    _bump(moved, "camera_calibrations", await _update(conn,
        "UPDATE camera_calibrations SET tenant_id = :t "
        "WHERE tenant_id = :s AND device_id = ANY(:d)",
        {"s": source, "t": target, "d": devices}))  # fmt: skip

    await _follow(conn, keys, source, target, moved)

    along = sorted(
        (await conn.execute(text(
            "SELECT number FROM missions WHERE tenant_id = :t AND id = ANY(:in_source) "
            "AND id <> ALL(:asked)"),
            {"t": target, "in_source": list(in_source), "asked": list(asked)},
        )).scalars()
    )  # fmt: skip
    if along:
        raise MoveRefused(
            "missions not asked for would have to come along (they take a "
            "calibration from, or are used by, a moving one): "
            + ", ".join(map(str, along))
        )

    repointed = await _repoint(conn, keys, source, "devices", device_copies)
    for table, n in (
        await _repoint(conn, keys, source, "camera_calibrations", calibration_copies)
    ).items():
        _bump(repointed, table, n)
    for original, copy in device_copies.items():
        await conn.execute(text("""
            UPDATE devices c SET serial = o.serial, name = o.name
            FROM devices o WHERE c.id = :copy AND o.id = :original
            """), {"copy": copy, "original": original})  # fmt: skip

    await _put_keys_back(conn, saved)

    body = {
        "source": str(source),
        "target": str(target),
        "missions": sorted(asked.values()),
        "devices_moved": [r.label for r in whole],
        "devices_split": [r.label for r in split],
        "rows": sorted(moved.items()),
        "repointed": sorted(repointed.items()),
        "buckets": [source_bucket, target_bucket],
    }
    return MovePlan(
        missions=tuple(body["missions"]),
        devices_moved=tuple(body["devices_moved"]),
        devices_split=tuple(body["devices_split"]),
        rows=tuple(body["rows"]),
        repointed=tuple(body["repointed"]),
        fingerprint=plan_fingerprint(body),
    )


async def plan_move(
    conn: AsyncConnection,
    source: uuid.UUID,
    target: uuid.UUID,
    numbers: Sequence[int],
    *,
    source_bucket: str,
    target_bucket: str,
) -> MovePlan:
    """What moving these missions would do: the move itself, rolled back."""
    savepoint = await conn.begin_nested()
    try:
        return await _move(conn, source, target, numbers,
                           source_bucket=source_bucket, target_bucket=target_bucket)  # fmt: skip
    finally:
        await savepoint.rollback()


async def apply_move(
    conn: AsyncConnection,
    source: uuid.UUID,
    target: uuid.UUID,
    numbers: Sequence[int],
    *,
    source_bucket: str,
    target_bucket: str,
    expect: str,
) -> MovePlan:
    """The move, kept only if it is the plan reviewed (its fingerprint); the
    caller's transaction rolls back on anything raised."""
    plan = await _move(conn, source, target, numbers,
                       source_bucket=source_bucket, target_bucket=target_bucket)  # fmt: skip
    if plan.fingerprint != expect:
        raise PlanChanged(
            f"the move is now {plan.fingerprint}, not the {expect} reviewed; "
            "run the dry run again"
        )
    return plan
