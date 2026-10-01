/* Superset virtual dataset `pipeline_partial_dives` (uuid
   44444444-4444-4444-8444-444444444444): one row per unfinished HIGH-priority
   dive with its current blocker. Verbatim from fishsense-lite@77e8f8e5
   deploy/incus/superset_volumes/docker/assets/datasets/FishSense/
   pipeline_partial_dives.yaml `sql`; it reads v2's `dive_pipeline_status`,
   which keeps v1's column names and upper-case priority.
   tests/test_superset_datasets.py runs it. */
SELECT dive_id, priority,
  laser_labeling_complete, species_labeling_complete, headtail_labeling_complete,
  slate_labeling_complete, calibrated, measured,
  CASE
    WHEN NOT laser_labeling_complete THEN 'needs laser'
    WHEN calibrated AND NOT measured THEN 'ready to measure'
    WHEN species_labeling_complete AND headtail_labeling_complete AND dive_slate_id IS NULL THEN 'blocked: no slate'
    WHEN NOT species_labeling_complete AND NOT headtail_labeling_complete THEN 'needs species + headtail'
    WHEN NOT species_labeling_complete THEN 'needs species'
    WHEN NOT headtail_labeling_complete THEN 'needs headtail'
    ELSE 'other'
  END AS blocker
FROM dive_pipeline_status
WHERE priority = 'HIGH' AND NOT measured
ORDER BY dive_id
