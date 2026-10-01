-- Every Label Studio annotation behind every laserlabel row, one row per annotation.
--
-- Run read-only against a RESTORED BACKUP, not prod. How it was run for p2-results.md
-- (T1/T2), on the 2026-09-25T03-00-12Z nightly:
--
--   docker run -d --name p2-fishsense-db -e POSTGRES_PASSWORD=local-only -e POSTGRES_DB=fishsense \
--     -v ~/mnt/fishsense_process_work/database_backups/fishsense:/backups:ro postgres:17
--   docker exec p2-fishsense-db pg_restore -U postgres -d fishsense --no-owner --no-privileges \
--     /backups/2026-09-25T03-00-12Z.dump
--   docker exec p2-fishsense-db psql -U postgres -d fishsense -c "\copy (<this SELECT>) TO STDOUT WITH CSV HEADER"
--
-- Booleans come out as t/f. Parse them explicitly: pandas' astype(bool) makes every
-- non-empty string True, which silently marks every annotation as cancelled.
--
-- `origin` is Label Studio's per-result provenance: 'manual' (drawn), 'prediction'
-- (a pre-annotation accepted as-is), 'prediction-changed' (a pre-annotation moved).
-- It is the field that decides whether two labels of one image are independent.
SELECT l.id AS laserlabel_id, l.image_id, i.dive_id, i.is_canonical, l.superseded, l.completed,
       l.label_studio_task_id, l.label_studio_project_id,
       (a->>'id')::bigint AS ann_id, (a->>'completed_by') AS labeler,
       (a->>'was_cancelled')::bool AS cancelled, (a->>'lead_time')::float AS lead_time,
       (a->>'created_at') AS created_at, (a->>'updated_at') AS updated_at,
       (a->>'parent_prediction') AS parent_prediction, (a->>'parent_annotation') AS parent_annotation,
       (a->>'last_action') AS last_action, (a->>'ground_truth')::bool AS ground_truth,
       (SELECT count(*) FROM jsonb_array_elements(a->'result') r WHERE r->>'type' = 'keypointlabels') AS n_kp,
       (SELECT r->'value'->>'x' FROM jsonb_array_elements(a->'result') r WHERE r->>'type' = 'keypointlabels' LIMIT 1) AS x_pct,
       (SELECT r->'value'->>'y' FROM jsonb_array_elements(a->'result') r WHERE r->>'type' = 'keypointlabels' LIMIT 1) AS y_pct,
       (SELECT r->>'original_width' FROM jsonb_array_elements(a->'result') r WHERE r->>'type' = 'keypointlabels' LIMIT 1) AS ow,
       (SELECT r->>'original_height' FROM jsonb_array_elements(a->'result') r WHERE r->>'type' = 'keypointlabels' LIMIT 1) AS oh,
       (SELECT r->'value'->'keypointlabels'->>0 FROM jsonb_array_elements(a->'result') r WHERE r->>'type' = 'keypointlabels' LIMIT 1) AS kp_label,
       (SELECT r->>'origin' FROM jsonb_array_elements(a->'result') r WHERE r->>'type' = 'keypointlabels' LIMIT 1) AS origin
FROM laserlabel l
JOIN image i ON i.id = l.image_id
CROSS JOIN LATERAL jsonb_array_elements((l.label_studio_json::jsonb)->'annotations') a
WHERE l.label_studio_json IS NOT NULL;
