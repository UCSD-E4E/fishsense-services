-- Foreshortening experiment export (data/angles.csv): one row per measured
-- frame of the five Snook angle sessions, with the labelled fish angle.
-- Dive 526 (the sixth session) is parked and has no measurements; it does not
-- appear here because the JOIN on measurement is inner.
-- Run:  psql ... -At -F, -f extract_angles.sql > angles.csv   (then add the
-- header line data/angles.csv carries).
SELECT i.dive_id,
       i.id                    AS image_id,
       i.taken_datetime,
       sl.fish_angle_degrees,
       sl.fish_angle_category  AS angle_category,
       m.length_m,
       ld.depth_m
FROM specieslabel sl
JOIN image i        ON i.id = sl.image_id
JOIN measurement m  ON m.image_id = i.id
LEFT JOIN laserdepth ld ON ld.image_id = i.id
WHERE i.dive_id IN (87, 94, 103, 107, 114)
  AND sl.completed
  AND NOT sl.superseded
  AND sl.content_of_image LIKE 'Fish Model, Snook%'
ORDER BY i.dive_id, i.taken_datetime, i.id;
