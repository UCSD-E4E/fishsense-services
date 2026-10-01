# Superset dashboards-as-code

> **fishsense-services:** moved from fishsense-lite's
> `deploy/incus/superset_volumes/docker/assets` with every uuid kept, so the
> first v2 import updates v1's objects in place. What changed:
>
> - `databases/FishSense.yaml` points at v2's `fishsense_services` as the
>   `fishsense_superset` login (in `fishsense_analytics`, bound to the lab), and
>   its placeholder is `__ANALYTICS_DB_PASSWORD__`;
> - the two virtual pipeline datasets carry `deploy/superset/datasets/*.sql`
>   verbatim (the API's suite runs that SQL against v2; `deploy/tests` pins the
>   copy); `dive_pipeline_status` is v2's view with v1's column names;
> - `fish_measurements` reads the `v1` research views, which the analytics role
>   **cannot** read: the Fish Measurements dashboard needs
>   `GRANT fishsense_research TO fishsense_superset` (docs/cutover.md,
>   "What breaks").
>
> The rest of this file is v1's.

Superset assets (database connection, datasets, charts, dashboards) as YAML,
in Superset's native export format. `docker-init.sh` (step 5) re-imports this
bundle on **every converge** — idempotent, overwrites by `uuid` — so the
dashboards are declarative and versioned instead of clicked-in-the-UI.

```
assets/
  metadata.yaml
  databases/FishSense.yaml          # fishsense Postgres connection (password injected at import, see below)
  datasets/FishSense/
    dive_pipeline_status.yaml        # the physical view — one bool per stage per dive
    pipeline_labeling_queue.yaml     # virtual: count of HIGH dives waiting at each stage
    pipeline_partial_dives.yaml      # virtual: one row per unfinished dive + its blocker
    fish_measurements.yaml           # virtual: one row per stage-14 measurement (dive/image/species)
  charts/                            # Labeling queue + Partially-done dives + fish-measurement tiles
  dashboards/FishSense_Pipeline_Status.yaml
  dashboards/Fish_Measurements.yaml  # reshaped from the retired hosted "REEF - SMILE Lengths"
```

Two dashboards ship in this one bundle — `import-dashboards` imports every
dashboard/chart/dataset in the zip in one pass.

## Secret handling

`databases/FishSense.yaml` ships with `__DB_PASSWORD__` in the SQLAlchemy URI.
`docker-init.sh` `sed`-substitutes `$DATABASE_PASSWORD` (the `superset` DB
role's password, already in the container env) into a temp copy before import,
so the credential is **never committed**. The `superset` role can already read
the `fishsense` DB, so no extra grant is needed.

## The maintenance loop (recommended)

This first bundle is **hand-authored** and hasn't been round-tripped through a
running Superset, so the charts/dashboard layout may need a fixup pass. The
durable workflow:

1. Let it import on converge (or `superset import-assets -p bundle.zip` by hand).
2. Adjust datasets/charts/dashboard **in the UI** until they're right.
3. **Settings → Export** (or `superset export-dashboards`) → drop the exported
   YAML back into this directory, keeping the same `uuid`s.
4. Commit — now the running state is the committed state.

CI runs `yamllint` over these files (syntax / duplicate-key checks), but that
does **not** validate Superset-import semantics — the loop above does.

## UUIDs

Assets cross-reference by `uuid` (dataset → `database_uuid`, chart →
`dataset_uuid`, dashboard `position` → chart `uuid`). Keep them stable across
edits so imports update in place instead of creating duplicates.
