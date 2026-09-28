/* Superset virtual dataset `pipeline_labeling_queue` (uuid
   33333333-3333-4333-8333-333333333333): count of HIGH-priority dives waiting
   at each labeling / processing stage. Verbatim from fishsense-lite@77e8f8e5
   deploy/incus/superset_volumes/docker/assets/datasets/FishSense/
   pipeline_labeling_queue.yaml `sql`; it reads v2's `dive_pipeline_status`,
   which keeps v1's column names and upper-case priority.
   tests/test_superset_datasets.py runs it. */
SELECT stage, cnt FROM (VALUES
  ('1 laser',    (SELECT count(*) FROM dive_pipeline_status WHERE priority = 'HIGH' AND laser_preprocessed AND NOT laser_labeling_complete)),
  ('2 species',  (SELECT count(*) FROM dive_pipeline_status WHERE priority = 'HIGH' AND has_prediction_clusters AND dive_images_preprocessed AND NOT species_labeling_complete)),
  ('3 headtail', (SELECT count(*) FROM dive_pipeline_status WHERE priority = 'HIGH' AND headtail_preprocessed AND NOT headtail_labeling_complete)),
  ('4 slate',    (SELECT count(*) FROM dive_pipeline_status WHERE priority = 'HIGH' AND slate_applicable AND slate_preprocessed AND NOT slate_labeling_complete)),
  ('5 measure',  (SELECT count(*) FROM dive_pipeline_status WHERE priority = 'HIGH' AND calibrated AND NOT measured))
) AS t(stage, cnt)
