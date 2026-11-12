"""Species for the fish on phone captures (the owner, 2026-11-11).

The Fish page listed CCFRP's 530 phone fish as "Unidentified fish": nothing
named them. The app syncs no species, and BioCLIP ran only on the Lite
camera's automatic track. The owner: "let's add the missing species and run
it", and "all of the ccfrp photos were taken in san diego (either la jolla or
oceanside)".

* ``regional_species_additions``: fish a region's 0085 list lacks, added by
  hand with their source. Southern California gains the fish CCFRP measures
  there that OBIS's 45 leave out: Calico and Honeycomb Rockfish, named in
  OCEANS 2025's own data (its Fig. 8; scripts/02_plotting.ipynb), and the
  groundfish the owner agreed were missing (Olive and Starry Rockfish,
  Lingcod), with Rosy and Grass Rockfish and Longfin Sanddab from that
  notebook's own rockfish list, all found off San Diego. The Lite automatic
  track does not read these: adding them there would change what every one
  of its missions is offered, and its `SPECIES_LIST_VERSION` would owe every
  lab species row again (0085). That is a separate decision.
* ``phone_species_predictions``: one row per BioCLIP run on a phone fish,
  naming the server measurement (``phone_measurement_id``) whose mask it was
  cropped to, the region it was offered and the list's version. Append-only,
  tenant-scoped under forced RLS. ``current_phone_species_predictions`` is
  each capture's latest. A prediction for a measurement that is no longer
  the capture's current one is stale, and readers join on both.

Revision ID: 0090
Revises: 0089
"""

from alembic import context, op
from sqlalchemy import text

revision = "0090"
down_revision = "0089"

ADDITIONS = "regional_species_additions"
TABLE = "phone_species_predictions"
ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
STATUSES = ("predicted", "decode_failed")

_CCFRP_DATA = "CCFRP 2025 field data (OCEANS 2025, Fig. 8)"
_CCFRP_LIST = "CCFRP rockfish list (OCEANS 2025, scripts/02_plotting.ipynb)"
_OWNER = "the owner, 2026-11-11 (missing from the OBIS list)"

#: (region, scientific name, common name, source).
SPECIES = (
    ("Southern California, USA", "Sebastes dallii", "Calico Rockfish", _CCFRP_DATA),
    ("Southern California, USA", "Sebastes umbrosus", "Honeycomb Rockfish", _CCFRP_DATA),
    ("Southern California, USA", "Sebastes serranoides", "Olive Rockfish", _OWNER),
    ("Southern California, USA", "Sebastes constellatus", "Starry Rockfish", _OWNER),
    ("Southern California, USA", "Ophiodon elongatus", "Lingcod", _OWNER),
    ("Southern California, USA", "Sebastes rosaceus", "Rosy Rockfish", _CCFRP_LIST),
    ("Southern California, USA", "Sebastes rastrelliger", "Grass Rockfish", _CCFRP_LIST),
    ("Southern California, USA", "Citharichthys xanthostigma", "Longfin Sanddab",
     _CCFRP_LIST),
)  # fmt: skip


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _values(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    app_role = _app_role()
    op.execute(f"""
        CREATE TABLE {ADDITIONS} (
            region text NOT NULL,
            scientific_name text NOT NULL,
            common_name text NOT NULL,
            source text NOT NULL,
            PRIMARY KEY (region, scientific_name)
        )
        """)
    for region, name, common, source in SPECIES:
        op.get_bind().execute(
            text(f"INSERT INTO {ADDITIONS} VALUES (:r, :n, :c, :s)"),
            {"r": region, "n": name, "c": common, "s": source},
        )
    op.execute(f"GRANT SELECT ON {ADDITIONS} TO {app_role}")

    op.execute(f"""
        CREATE TABLE {TABLE} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            capture_id uuid NOT NULL,
            phone_measurement_id uuid NOT NULL,
            status text NOT NULL
                CONSTRAINT {TABLE}_status_check CHECK (status IN ({_values(STATUSES)})),
            predictor_version integer NOT NULL,
            model_id text NOT NULL,
            predicted_choice text,
            top1_probability double precision,
            margin double precision,
            top5 jsonb NOT NULL DEFAULT '[]'::jsonb,
            species_region text NOT NULL,
            species_list_version integer NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, phone_measurement_id)
                REFERENCES phone_measurements (tenant_id, id),
            CONSTRAINT {TABLE}_predicted_check CHECK (
                status <> 'predicted' OR predicted_choice IS NOT NULL)
        )
        """)
    op.execute(f"CREATE INDEX {TABLE}_tenant_id_capture_id_seq_idx "
               f"ON {TABLE} (tenant_id, capture_id, seq)")  # fmt: skip
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {TABLE}
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    # Append-only: no UPDATE, no DELETE.
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO {app_role}")
    op.execute(f"""
        CREATE VIEW current_{TABLE} WITH (security_invoker = true) AS
        SELECT DISTINCT ON (tenant_id, capture_id) *
        FROM {TABLE}
        ORDER BY tenant_id, capture_id, seq DESC
        """)
    op.execute(f"GRANT SELECT ON current_{TABLE} TO {app_role}")


def downgrade() -> None:
    op.execute(f"DROP VIEW current_{TABLE}")
    op.execute(f"DROP TABLE {TABLE}")
    op.execute(f"DROP TABLE {ADDITIONS}")
