"""The one-shot v1 -> v2 data migration (PLAN.md §6.4).

Reads v1's ``fishsense`` database and writes the v2 database as the **lab
tenant**, in dependency order, inside **one transaction**: it lands whole or not
at all. Every migrated row keeps its ``v1_id``, which makes the job idempotent
(re-running inserts nothing new) and keeps v1 ids addressable for the research
repos.

It never invents data. Values v1 never recorded stay NULL / unknown (v2's
constraints allow that only for rows with a ``v1_id``). The returned
:class:`Report` accounts for every v1 row: (rows in v1, rows migrated to v2).

v1's production data is only ever used in local rehearsals, never in tests.
"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field

from sqlalchemy import Connection, create_engine, text

BATCH = 5_000


@dataclass
class Report(Mapping):
    """Per v1 table: (rows in v1, rows migrated into v2)."""

    counts: dict[str, tuple[int, int]] = field(default_factory=dict)

    def __getitem__(self, table: str) -> tuple[int, int]:
        return self.counts[table]

    def __iter__(self) -> Iterator[str]:
        return iter(self.counts)

    def __len__(self) -> int:
        return len(self.counts)

    def discrepancies(self) -> dict[str, tuple[int, int]]:
        return {t: c for t, c in self.counts.items() if c[0] != c[1]}


def migrate_v1(
    *,
    source_url: str,
    target_url: str,
    tenant_slug: str = "lab",
    tenant_name: str = "E4E FishSense lab",
) -> Report:
    source, target = create_engine(source_url), create_engine(target_url)
    report = Report()
    try:
        with source.connect() as v1, target.begin() as v2:
            tenant = _ensure_tenant(v2, tenant_slug, tenant_name)
            for step in STEPS:
                step(v1, v2, tenant, report)
    finally:
        source.dispose()
        target.dispose()
    return report


# --- helpers ---------------------------------------------------------------------


def _ensure_tenant(v2: Connection, slug: str, name: str):
    v2.execute(
        text(
            "INSERT INTO tenants (slug, name) VALUES (:s, :n) "
            "ON CONFLICT (slug) DO NOTHING"
        ),
        {"s": slug, "n": name},
    )
    return v2.execute(
        text("SELECT id FROM tenants WHERE slug = :s"), {"s": slug}
    ).scalar_one()


def _rows(v1: Connection, sql: str) -> Iterator[dict]:
    result = v1.execution_options(stream_results=True).execute(text(sql))
    for row in result.mappings():
        yield dict(row)


def _insert(v2: Connection, sql: str, rows: Iterator[dict]) -> None:
    batch: list[dict] = []
    for row in rows:
        batch.append(row)
        if len(batch) == BATCH:
            v2.execute(text(sql), batch)
            batch = []
    if batch:
        v2.execute(text(sql), batch)


def _ids(v2: Connection, table: str) -> dict[int, object]:
    """v1 id -> v2 id for one table's migrated rows."""
    result = v2.execute(text(f"SELECT v1_id, id FROM {table} WHERE v1_id IS NOT NULL"))
    return {v1_id: v2_id for v1_id, v2_id in result}


def _account(v1: Connection, v2: Connection, report, v1_table: str, v2_table: str):
    in_v1 = v1.execute(text(f'SELECT count(*) FROM "{v1_table}"')).scalar_one()
    in_v2 = v2.execute(
        text(f"SELECT count(*) FROM {v2_table} WHERE v1_id IS NOT NULL")
    ).scalar_one()
    report.counts[v1_table] = (in_v1, in_v2)


# --- steps, in dependency order ---------------------------------------------------


def _reference_data(v1, v2, tenant, report) -> None:
    _insert(
        v2,
        "INSERT INTO calibration_targets (name, interior_rows, interior_cols, "
        "pitch_x_m, pitch_y_m, notes, valid_from, v1_id) "
        "VALUES (:name, :rows, :cols, :square_size_m, :square_size_m, :notes, "
        "coalesce(:created_at, now()), :id) ON CONFLICT DO NOTHING",
        _rows(v1, "SELECT * FROM calibrationtarget"),
    )
    _account(v1, v2, report, "calibrationtarget", "calibration_targets")

    # A fish model is an identity (fish_models); its known length is versioned.
    _insert(
        v2,
        "INSERT INTO fish_models (name) VALUES (:name) ON CONFLICT DO NOTHING",
        _rows(
            v1,
            "SELECT name FROM fishmodelreference "
            "UNION SELECT name FROM fish WHERE name IS NOT NULL",
        ),
    )
    _insert(
        v2,
        "INSERT INTO fish_model_references (name, known_length_m, is_provisional, "
        "notes, v1_id) VALUES (:name, :known_length_m, :is_provisional, :notes, :id) "
        "ON CONFLICT DO NOTHING",
        _rows(v1, "SELECT * FROM fishmodelreference"),
    )
    _account(v1, v2, report, "fishmodelreference", "fish_model_references")

    _insert(
        v2,
        "INSERT INTO species (scientific_name, common_name, v1_id) "
        "VALUES (:scientific_name, :common_name, :id) ON CONFLICT DO NOTHING",
        _rows(v1, "SELECT * FROM species"),
    )
    _account(v1, v2, report, "species", "species")

    _insert(
        v2,
        "INSERT INTO slate_templates (name, dpi, source_path, reference_points, "
        "created_at, v1_id) VALUES (:name, :dpi, :path, "
        "CAST(:reference_points AS jsonb), coalesce(:created_at, now()), :id) "
        "ON CONFLICT DO NOTHING",
        _rows(
            v1, "SELECT *, reference_points::text AS reference_points FROM diveslate"
        ),
    )
    _account(v1, v2, report, "diveslate", "slate_templates")


def _devices(v1, v2, tenant, report) -> None:
    _insert(
        v2,
        "INSERT INTO devices (tenant_id, kind, serial, name, v1_id) "
        "VALUES (:tenant, 'lite', :serial_number, :name, :id) ON CONFLICT DO NOTHING",
        ({**r, "tenant": tenant} for r in _rows(v1, "SELECT * FROM camera")),
    )
    _account(v1, v2, report, "camera", "devices")


def _dives(v1, v2, tenant, report) -> None:
    devices = _ids(v2, "devices")
    slates = _ids(v2, "slate_templates")
    targets = _ids(v2, "calibration_targets")

    def rows():
        for r in _rows(v1, "SELECT * FROM dive"):
            yield {
                "tenant": tenant,
                "id": r["id"],
                "name": r["name"],
                "path": r["path"],
                "dived_at": r["dive_datetime"],
                # NULL at the DB level meant the ORM default, LOW.
                "priority": (r["priority"] or "LOW").lower(),
                "notes": r["notes"],
                "flip": bool(r["flip_dive_slate"]),
                "device": devices.get(r["camera_id"]),
                "slate": slates.get(r["dive_slate_id"]),
                "target": targets.get(r["calibration_target_id"]),
            }

    _insert(
        v2,
        "INSERT INTO dives (tenant_id, v1_id, name, source_path, dived_at, priority, "
        "notes, flip_dive_slate, device_id, slate_template_id, calibration_target_id) "
        "VALUES (:tenant, :id, :name, :path, :dived_at, :priority, :notes, :flip, "
        ":device, :slate, :target) ON CONFLICT DO NOTHING",
        rows(),
    )
    # Borrowing links need every dive first.
    v2.execute(
        text(
            "UPDATE dives d SET calibration_source_dive_id = src.id "
            "FROM dives src "
            "WHERE src.tenant_id = d.tenant_id AND src.v1_id = :source_v1 "
            "AND d.v1_id = :dive_v1"
        ),
        [
            {"dive_v1": r["id"], "source_v1": r["calibration_dive_id"]}
            for r in _rows(
                v1,
                "SELECT id, calibration_dive_id FROM dive "
                "WHERE calibration_dive_id IS NOT NULL",
            )
        ]
        or [{"dive_v1": None, "source_v1": None}],
    )
    _account(v1, v2, report, "dive", "dives")


def _captures(v1, v2, tenant, report) -> None:
    dives, devices = _ids(v2, "dives"), _ids(v2, "devices")
    _insert(
        v2,
        "INSERT INTO captures (tenant_id, v1_id, dive_id, device_id, source_path, "
        "captured_at, checksum, checksum_algorithm, is_canonical) "
        "VALUES (:tenant, :id, :dive, :device, :path, :taken_datetime, :checksum, "
        "'md5', :is_canonical) ON CONFLICT DO NOTHING",
        (
            {
                **r,
                "tenant": tenant,
                "dive": dives.get(r["dive_id"]),
                "device": devices.get(r["camera_id"]),
            }
            for r in _rows(v1, "SELECT * FROM image")
        ),
    )
    _account(v1, v2, report, "image", "captures")


STEPS: list[Callable] = [_reference_data, _devices, _dives, _captures]
