"""Regional species: more choices for BioCLIP, by where a mission was (the
owner, 2026-10-30).

BioCLIP chose among REEF's 13 target species only, so a fish that is none of
them got the nearest one's name: mission 223's nurse shark became a 2.53 m
"Red Grouper", and the species flag called it longer than a red grouper
grows. The automatic track now also offers the fish commonly recorded where
the mission was, by its broad region (`regions`):

* the source: OBIS checklists (api.obis.org/v3/checklist, retrieved
  2026-10-30) over boxes around where we dive: the Florida Keys reef tract
  (24.3-25.6 N, 80.0-82.5 W); the Caribbean as the union of the Cayman
  Islands, western Cuba (Guanahacabibes), St Lucia and Curacao; Southern
  California (32.4-34.6 N, 117-121 W);
* per region, the 40 ray-finned fish (Actinopterygii, taxon 10194) and the 5
  sharks and rays (Elasmobranchii, 10193) with the most OBIS records,
  freshwater species left out. Rank 1-40 is the fish by records, 41-45 the
  sharks and rays by records;
* common names chosen by hand (WoRMS's first English name is often not the
  usual one: "whip moray" for the spotted eagle ray, "hamlet" for the Nassau
  grouper). BioCLIP is prompted with the scientific name; the common name is
  only what people read.

Records count survey effort as much as abundance, and small fish dominate
(bluehead wrasse, bicolor damselfish): these are the fish a diver sees, not
the ones FishSense targets. They are there so a fish that is not a target has
somewhere to go. Tenants will choose among them later (the owner).

`automatic_species_predictions` records the list a prediction chose from:
its region (NULL for a mission in none: the 13 targets alone) and the lists'
version (`regional_species.SPECIES_LIST_VERSION`). A row with no version, or
an older one, is owed again.

Revision ID: 0085
Revises: 0084
"""

from alembic import context, op
from sqlalchemy import text

revision = "0085"
down_revision = "0084"

TABLE = "regional_species"

#: (region, rank, scientific name, common name, WoRMS AphiaID, OBIS records).
SPECIES = [
    ('Florida Keys, USA', 1, 'Thalassoma bifasciatum', 'Bluehead Wrasse', 159786, 71234),
    ('Florida Keys, USA', 2, 'Stegastes partitus', 'Bicolor Damselfish', 276677, 56722),
    ('Florida Keys, USA', 3, 'Sparisoma aurofrenatum', 'Redband Parrotfish', 273772, 56607),
    ('Florida Keys, USA', 4, 'Scarus iseri', 'Striped Parrotfish', 276052, 55162),
    ('Florida Keys, USA', 5, 'Halichoeres bivittatus', 'Slippery Dick', 158815, 45273),
    ('Florida Keys, USA', 6, 'Haemulon plumierii', 'White Grunt', 158808, 43009),
    ('Florida Keys, USA', 7, 'Acanthurus bahianus', 'Ocean Surgeon', 159578, 41448),
    ('Florida Keys, USA', 8, 'Halichoeres garnoti', 'Yellowhead Wrasse', 275764, 40932),
    ('Florida Keys, USA', 9, 'Ocyurus chrysurus', 'Yellowtail Snapper', 159803, 38313),
    ('Florida Keys, USA', 10, 'Acanthurus coeruleus', 'Blue Tang', 159581, 36871),
    ('Florida Keys, USA', 11, 'Halichoeres maculipinna', 'Clown Wrasse', 275772, 32619),
    ('Florida Keys, USA', 12, 'Sparisoma viride', 'Stoplight Parrotfish', 273780, 28681),
    ('Florida Keys, USA', 13, 'Haemulon sciurus', 'Bluestriped Grunt', 275733, 23463),
    ('Florida Keys, USA', 14, 'Haemulon flavolineatum', 'French Grunt', 275727, 21655),
    ('Florida Keys, USA', 15, 'Acanthurus chirurgus', 'Doctorfish', 159580, 20249),
    ('Florida Keys, USA', 16, 'Lachnolaimus maximus', 'Hogfish', 158822, 18151),
    ('Florida Keys, USA', 17, 'Coryphopterus glaucofraenum', 'Bridled Goby', 159736, 16020),
    ('Florida Keys, USA', 18, 'Abudefduf saxatilis', 'Sergeant Major', 159288, 15920),
    ('Florida Keys, USA', 19, 'Stegastes variabilis', 'Cocoa Damselfish', 276688, 14706),
    ('Florida Keys, USA', 20, 'Caranx ruber', 'Bar Jack', 302432, 13865),
    ('Florida Keys, USA', 21, 'Pomacanthus arcuatus', 'Gray Angelfish', 159287, 13428),
    ('Florida Keys, USA', 22, 'Anisotremus virginicus', 'Porkfish', 279625, 13094),
    ('Florida Keys, USA', 23, 'Sparisoma atomarium', 'Greenblotch Parrotfish', 273771, 12355),
    ('Florida Keys, USA', 24, 'Coryphopterus personatus', 'Masked Goby', 276432, 11690),
    ('Florida Keys, USA', 25, 'Azurina cyanea', 'Blue Chromis', 1565681, 11647),
    ('Florida Keys, USA', 26, 'Canthigaster rostrata', 'Sharpnose Puffer', 127411, 11366),
    ('Florida Keys, USA', 27, 'Haemulon aurolineatum', 'Tomtate', 158807, 11278),
    ('Florida Keys, USA', 28, 'Chaetodon capistratus', 'Foureye Butterflyfish', 159661, 10995),
    ('Florida Keys, USA', 29, 'Lutjanus apodus', 'Schoolmaster', 159793, 9649),
    ('Florida Keys, USA', 30, 'Scarus taeniopterus', 'Princess Parrotfish', 276062, 8235),
    ('Florida Keys, USA', 31, 'Azurina multilineata', 'Brown Chromis', 1623786, 8108),
    ('Florida Keys, USA', 32, 'Calamus calamus', 'Saucereye Porgy', 159242, 8067),
    ('Florida Keys, USA', 33, 'Chaetodon ocellatus', 'Spotfin Butterflyfish', 159662, 7928),
    ('Florida Keys, USA', 34, 'Cephalopholis cruentata', 'Graysby', 279146, 7858),
    ('Florida Keys, USA', 35, 'Holacanthus tricolor', 'Rock Beauty', 276017, 7753),
    ('Florida Keys, USA', 36, 'Pseudupeneus maculatus', 'Spotted Goatfish', 159421, 7623),
    ('Florida Keys, USA', 37, 'Sparisoma rubripinne', 'Yellowtail Parrotfish', 159301, 7557),
    ('Florida Keys, USA', 38, 'Microspathodon chrysurus', 'Yellowtail Damselfish', 281571, 7329),
    ('Florida Keys, USA', 39, 'Stegastes planifrons', 'Threespot Damselfish', 276679, 7326),
    ('Florida Keys, USA', 40, 'Sparisoma chrysopterum', 'Redtail Parrotfish', 273774, 7236),
    ('Florida Keys, USA', 41, 'Ginglymostoma cirratum', 'Nurse Shark', 105846, 1130),
    ('Florida Keys, USA', 42, 'Urobatis jamaicensis', 'Yellow Stingray', 283086, 1061),
    ('Florida Keys, USA', 43, 'Hypanus americanus', 'Southern Stingray', 1042856, 661),
    ('Florida Keys, USA', 44, 'Negaprion brevirostris', 'Lemon Shark', 105800, 288),
    ('Florida Keys, USA', 45, 'Aetobatus narinari', 'Spotted Eagle Ray', 217426, 230),
    ('Caribbean', 1, 'Acanthurus coeruleus', 'Blue Tang', 159581, 992),
    ('Caribbean', 2, 'Sparisoma viride', 'Stoplight Parrotfish', 273780, 781),
    ('Caribbean', 3, 'Haemulon flavolineatum', 'French Grunt', 275727, 581),
    ('Caribbean', 4, 'Sparisoma aurofrenatum', 'Redband Parrotfish', 273772, 559),
    ('Caribbean', 5, 'Thalassoma bifasciatum', 'Bluehead Wrasse', 159786, 513),
    ('Caribbean', 6, 'Chaetodon capistratus', 'Foureye Butterflyfish', 159661, 488),
    ('Caribbean', 7, 'Scarus iseri', 'Striped Parrotfish', 276052, 470),
    ('Caribbean', 8, 'Scarus taeniopterus', 'Princess Parrotfish', 276062, 468),
    ('Caribbean', 9, 'Microspathodon chrysurus', 'Yellowtail Damselfish', 281571, 450),
    ('Caribbean', 10, 'Pterois volitans', 'Red Lionfish', 159559, 449),
    ('Caribbean', 11, 'Lutjanus apodus', 'Schoolmaster', 159793, 449),
    ('Caribbean', 12, 'Abudefduf saxatilis', 'Sergeant Major', 159288, 421),
    ('Caribbean', 13, 'Cephalopholis cruentata', 'Graysby', 279146, 395),
    ('Caribbean', 14, 'Chaetodon striatus', 'Banded Butterflyfish', 159663, 383),
    ('Caribbean', 15, 'Pomacanthus paru', 'French Angelfish', 276025, 383),
    ('Caribbean', 16, 'Aulostomus maculatus', 'Trumpetfish', 278080, 357),
    ('Caribbean', 17, 'Caranx ruber', 'Bar Jack', 302432, 330),
    ('Caribbean', 18, 'Acanthurus bahianus', 'Ocean Surgeon', 159578, 316),
    ('Caribbean', 19, 'Melichthys niger', 'Black Durgon', 219881, 315),
    ('Caribbean', 20, 'Lactophrys triqueter', 'Smooth Trunkfish', 158932, 300),
    ('Caribbean', 21, 'Scarus vetula', 'Queen Parrotfish', 276064, 294),
    ('Caribbean', 22, 'Mulloidichthys martinicus', 'Yellow Goatfish', 277991, 294),
    ('Caribbean', 23, 'Sphyraena barracuda', 'Great Barracuda', 345843, 285),
    ('Caribbean', 24, 'Haemulon sciurus', 'Bluestriped Grunt', 275733, 270),
    ('Caribbean', 25, 'Acanthurus tractus', 'Ocean Surgeonfish', 301914, 268),
    ('Caribbean', 26, 'Holocentrus rufus', 'Longspine Squirrelfish', 276189, 261),
    ('Caribbean', 27, 'Bodianus rufus', 'Spanish Hogfish', 273541, 240),
    ('Caribbean', 28, 'Sparisoma rubripinne', 'Yellowtail Parrotfish', 159301, 237),
    ('Caribbean', 29, 'Canthigaster rostrata', 'Sharpnose Puffer', 127411, 226),
    ('Caribbean', 30, 'Cephalopholis fulva', 'Coney', 279148, 222),
    ('Caribbean', 31, 'Halichoeres bivittatus', 'Slippery Dick', 158815, 219),
    ('Caribbean', 32, 'Diodon hystrix', 'Porcupinefish', 127403, 218),
    ('Caribbean', 33, 'Myripristis jacobus', 'Blackbar Soldierfish', 159385, 216),
    ('Caribbean', 34, 'Bothus lunatus', 'Peacock Flounder', 274187, 212),
    ('Caribbean', 35, 'Scorpaena plumieri', 'Spotted Scorpionfish', 159564, 209),
    ('Caribbean', 36, 'Halichoeres garnoti', 'Yellowhead Wrasse', 275764, 207),
    ('Caribbean', 37, 'Holacanthus tricolor', 'Rock Beauty', 276017, 200),
    ('Caribbean', 38, 'Gymnothorax moringa', 'Spotted Moray', 158584, 199),
    ('Caribbean', 39, 'Azurina multilineata', 'Brown Chromis', 1623786, 193),
    ('Caribbean', 40, 'Epinephelus striatus', 'Nassau Grouper', 159222, 190),
    ('Caribbean', 41, 'Hypanus americanus', 'Southern Stingray', 1042856, 332),
    ('Caribbean', 42, 'Carcharhinus perezii', 'Caribbean Reef Shark', 271324, 93),
    ('Caribbean', 43, 'Aetobatus narinari', 'Spotted Eagle Ray', 217426, 92),
    ('Caribbean', 44, 'Ginglymostoma cirratum', 'Nurse Shark', 105846, 89),
    ('Caribbean', 45, 'Urobatis jamaicensis', 'Yellow Stingray', 283086, 84),
    ('Southern California, USA', 1, 'Paralabrax clathratus', 'Kelp Bass', 282054, 123412),
    ('Southern California, USA', 2, 'Oxyjulis californica', 'Senorita', 240727, 99518),
    ('Southern California, USA', 3, 'Bodianus pulcher', 'California Sheephead', 1702292, 96210),
    ('Southern California, USA', 4, 'Chromis punctipinnis', 'Blacksmith', 273751, 93456),
    ('Southern California, USA', 5, 'Hypsypops rubicundus', 'Garibaldi', 281130, 78024),
    ('Southern California, USA', 6, 'Embiotoca jacksoni', 'Black Perch', 240746, 72939),
    ('Southern California, USA', 7, 'Halichoeres semicinctus', 'Rock Wrasse', 275798, 70825),
    ('Southern California, USA', 8, 'Brachyistius frenatus', 'Kelp Perch', 240721, 66564),
    ('Southern California, USA', 9, 'Engraulis mordax', 'Northern Anchovy', 272286, 57816),
    ('Southern California, USA', 10, 'Girella nigricans', 'Opaleye', 280865, 55177),
    ('Southern California, USA', 11, 'Oxylebius pictus', 'Painted Greenling', 240743, 54639),
    ('Southern California, USA', 12, 'Sebastes atrovirens', 'Kelp Rockfish', 274772, 52254),
    ('Southern California, USA', 13, 'Sebastes mystinus', 'Blue Rockfish', 274823, 51831),
    ('Southern California, USA', 14, 'Phanerodon vacca', 'Pile Perch', 1577347, 47078),
    ('Southern California, USA', 15, 'Embiotoca lateralis', 'Striped Seaperch', 240740, 46177),
    ('Southern California, USA', 16, 'Medialuna californiensis', 'Halfmoon', 281541, 44936),
    ('Southern California, USA', 17, 'Hypsurus caryi', 'Rainbow Seaperch', 240717, 44044),
    ('Southern California, USA', 18, 'Heterostichus rostratus', 'Giant Kelpfish', 281075, 43657),
    ('Southern California, USA', 19, 'Paralabrax nebulifer', 'Barred Sand Bass', 282059, 42570),
    ('Southern California, USA', 20, 'Sardinops sagax', 'Pacific Sardine', 217452, 41594),
    ('Southern California, USA', 21, 'Sebastes paucispinis', 'Bocaccio', 274833, 39910),
    ('Southern California, USA', 22, 'Citharichthys stigmaeus', 'Speckled Sanddab', 275696, 39899),
    ('Southern California, USA', 23, 'Rhacochilus toxotes', 'Rubberlip Seaperch', 282575, 39484),
    ('Southern California, USA', 24, 'Caulolatilus princeps', 'Ocean Whitefish', 276217, 39372),
    ('Southern California, USA', 25, 'Trachurus symmetricus', 'Pacific Jack Mackerel', 273305, 39241),
    ('Southern California, USA', 26, 'Sebastes caurinus', 'Copper Rockfish', 274780, 38868),
    ('Southern California, USA', 27, 'Sebastes serriceps', 'Treefish', 274853, 38765),
    ('Southern California, USA', 28, 'Citharichthys sordidus', 'Pacific Sanddab', 275694, 38659),
    ('Southern California, USA', 29, 'Anisotremus davidsonii', 'Sargo', 279617, 38561),
    ('Southern California, USA', 30, 'Phanerodon furcatus', 'White Seaperch', 240730, 38366),
    ('Southern California, USA', 31, 'Paralichthys californicus', 'California Halibut', 275809, 38361),
    ('Southern California, USA', 32, 'Scorpaenichthys marmoratus', 'Cabezon', 282726, 38332),
    ('Southern California, USA', 33, 'Sebastes carnatus', 'Gopher Rockfish', 274779, 38286),
    ('Southern California, USA', 34, 'Sebastes chrysomelas', 'Black-and-yellow Rockfish', 240737, 38230),
    ('Southern California, USA', 35, 'Scomber japonicus', 'Pacific Chub Mackerel', 127022, 38201),
    ('Southern California, USA', 36, 'Sebastes miniatus', 'Vermilion Rockfish', 274820, 37970),
    ('Southern California, USA', 37, 'Porichthys notatus', 'Plainfin Midshipman', 275658, 37850),
    ('Southern California, USA', 38, 'Sebastes auriculatus', 'Brown Rockfish', 274773, 37827),
    ('Southern California, USA', 39, 'Sebastes diploproa', 'Splitnose Rockfish', 274787, 37771),
    ('Southern California, USA', 40, 'Scorpaena guttata', 'California Scorpionfish', 274713, 37737),
    ('Southern California, USA', 41, 'Urobatis halleri', 'Round Stingray', 283085, 38894),
    ('Southern California, USA', 42, 'Heterodontus francisci', 'Horn Shark', 276694, 38472),
    ('Southern California, USA', 43, 'Triakis semifasciata', 'Leopard Shark', 279060, 38326),
    ('Southern California, USA', 44, 'Myliobatis californica', 'Bat Ray', 271485, 37795),
    ('Southern California, USA', 45, 'Pseudobatos productus', 'Shovelnose Guitarfish', 1043469, 37488),
]  # fmt: skip


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE {TABLE} (
            region text NOT NULL,
            rank integer NOT NULL CONSTRAINT {TABLE}_rank_check CHECK (rank > 0),
            scientific_name text NOT NULL,
            common_name text NOT NULL,
            aphia_id integer NOT NULL,
            obis_records integer NOT NULL,
            PRIMARY KEY (region, scientific_name),
            UNIQUE (region, rank)
        )
        """)
    for region, rank, name, common, aphia, records in SPECIES:
        op.get_bind().execute(
            text(f"INSERT INTO {TABLE} VALUES (:r, :k, :n, :c, :a, :o)"),
            {"r": region, "k": rank, "n": name, "c": common, "a": aphia, "o": records},
        )
    op.execute(f"GRANT SELECT ON {TABLE} TO {_app_role()}")
    op.execute("""
        ALTER TABLE automatic_species_predictions
            ADD COLUMN species_region text,
            ADD COLUMN species_list_version integer
        """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE automatic_species_predictions
            DROP COLUMN species_region,
            DROP COLUMN species_list_version
        """)
    op.execute(f"DROP TABLE {TABLE}")
