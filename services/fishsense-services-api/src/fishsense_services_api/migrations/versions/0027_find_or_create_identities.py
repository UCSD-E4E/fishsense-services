"""Find-or-create a species or a fish model at measure time -- and nothing more.

v1's stage 14 (fishsense-lite@77e8f8e5 measure_fish_activity.py
`_ensure_species`, `_ensure_model_fish`) created a `species` row the first
time a labeler's `Common (Scientific)` leaf was measured, and a named `fish`
for any `Fish Model, <name>` leaf. Both tables are global reference data in v2
(0004, 0012), and the schema audit forbids the app role any write to them --
correctly: their rows are shared by every tenant, and the reference *values*
(lengths, versioned) must never be editable from a request.

**Decided:** identities, not values, may be added -- through two narrow
functions, never a table grant:

- ``ensure_species(scientific_name, common_name)``: the species with that
  scientific name, added if it is new. An existing row is returned as it is;
  nothing is updated, renamed or deleted.
- ``ensure_fish_model(name)``: the fish model with that name, added if new.
  It carries no length: a reference length stays a versioned
  ``fish_model_references`` row that only a migration or an admin adds.

Why not the alternatives:

- *grant INSERT*: the audit's rule for global tables is "read, never write",
  and INSERT alone would still let a request fill them with anything;
- *require registration* (measure only models already in ``fish_models``): the
  stage-14 cohort would then need a registry join that the taxonomy predicates
  (and so the pipeline-status view) lack -- the cohort/activity disagreement
  that wedges a dive the day a model is added to the labeling config first.
  v1 measured every `Fish Model,` leaf, and ungraded models still measure.

``SECURITY DEFINER`` with a pinned ``search_path``: they run as their owner
(the migration role), so no caller can redirect `species` to a table of its
own. EXECUTE is revoked from PUBLIC and granted to the app role only.

Revision ID: 0027
Revises: 0026
"""

from alembic import context, op

revision = "0027"
down_revision = "0026"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    app_role = _app_role()
    op.execute("""
        CREATE FUNCTION ensure_species(p_scientific_name text, p_common_name text)
        RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE
            found uuid;
        BEGIN
            IF p_scientific_name IS NULL OR btrim(p_scientific_name) = '' THEN
                RAISE EXCEPTION 'a species needs a scientific name'
                    USING ERRCODE = 'check_violation';
            END IF;
            INSERT INTO public.species (scientific_name, common_name)
            VALUES (p_scientific_name, p_common_name)
            ON CONFLICT (scientific_name) DO NOTHING;
            SELECT id INTO found FROM public.species
            WHERE scientific_name = p_scientific_name;
            RETURN found;
        END
        $$
        """)
    op.execute("""
        CREATE FUNCTION ensure_fish_model(p_name text)
        RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE
            found uuid;
        BEGIN
            IF p_name IS NULL OR btrim(p_name) = '' THEN
                RAISE EXCEPTION 'a fish model needs a name'
                    USING ERRCODE = 'check_violation';
            END IF;
            INSERT INTO public.fish_models (name) VALUES (p_name)
            ON CONFLICT (name) DO NOTHING;
            SELECT id INTO found FROM public.fish_models WHERE name = p_name;
            RETURN found;
        END
        $$
        """)
    for signature in ("ensure_species(text, text)", "ensure_fish_model(text)"):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {app_role}")


def downgrade() -> None:
    op.execute("DROP FUNCTION ensure_fish_model(text)")
    op.execute("DROP FUNCTION ensure_species(text, text)")
