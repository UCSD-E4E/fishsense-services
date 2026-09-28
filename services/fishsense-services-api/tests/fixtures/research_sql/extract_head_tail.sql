-- Head/tail label pixels for the accuracy cohort, with each camera's full
-- intrinsics INCLUDING distortion coefficients.
--
-- corpus.csv carries the laser dot but not the clicked endpoints, and the dot
-- is the wrong quantity for any question about lens distortion: distortion acts
-- on where the ENDPOINTS sit, and dot radius differs from endpoint radius by
-- half the apparent span, which is itself proportional to 1/range. Using the
-- dot as a proxy gives the wrong answer -- it reports a radial effect that the
-- endpoints show is not there.
--
-- Labels are clicked on UNDISTORTED images, so `distortion_coefficients` is the
-- model that was already removed; what it bounds is the residual.
\pset format unaligned
\pset fieldsep '|'
SELECT DISTINCT ON (m.id)
       i.dive_id,
       m.id                    AS measurement_id,
       d.camera_id,
       split_part(sl.content_of_image, ', ', 2) AS model,
       fmr.known_length_m,
       m.length_m,
       ld.depth_m,
       hl.head_x, hl.head_y, hl.tail_x, hl.tail_y,
       ci.camera_matrix::text           AS km,
       ci.distortion_coefficients::text AS dist
FROM measurement m
JOIN image i             ON i.id = m.image_id
JOIN dive d              ON d.id = i.dive_id
JOIN specieslabel sl     ON sl.image_id = i.id
JOIN headtaillabel hl    ON hl.image_id = i.id AND hl.head_x IS NOT NULL
JOIN laserdepth ld       ON ld.image_id = i.id
JOIN cameraintrinsics ci ON ci.camera_id = d.camera_id
JOIN fishmodelreference fmr ON fmr.name = split_part(sl.content_of_image, ', ', 2)
WHERE i.dive_id IN (58,59,61,66,84,495,497,498,500,501,503,504,506,507,519,520,521,522,527)
  AND ((sl.content_of_image LIKE 'Fish Model,%' AND trim(sl.content_of_image) <> 'Fish Model,')
       OR sl.content_of_image IN ('Calibration Targets, Box','Calibration Targets, Ruler'))
ORDER BY m.id, sl.id, hl.id, ci.id;
