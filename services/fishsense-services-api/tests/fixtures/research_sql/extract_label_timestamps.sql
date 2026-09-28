-- When each dive's images were first labelled, per label kind. For the REEF turnaround
-- analysis (p2-results.md, "Why this paper"). Run against a restored backup; see
-- extract_laser_annotations.sql for the recipe.
--
-- `measurement` carries no timestamp, so "measurable" is reconstructed from the labels a
-- measurement needs. Each image's FIRST annotation per kind is used: T1 showed images
-- are re-labelled in later campaigns (~260 days on), and the latest annotation would
-- stretch the turnaround with work that was not the first pass.
WITH ann AS (
  SELECT 'laser' AS kind, l.image_id, i.dive_id, (a->>'created_at')::timestamptz AS t
    FROM laserlabel l JOIN image i ON i.id = l.image_id
    CROSS JOIN LATERAL jsonb_array_elements((l.label_studio_json::jsonb)->'annotations') a
   WHERE NOT coalesce((a->>'was_cancelled')::bool, false)
  UNION ALL
  SELECT 'headtail', l.image_id, i.dive_id, (a->>'created_at')::timestamptz
    FROM headtaillabel l JOIN image i ON i.id = l.image_id
    CROSS JOIN LATERAL jsonb_array_elements((l.label_studio_json::jsonb)->'annotations') a
   WHERE NOT coalesce((a->>'was_cancelled')::bool, false)
  UNION ALL
  SELECT 'species', l.image_id, i.dive_id, (a->>'created_at')::timestamptz
    FROM specieslabel l JOIN image i ON i.id = l.image_id
    CROSS JOIN LATERAL jsonb_array_elements((l.label_studio_json::jsonb)->'annotations') a
   WHERE NOT coalesce((a->>'was_cancelled')::bool, false)
), first_per_image AS (
  SELECT kind, dive_id, image_id, min(t) AS t FROM ann GROUP BY kind, dive_id, image_id
)
SELECT d.id AS dive_id, d.path, d.dive_datetime, f.kind, count(*) AS images, min(f.t) AS first_t,
       to_timestamp(percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM f.t))) AS p90_t
FROM dive d JOIN first_per_image f ON f.dive_id = d.id
GROUP BY d.id, d.path, d.dive_datetime, f.kind;
