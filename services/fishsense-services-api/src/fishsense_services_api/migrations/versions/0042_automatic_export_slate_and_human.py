"""The automatic export drops slate frames and flags human-measured dives.

0036's ``automatic_results_export`` (2026-10-15 gap audit, both decided that
day):

* **slate frames are dropped.** The measure step never measures a frame the
  slate-presence detector calls a slate (p >= 0.5), but a length written
  before the detector scored its frame, or before a re-score called it a
  slate, stayed in the export: a slate measured as a fish. It is history now,
  not a result;
* **``dive_human_measured``** (new, last column): the dive has a human
  measurement. Dives humans measure after their automatic run keep their
  automatic lengths, so a reader can prefer the human ones or compare.

Both are read per frame / per dive as index lookups (the slate predicate is
`slate_presence_store.detector_slate_frame`'s, spelled literally), never
through a DISTINCT ON view correlated per row (the 2026-10-14 slate-cohort
timeout).

Revision ID: 0042
Revises: 0041
"""

from alembic import op

revision = "0042"
down_revision = "0041"

VIEW = "automatic_results_export"

#: `slate_presence_store.detector_slate_frame(0.5)`, frozen here: a migration
#: must not change when the module does.
_SLATE = """EXISTS (
                SELECT 1 FROM (
                    SELECT latest.* FROM slate_presence_predictions latest
                    WHERE latest.tenant_id = c.tenant_id AND latest.capture_id = c.id
                    ORDER BY latest.seq DESC
                    LIMIT 1
                ) sp_p
                WHERE sp_p.probability >= 0.5
            )"""

_HUMAN_MEASURED = """EXISTS (
                       SELECT 1 FROM measurements hm
                       JOIN captures hc
                         ON hc.tenant_id = hm.tenant_id AND hc.id = hm.capture_id
                       WHERE hc.tenant_id = d.tenant_id AND hc.dive_id = d.id
                   )"""


def upgrade() -> None:
    # The new column is last, so the view is replaced in place: its grants
    # (the app role's, the research role's) stay.
    op.execute(f"""
        CREATE OR REPLACE VIEW {VIEW} WITH (security_invoker = true) AS
            SELECT m.tenant_id,
                   m.producer,
                   d.id AS dive_id,
                   d.number AS dive_number,
                   c.id AS capture_id,
                   c.number AS capture_number,
                   c.captured_at,
                   m.id AS automatic_measurement_id,
                   m.length_m,
                   m.depth_m,
                   m.algorithm,
                   m.algorithm_version,
                   m.core_version,
                   m.calibration_source,
                   m.automatic_laser_calibration_id,
                   m.laser_calibration_id,
                   m.camera_calibration_id,
                   lf.method AS calibration_method,
                   lf.algorithm_version AS calibration_algorithm_version,
                   lf.o_mag_m AS calibration_o_mag_m,
                   lf.size_ratio AS calibration_size_ratio,
                   lf.frames_used AS calibration_frames_used,
                   h.id AS automatic_head_tail_prediction_id,
                   h.laser_x,
                   h.laser_y,
                   h.laser_confidence,
                   h.laser_predictor_version,
                   h.laser_checkpoint,
                   h.head_x,
                   h.head_y,
                   h.tail_x,
                   h.tail_y,
                   h.mask_bbox,
                   h.sam_score,
                   h.predictor_version AS sam_predictor_version,
                   h.checkpoint AS sam_checkpoint,
                   s.producer AS species_producer,
                   s.predicted_choice AS species_choice,
                   s.top1_probability AS species_probability,
                   s.margin AS species_margin,
                   s.predictor_version AS species_predictor_version,
                   s.model_id AS species_model_id,
                   m.created_at,
                   {_HUMAN_MEASURED} AS dive_human_measured
            FROM current_automatic_measurements m
            JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
            JOIN dives d ON d.tenant_id = c.tenant_id AND d.id = c.dive_id
            JOIN automatic_head_tail_predictions h
              ON h.tenant_id = m.tenant_id AND h.id = m.automatic_head_tail_prediction_id
            LEFT JOIN automatic_laser_calibrations lf
              ON lf.tenant_id = m.tenant_id AND lf.id = m.automatic_laser_calibration_id
            LEFT JOIN current_automatic_species_predictions s
              ON s.tenant_id = m.tenant_id AND s.capture_id = m.capture_id
             AND s.automatic_head_tail_prediction_id = m.automatic_head_tail_prediction_id
            WHERE m.length_m IS NOT NULL
              AND NOT {_SLATE}
        """)


def downgrade() -> None:
    # A column can't be dropped from a view in place: recreate 0036's, and
    # its grants.
    grants = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT grantee FROM information_schema.role_table_grants "
            f"WHERE table_name = '{VIEW}' AND privilege_type = 'SELECT' "
            "AND grantee <> current_user"
        )
        .scalars()
        .all()
    )
    op.execute(f"DROP VIEW {VIEW}")
    op.execute(f"""
        CREATE VIEW {VIEW} WITH (security_invoker = true) AS
            SELECT m.tenant_id,
                   m.producer,
                   d.id AS dive_id,
                   d.number AS dive_number,
                   c.id AS capture_id,
                   c.number AS capture_number,
                   c.captured_at,
                   m.id AS automatic_measurement_id,
                   m.length_m,
                   m.depth_m,
                   m.algorithm,
                   m.algorithm_version,
                   m.core_version,
                   m.calibration_source,
                   m.automatic_laser_calibration_id,
                   m.laser_calibration_id,
                   m.camera_calibration_id,
                   lf.method AS calibration_method,
                   lf.algorithm_version AS calibration_algorithm_version,
                   lf.o_mag_m AS calibration_o_mag_m,
                   lf.size_ratio AS calibration_size_ratio,
                   lf.frames_used AS calibration_frames_used,
                   h.id AS automatic_head_tail_prediction_id,
                   h.laser_x,
                   h.laser_y,
                   h.laser_confidence,
                   h.laser_predictor_version,
                   h.laser_checkpoint,
                   h.head_x,
                   h.head_y,
                   h.tail_x,
                   h.tail_y,
                   h.mask_bbox,
                   h.sam_score,
                   h.predictor_version AS sam_predictor_version,
                   h.checkpoint AS sam_checkpoint,
                   s.producer AS species_producer,
                   s.predicted_choice AS species_choice,
                   s.top1_probability AS species_probability,
                   s.margin AS species_margin,
                   s.predictor_version AS species_predictor_version,
                   s.model_id AS species_model_id,
                   m.created_at
            FROM current_automatic_measurements m
            JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
            JOIN dives d ON d.tenant_id = c.tenant_id AND d.id = c.dive_id
            JOIN automatic_head_tail_predictions h
              ON h.tenant_id = m.tenant_id AND h.id = m.automatic_head_tail_prediction_id
            LEFT JOIN automatic_laser_calibrations lf
              ON lf.tenant_id = m.tenant_id AND lf.id = m.automatic_laser_calibration_id
            LEFT JOIN current_automatic_species_predictions s
              ON s.tenant_id = m.tenant_id AND s.capture_id = m.capture_id
             AND s.automatic_head_tail_prediction_id = m.automatic_head_tail_prediction_id
            WHERE m.length_m IS NOT NULL
        """)
    for grantee in grants:
        op.execute(f'GRANT SELECT ON {VIEW} TO "{grantee}"')
