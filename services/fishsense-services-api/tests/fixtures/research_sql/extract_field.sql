-- The seven Florida-reef deployments behind PAPER.md section 4.5.
-- One row per Measurement of a real (non-model) fish; pipe-delimited to match
-- the other extractions in this directory.
--
-- The cohort is the seven dives with wild-fish measurements, all of which
-- self-calibrate (calibration_dive_id IS NULL on every one as of 2026-09-14).
-- `fish_id` is the individual: stage 14 binds one Fish row per animal per
-- dive, from the LABEL_STUDIO cluster, so repeat frames of one animal share
-- it. That is what makes the within-fish spread a repeatability rather than a
-- population spread.
--
-- `camera_id` is the rig, and it is NOT the dive: section 4.5's unit-to-unit
-- comparison groups by camera, and two of the seven deployments share one.
--
-- `laser_extrinsics_id` is carried so provenance can be checked: a NULL here
-- is an orphaned row (FINDINGS section 9.4) and must be zero.
--
-- Deliberately NOT filtered on head/tail or laser-label validity. A
-- Measurement exists only where stage 14 found all of them, so the join would
-- be redundant, and adding it silently drops rows whose labels were later
-- superseded -- which would make the count depend on when the export ran.
\pset format unaligned
\pset fieldsep '|'
SELECT i.dive_id,
       d.camera_id,
       m.fish_id,
       m.image_id,
       m.id                  AS measurement_id,
       m.laser_extrinsics_id,
       m.length_m,
       ld.range_m,
       ld.depth_m,
       split_part(split_part(sl.content_of_image, ', ', 2), ' (', 1) AS species
FROM measurement m
JOIN image i         ON i.id = m.image_id
JOIN dive d          ON d.id = i.dive_id
JOIN specieslabel sl ON sl.image_id = i.id AND sl.completed
LEFT JOIN laserdepth ld ON ld.image_id = i.id
WHERE sl.content_of_image LIKE 'Fish,%'
  AND i.dive_id IN (279, 341, 347, 349, 383, 465, 471)
ORDER BY i.dive_id, m.fish_id, m.image_id;
