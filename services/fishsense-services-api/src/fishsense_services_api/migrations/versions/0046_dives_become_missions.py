"""Dives become missions (PLAN.md §4.9, Phase 2a of §9.26).

A mission is one device's continuous recording session in the field; a Lite
dive is one kind. The owner chose a full rename (2026-10-18), done here and
across the code in the same release, with no change of behaviour:

* tables: ``dives`` -> ``missions``; ``dive_laser_lines``,
  ``dive_frame_clusters`` and ``dive_frame_cluster_captures`` -> ``mission_*``;
* columns: every ``dive_id`` -> ``mission_id``, and on ``missions``
  ``calibration_source_dive_id`` -> ``calibration_source_mission_id``,
  ``dived_at`` -> ``started_at``, ``flip_dive_slate`` -> ``flip_mission_slate``;
* views: ``current_dive_laser_lines`` and ``dive_laser_geometry`` ->
  ``mission_*``, and the dive columns of every internal view;
* the per-table number sequences, and their triggers, whose argument names
  the sequence as text (0019); every index, constraint and policy named for a
  dive, by the same rule as the code (``dives`` -> ``missions``, then
  ``dive`` -> ``mission``);
* ``dive_pipeline_status_rows()``: a SQL function keeps its body as text, so
  it is recreated, from 0029's frozen rendering with the names above.

**Kept as they are**, because something outside this repo reads them:

* the research views (schema ``v1``; ``fish_length_estimate``,
  ``fish_model_measurement_accuracy`` and ``fish_model_species_mislabel_suspects``,
  0032's research grants) and the research export (``automatic_results_export``):
  their columns are the research repos' interface. A view's stored query
  follows the renames by itself;
* ``dive_pipeline_status``, which Superset's datasets read, columns and all;
* ``v1_refusal_dive_id``, a v1 id, named for what v1 called it.

Temporal-visible names (workflow and activity types, workflow and schedule
ids, error types) are not schema and are pinned in the code.

Revision ID: 0046
Revises: 0045
"""

import importlib.util
import re
from pathlib import Path

from alembic import op

revision = "0046"
down_revision = "0045"

TABLES = {
    "dives": "missions",
    "dive_laser_lines": "mission_laser_lines",
    "dive_frame_clusters": "mission_frame_clusters",
    "dive_frame_cluster_captures": "mission_frame_cluster_captures",
}

#: (table after the table renames, old column, new column).
COLUMNS = [
    ("captures", "dive_id", "mission_id"),
    ("laser_calibrations", "dive_id", "mission_id"),
    ("label_studio_projects", "dive_id", "mission_id"),
    ("mission_frame_clusters", "dive_id", "mission_id"),
    ("mission_laser_lines", "dive_id", "mission_id"),
    ("automatic_laser_calibrations", "dive_id", "mission_id"),
    ("automatic_results_exclusions", "dive_id", "mission_id"),
    ("automatic_results_failures", "dive_id", "mission_id"),
    ("missions", "calibration_source_dive_id", "calibration_source_mission_id"),
    ("missions", "dived_at", "started_at"),
    ("missions", "flip_dive_slate", "flip_mission_slate"),
]

VIEWS = {
    "current_dive_laser_lines": "current_mission_laser_lines",
    "dive_laser_geometry": "mission_laser_geometry",
}

#: (view after the view renames, old column, new column).
VIEW_COLUMNS = [
    ("automatic_measurement_calibrations", "dive_id", "mission_id"),
    ("current_automatic_laser_calibrations", "dive_id", "mission_id"),
    ("current_mission_laser_lines", "dive_id", "mission_id"),
    ("current_laser_calibrations", "dive_id", "mission_id"),
    ("mission_laser_geometry", "dive_id", "mission_id"),
    ("effective_laser_calibrations", "dive_id", "mission_id"),
    ("effective_laser_calibrations", "source_dive_id", "source_mission_id"),
    ("laser_depth_work", "dive_id", "mission_id"),
    ("measurement_work", "dive_id", "mission_id"),
    ("observations", "dive_id", "mission_id"),
    ("observations", "dive_name", "mission_name"),
    ("observations", "dive_number", "mission_number"),
    (
        "slate_presence_evaluation",
        "dive_has_slate_template",
        "mission_has_slate_template",
    ),
    ("slate_presence_evaluation", "dive_id", "mission_id"),
    ("slate_presence_evaluation", "dive_priority", "mission_priority"),
    ("slate_presence_evaluation", "dive_uuid", "mission_uuid"),
]

#: Numbered tables (0019): (table after the rename, old sequence, new).
NUMBERED = [
    ("missions", "dives_number_seq", "missions_number_seq"),
    (
        "mission_laser_lines",
        "dive_laser_lines_number_seq",
        "mission_laser_lines_number_seq",
    ),
    (
        "mission_frame_clusters",
        "dive_frame_clusters_number_seq",
        "mission_frame_clusters_number_seq",
    ),
]
#: The identity sequence of `dive_laser_lines.seq`.
IDENTITY_SEQUENCES = {"dive_laser_lines_seq_seq": "mission_laser_lines_seq_seq"}


def _migration(filename: str):
    spec = importlib.util.spec_from_file_location(
        f"_fishsense_migration_{Path(filename).stem}",
        Path(__file__).with_name(filename),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_0029 = _migration("0029_dive_pipeline_status.py")

#: The function's own output names: Superset's columns, kept.
_OUTPUT = ("RETURNS TABLE (dive_id bigint", "(d.number) AS dive_id")
#: The schema identifiers the function body reads, renamed above.
_IDENTIFIERS = {
    "dives": "missions",
    "dive_id": "mission_id",
    "dive_frame_clusters": "mission_frame_clusters",
    "dive_frame_cluster_captures": "mission_frame_cluster_captures",
    "dive_laser_lines": "mission_laser_lines",
    "current_dive_laser_lines": "current_mission_laser_lines",
    "dive_laser_geometry": "mission_laser_geometry",
    "calibration_source_dive_id": "calibration_source_mission_id",
    "source_dive_id": "source_mission_id",
    "dived_at": "started_at",
    "flip_dive_slate": "flip_mission_slate",
}


def _renamed_function(sql: str) -> str:
    """0029's function with the renamed schema identifiers. Its outputs keep
    their names, and SQL comments (which name v1's functions) are left."""
    for i, kept in enumerate(_OUTPUT):
        assert sql.count(kept) == 1, kept
        sql = sql.replace(kept, f"\x00{i}\x00")

    def line(part: str) -> str:
        code, dash, comment = part.partition("--")
        code = re.sub(
            r"\b[a-z_]+\b", lambda m: _IDENTIFIERS.get(m.group(0), m.group(0)), code
        )
        return code + dash + comment

    out = "".join(line(p) for p in sql.splitlines(keepends=True))
    for i, kept in enumerate(_OUTPUT):
        out = out.replace(f"\x00{i}\x00", kept)
    return out.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


FUNCTION_SQL = _renamed_function(_0029.FUNCTION_SQL)
#: What `FUNCTION_SQL` rendered to. tests/test_dive_pipeline_status_view.py
#: pins it, and checks the function against today's stores on a seeded corpus.
RENDERED_SHA256 = "a54ef97a70740328c4ff2762e98c7fbdcf5158cf30da8c0811769db59819a630"

#: Indexes, constraints and policies: renamed by the code's rule, except those
#: named for `v1_refusal_dive_id`, a column that keeps its name.
_RENAME_NAMES = """
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT c.conrelid::regclass AS rel, c.conname AS name
        FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE n.nspname = 'public' AND c.conname ~ '{old}'
          AND c.conname !~ 'v1_refusal_dive_id'
    LOOP
        EXECUTE format('ALTER TABLE %s RENAME CONSTRAINT %I TO %I', r.rel, r.name,
            regexp_replace(regexp_replace(r.name, '{olds}', '{news}', 'g'),
                           '{old}(?!r)', '{new}', 'g'));
    END LOOP;
    FOR r IN
        SELECT c.relname AS name
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'i' AND c.relname ~ '{old}'
          AND c.relname !~ 'v1_refusal_dive_id'
    LOOP
        EXECUTE format('ALTER INDEX %I RENAME TO %I', r.name,
            regexp_replace(regexp_replace(r.name, '{olds}', '{news}', 'g'),
                           '{old}(?!r)', '{new}', 'g'));
    END LOOP;
    FOR r IN
        SELECT tablename AS rel, policyname AS name FROM pg_policies
        WHERE schemaname = 'public' AND policyname ~ '{old}'
    LOOP
        EXECUTE format('ALTER POLICY %I ON %I RENAME TO %I', r.name, r.rel,
            regexp_replace(regexp_replace(r.name, '{olds}', '{news}', 'g'),
                           '{old}(?!r)', '{new}', 'g'));
    END LOOP;
END
$$
"""


def _numbered(table: str, old_seq: str, new_seq: str, *, old_table: str) -> None:
    op.execute(f"ALTER SEQUENCE {old_seq} RENAME TO {new_seq}")
    op.execute(f"DROP TRIGGER {old_table}_number ON {table}")
    op.execute(f"""
        CREATE TRIGGER {table}_number BEFORE INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION assign_number('{new_seq}')
        """)


def upgrade() -> None:
    for old, new in TABLES.items():
        op.execute(f"ALTER TABLE {old} RENAME TO {new}")
    for table, old, new in COLUMNS:
        op.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
    for old, new in VIEWS.items():
        op.execute(f"ALTER VIEW {old} RENAME TO {new}")
    for view, old, new in VIEW_COLUMNS:
        op.execute(f"ALTER VIEW {view} RENAME COLUMN {old} TO {new}")
    old_tables = {new: old for old, new in TABLES.items()}
    for table, old_seq, new_seq in NUMBERED:
        _numbered(table, old_seq, new_seq, old_table=old_tables[table])
    for old, new in IDENTITY_SEQUENCES.items():
        op.execute(f"ALTER SEQUENCE {old} RENAME TO {new}")
    op.execute(
        _RENAME_NAMES.format(old="dive", new="mission", olds="dives", news="missions")
    )
    op.execute(FUNCTION_SQL)


def downgrade() -> None:
    op.execute(
        _RENAME_NAMES.format(old="mission", new="dive", olds="missions", news="dives")
    )
    for old, new in IDENTITY_SEQUENCES.items():
        op.execute(f"ALTER SEQUENCE {new} RENAME TO {old}")
    new_tables = dict(TABLES)
    for table, old_seq, new_seq in NUMBERED:
        old_table = {v: k for k, v in new_tables.items()}[table]
        op.execute(f"ALTER SEQUENCE {new_seq} RENAME TO {old_seq}")
        op.execute(f"DROP TRIGGER {table}_number ON {table}")
        op.execute(f"""
            CREATE TRIGGER {old_table}_number BEFORE INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION assign_number('{old_seq}')
            """)
    for view, old, new in reversed(VIEW_COLUMNS):
        op.execute(f"ALTER VIEW {view} RENAME COLUMN {new} TO {old}")
    for old, new in VIEWS.items():
        op.execute(f"ALTER VIEW {new} RENAME TO {old}")
    for table, old, new in reversed(COLUMNS):
        op.execute(f"ALTER TABLE {table} RENAME COLUMN {new} TO {old}")
    for old, new in TABLES.items():
        op.execute(f"ALTER TABLE {new} RENAME TO {old}")
    # Last: 0029's function reads the old names.
    op.execute(
        _0029.FUNCTION_SQL.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    )
