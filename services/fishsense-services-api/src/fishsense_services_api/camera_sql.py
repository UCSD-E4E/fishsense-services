"""Which dives the pipeline can rectify, in SQL, for every stage that does.

v2 only. v1 had one camera model, so its cohorts never asked; v2 adds the
axial (flat-port) camera, which must not be undistorted as a pinhole (PLAN.md
§8). Every stage that rectifies a raw frame -- laser 0.1 and prediction,
species 2, head/tail 5.1 -- does it with pinhole maths, so each of them
offers only a dive whose device's current calibration is a pinhole, and
reads intrinsics only from one. A dive a resolver would refuse is not a
candidate: the selector takes the oldest candidate across every tenant, so a
refused dive re-selected every hour would starve them all.

**The port** (PLAN.md §4.9, 2b-2; the owner, 2026-10-20). Pinhole maths is
right only behind the corrective optic. A flat port makes the camera axial, a
dome is pinhole-like only when centred (nothing checks that), and an unknown
port is not yet any of them. So a dive is rectifiable only when its port is
the corrective optic, and every stage built on this predicate inherits it. A
mission whose port is stated otherwise is recorded and shown, but not
processed until the pipeline supports its camera.

Every selector reads one of the three predicates below, and the stages that
also resolve and persist work (laser depth, measurement) read the same one
there, so the cohort, the resolver and the persist check cannot disagree.
"""

__all__ = [
    "ADMITTED_PORT",
    "CORRECTIVE_OPTIC",
    "RECTIFIABLE_CAMERA_MODEL",
    "RECTIFIABLE_MISSION",
    "RECTIFIABLE_PORT",
]

#: The one camera model the pipeline rectifies.
RECTIFIABLE_CAMERA_MODEL = "pinhole"
#: The one port behind which that model holds.
RECTIFIABLE_PORT = "corrective_optic"

#: Dive `d` is behind the corrective optic. Alone, for the stages that act on
#: what the rectifying stages produced (clustering, laser auto-accept, species
#: prediction): a dive restated off the corrective optic after its frames were
#: processed must not keep generating data the page calls "not processed".
CORRECTIVE_OPTIC = f"d.port = '{RECTIFIABLE_PORT}'"

#: Dive `d`, and the dive it borrows its laser calibration from, are behind
#: the corrective optic: for the stages that project through a calibration
#: (laser depth, measurement). A borrowed fit was made through the source's
#: port, so the source's port admits it too. A link is kept even when the dive
#: fits its own, so a restated source refuses the borrower either way: the
#: strict reading, since which fit is effective can change under it.
ADMITTED_PORT = f"""({CORRECTIVE_OPTIC} AND NOT EXISTS (
    SELECT 1 FROM missions src
    WHERE src.tenant_id = d.tenant_id AND src.id = d.calibration_source_mission_id
      AND src.port <> '{RECTIFIABLE_PORT}'
))"""

#: Dive `d` is behind the corrective optic, and its device has a current
#: pinhole calibration.
RECTIFIABLE_MISSION = f"""({CORRECTIVE_OPTIC} AND EXISTS (
    SELECT 1 FROM current_camera_calibrations cc
    WHERE cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
      AND cc.camera_model = '{RECTIFIABLE_CAMERA_MODEL}'
))"""
