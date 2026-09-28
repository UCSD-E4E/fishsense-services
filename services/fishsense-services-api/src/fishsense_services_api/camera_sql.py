"""Which dives the pipeline can rectify, in SQL, for every stage that does.

v2 only. v1 had one camera model, so its cohorts never asked; v2 adds the
axial (flat-port) camera, which must not be undistorted as a pinhole (PLAN.md
§8). Every stage that rectifies a raw frame -- laser 0.1 and prediction,
species 2, head/tail 5.1 -- does it with pinhole maths, so each of them
offers only a dive whose device's current calibration is a pinhole, and
reads intrinsics only from one. A dive a resolver would refuse is not a
candidate: the selector takes the oldest candidate across every tenant, so a
refused dive re-selected every hour would starve them all.
"""

__all__ = ["RECTIFIABLE_CAMERA_MODEL", "RECTIFIABLE_DIVE"]

#: The one camera model the pipeline rectifies.
RECTIFIABLE_CAMERA_MODEL = "pinhole"

#: Dive `d`'s device has a current pinhole calibration.
RECTIFIABLE_DIVE = f"""EXISTS (
    SELECT 1 FROM current_camera_calibrations cc
    WHERE cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
      AND cc.camera_model = '{RECTIFIABLE_CAMERA_MODEL}'
)"""
