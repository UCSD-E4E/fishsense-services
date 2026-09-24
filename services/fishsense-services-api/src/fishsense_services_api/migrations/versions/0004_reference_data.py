"""Global reference data: species, calibration targets, fish-model lengths, slates.

Shared by every tenant and read-only to the app role. Measured reference values
(calibration targets, fish-model lengths) are **versioned by ``valid_from``**:
a correction is a new row, never an edit, and ``current_*`` views give the
latest per name. Slate templates and species are identities, not measurements,
so they aren't versioned.

``v1_id`` keeps the v1 row id for migrated rows (PLAN.md §6.4).

Revision ID: 0004
Revises: 0003
"""

from alembic import context, op

revision = "0004"
down_revision = "0003"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    app_role = _app_role()

    op.execute("""
        CREATE TABLE species (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            scientific_name text UNIQUE,
            common_name text,
            v1_id bigint UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """)
    op.execute("""
        CREATE TABLE calibration_targets (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL,
            interior_rows integer NOT NULL CHECK (interior_rows > 0),
            interior_cols integer NOT NULL CHECK (interior_cols > 0),
            pitch_x_m double precision NOT NULL CHECK (pitch_x_m > 0),
            pitch_y_m double precision NOT NULL CHECK (pitch_y_m > 0),
            notes text,
            valid_from timestamptz NOT NULL DEFAULT now(),
            v1_id bigint UNIQUE,
            UNIQUE (name, valid_from)
        )
        """)
    op.execute("""
        CREATE TABLE fish_model_references (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL,
            known_length_m double precision NOT NULL CHECK (known_length_m > 0),
            is_provisional boolean NOT NULL DEFAULT false,
            notes text,
            valid_from timestamptz NOT NULL DEFAULT now(),
            v1_id bigint UNIQUE,
            UNIQUE (name, valid_from)
        )
        """)
    op.execute("""
        CREATE TABLE slate_templates (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL UNIQUE,
            dpi integer CHECK (dpi > 0),
            source_path text,
            reference_points jsonb NOT NULL,
            v1_id bigint UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """)

    for table in ("calibration_targets", "fish_model_references"):
        op.execute(f"""
            CREATE VIEW current_{table} AS
                SELECT DISTINCT ON (name) *
                FROM {table}
                ORDER BY name, valid_from DESC
            """)

    op.execute(f"""
        GRANT SELECT ON species, calibration_targets, fish_model_references,
            slate_templates, current_calibration_targets,
            current_fish_model_references
        TO {app_role}
        """)


def downgrade() -> None:
    op.execute("DROP VIEW current_fish_model_references")
    op.execute("DROP VIEW current_calibration_targets")
    op.execute("DROP TABLE slate_templates")
    op.execute("DROP TABLE fish_model_references")
    op.execute("DROP TABLE calibration_targets")
    op.execute("DROP TABLE species")
