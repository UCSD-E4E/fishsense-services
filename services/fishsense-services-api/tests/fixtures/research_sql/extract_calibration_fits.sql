-- Every stored laser calibration, with the object it was fitted from.
--
-- §4.3 claims the dive slate carries the same scale as the checkerboard, and
-- checks it on the laser baseline: the baseline is a property of the rig, so a
-- unit calibrated against both objects must report the same one either way.
-- That check needs the calibration OBJECT per fit, which no other export
-- carries -- `dive.calibration_target_id` is the only place it is recorded.
--
-- A refused calibration has no `laserextrinsics` row at all (the gate refuses
-- to persist rather than flagging), so this returns accepted fits only. That
-- is the intended cohort: the comparison is between calibrations the pipeline
-- would actually use.
--
-- The baseline is hypot(x, y) of `laser_position`; z is 0 by construction.
select
    le.dive_id,
    le.camera_id,
    d.name                as dive_name,
    case when d.calibration_target_id is not null
         then 'checkerboard' else 'slate' end as standard,
    -- The date is not decoration. In this corpus every checkerboard fit is
    -- 14-18 August 2023 and every slate fit 29-31 August, with the cameras
    -- shipped in between, so the calibration OBJECT is perfectly confounded
    -- with the epoch and nothing here can separate the two. Any analysis that
    -- compares the objects has to carry this column and say so.
    to_char(d.dive_datetime, 'YYYY-MM-DD') as dive_date,
    le.laser_position::text as laser_position,
    sqrt(power((le.laser_position->>0)::double precision, 2)
       + power((le.laser_position->>1)::double precision, 2)) as baseline_m
from laserextrinsics le
join dive d on d.id = le.dive_id
order by le.camera_id, standard, le.dive_id;
