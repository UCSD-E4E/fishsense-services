-- Full-corpus extraction in the imwut notebook's all.csv schema.
-- One row per Measurement; pipe-delimited because pos/ax/km are JSON.
-- calibration_dive_id = the dive that OWNS the extrinsics actually used
-- (self-calibrated dives therefore show their own id).
-- dot = the lowest-id live laser label with x/y on the image.
\pset format unaligned
\pset fieldsep '|'
SELECT DISTINCT ON (m.id)
       i.dive_id,
       le.dive_id            AS calibration_dive_id,
       d.camera_id,
       split_part(sl.content_of_image, ', ', 2) AS model,
       fmr.known_length_m,
       m.length_m,
       ld.depth_m,
       ll.x || ';' || ll.y    AS dot,
       le.id                 AS leid,
       le.laser_position::text AS pos,
       le.laser_axis::text     AS ax,
       ci.camera_matrix::text  AS km
FROM measurement m
JOIN image i            ON i.id = m.image_id
JOIN dive d             ON d.id = i.dive_id
JOIN specieslabel sl    ON sl.image_id = i.id
JOIN laserextrinsics le ON le.id = m.laser_extrinsics_id
JOIN laserdepth ld      ON ld.image_id = i.id
JOIN laserlabel ll      ON ll.image_id = i.id AND ll.superseded = false
                        AND ll.x IS NOT NULL AND ll.y IS NOT NULL
JOIN cameraintrinsics ci ON ci.camera_id = d.camera_id
JOIN fishmodelreference fmr ON fmr.name = split_part(sl.content_of_image, ', ', 2)
WHERE (sl.content_of_image LIKE 'Fish Model,%' AND trim(sl.content_of_image) <> 'Fish Model,')
   OR sl.content_of_image IN ('Calibration Targets, Box', 'Calibration Targets, Ruler')
ORDER BY m.id, sl.id, ll.id, ci.id;
