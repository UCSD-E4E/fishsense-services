"""Species length references (the owner, 2026-10-30).

The 13 species BioCLIP chooses among, each with its published maximum
length (FishBase's summary page, max total length, retrieved 2026-10-30) and
its typical log length in our automatic lengths: the median and MAD of
log(length m) over the 23,024 current automatic lengths of the lab on
2026-10-30, fish models left out, by BioCLIP's species. `species_length`
flags a length against them; the automatic lengths list and the review page
carry the flag. Frozen here, not recomputed: a typical range that moved
with each run would move to meet the errors it flags. Global, not a
tenant's: read by every tenant, written only by a migration.

Revision ID: 0084
Revises: 0083
"""

from alembic import context, op
from sqlalchemy import text

revision = "0084"
down_revision = "0083"

TABLE = "species_length_references"

#: (scientific name, max length m, its source, log median, log MAD, n).
REFERENCES = [
    ("Epinephelus itajara", 2.5, "https://www.fishbase.se/summary/Epinephelus-itajara.html", -1.3465, 0.3532, 2348),  # Goliath Grouper
    ("Epinephelus morio", 1.25, "https://www.fishbase.se/summary/Epinephelus-morio.html", -1.3541, 0.297, 1299),  # Red Grouper
    ("Epinephelus striatus", 1.22, "https://www.fishbase.se/summary/Epinephelus-striatus.html", -0.8336, 0.2067, 1138),  # Nassau Grouper
    ("Lachnolaimus maximus", 0.91, "https://www.fishbase.se/summary/Lachnolaimus-maximus.html", -1.4823, 0.1473, 2977),  # Hogfish
    ("Lutjanus analis", 0.94, "https://www.fishbase.se/summary/Lutjanus-analis.html", -1.4639, 0.3018, 1107),  # Mutton Snapper
    ("Lutjanus griseus", 0.89, "https://www.fishbase.se/summary/Lutjanus-griseus.html", -1.4156, 0.2377, 2207),  # Grey Snapper
    ("Mycteroperca bonaci", 1.5, "https://www.fishbase.se/summary/Mycteroperca-bonaci.html", -1.3724, 0.365, 386),  # Black Grouper
    ("Mycteroperca interstitialis", 0.84, "https://www.fishbase.se/summary/Mycteroperca-interstitialis.html", -1.541, 0.2376, 2655),  # Yellowmouth Grouper
    ("Ocyurus chrysurus", 0.863, "https://www.fishbase.se/summary/Ocyurus-chrysurus.html", -1.5621, 0.1649, 398),  # Yellowtail Snapper
    ("Scarus coelestinus", 0.77, "https://www.fishbase.se/summary/Scarus-coelestinus.html", -1.2992, 0.2722, 3125),  # Midnight Parrotfish
    ("Scarus coeruleus", 1.2, "https://www.fishbase.se/summary/Scarus-coeruleus.html", -1.3448, 0.198, 1077),  # Blue Parrotfish
    ("Scarus guacamaia", 1.2, "https://www.fishbase.se/summary/Scarus-guacamaia.html", -1.1558, 0.22, 800),  # Rainbow Parrotfish
    ("Sparisoma viride", 0.64, "https://www.fishbase.se/summary/Sparisoma-viride.html", -1.5025, 0.1735, 3507),  # Stoplight Parrotfish
]  # fmt: skip


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE {TABLE} (
            scientific_name text PRIMARY KEY,
            max_length_m double precision NOT NULL
                CONSTRAINT {TABLE}_max_check CHECK (max_length_m > 0),
            max_length_source text NOT NULL,
            log_median double precision,
            log_mad double precision
                CONSTRAINT {TABLE}_mad_check CHECK (log_mad > 0),
            typical_n integer,
            typical_derived_on date
        )
        """)
    for name, max_m, source, median, mad, n in REFERENCES:
        op.get_bind().execute(
            text(f"""
                INSERT INTO {TABLE} VALUES
                    (:name, :max_m, :source, :median, :mad, :n, DATE '2026-10-30')
                """),
            {"name": name, "max_m": max_m, "source": source, "median": median,
             "mad": mad, "n": n},
        )  # fmt: skip
    op.execute(f"GRANT SELECT ON {TABLE} TO {_app_role()}")


def downgrade() -> None:
    op.execute(f"DROP TABLE {TABLE}")
