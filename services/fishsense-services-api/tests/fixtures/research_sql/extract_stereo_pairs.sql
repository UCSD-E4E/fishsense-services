-- Our measurements of the 2023-08-03 morning individuals, for the paired
-- comparison against the SMILE stereo archive (FINDINGS section 12).
--
-- One row per Measurement. Each dive folder is ONE named individual
-- (`Hogfish01_MolHITW_0926_080323`), which is what makes the pairing possible:
-- the archive numbers the same fish the same way.
--
-- All eight dives borrow dive 32's LaserExtrinsics (10.551 cm, fitted
-- 2026-09-16 from `H Slate Dive 1` at 09:11) via `Dive.calibration_dive_id`,
-- so `laser_extrinsics_id` is constant and a calibration scale error is
-- COMMON-MODE across every row here. That is what lets the within-individual
-- range trend below test the calibration at all.
--
-- `head_tail_px` is the clicked landmark separation; with `range_m` and the
-- camera's fx it reproduces `length_m` to ~0.3 %, which is how the projection
-- was verified self-consistent. `range_m` is NULL where the hourly laser-depth
-- stage has not yet reached the frame.
\pset format unaligned
\pset fieldsep '|'
SELECT i.dive_id,
       d.name                AS dive_name,
       m.fish_id,
       m.image_id,
       m.laser_extrinsics_id,
       m.length_m,
       ld.range_m,
       ld.depth_m,
       sqrt(power(h.head_x - h.tail_x, 2) + power(h.head_y - h.tail_y, 2)) AS head_tail_px,
       split_part(split_part(sl.content_of_image, ', ', 2), ' (', 1)       AS species,
       sl.fish_measurable_category,
       sl.top_three_photos_of_group
FROM measurement m
JOIN image i            ON i.id = m.image_id
JOIN dive d             ON d.id = i.dive_id
LEFT JOIN laserdepth ld ON ld.image_id = i.id
LEFT JOIN headtaillabel h ON h.image_id = i.id
                          AND h.completed AND NOT h.superseded
                          AND h.head_x IS NOT NULL
LEFT JOIN specieslabel sl ON sl.image_id = i.id
                          AND sl.label_studio_project_id IS NOT NULL
                          AND NOT coalesce(sl.superseded, false)
WHERE i.dive_id IN (5, 8, 16, 20, 25, 28, 35, 39)
ORDER BY i.dive_id, m.image_id;
