"""Automatic results: fish lengths with no human label, as their own track.

New in v2 (no v1 counterpart). The chain is the one cscw-fishsense2027@96a8da07
validated (PAPER.md §6): the production laser detector's dot; a SAM 3.1 fish
mask seeded at that dot, kept only if the dot is on it and SAM scores it
>= 0.5 (§6.3: SAM's own confidence is the best gate); a geometric head/tail
from the mask; a label-free size-constancy laser calibration per dive (§4.3);
a length. BioCLIP's zero-shot species rides on each automatic mask.

**A separate track** (decided 2026-10-06). Nothing automatic is a label, a
human-path prediction or a `measurements` row, so it can never reach
`current_measurements`, `measurement_work`, Label Studio or a human cohort.
Four tables, each appended, never updated or deleted (SELECT and INSERT only),
tenant-scoped under forced RLS, `producer` pinned to ``automatic``, every row
naming its model and algorithm versions:

* ``automatic_head_tail_predictions`` -- per capture: the detector's dot and
  the SAM 3.1 head/tail at it (or which abstention: no dot, no mask, the dot on
  no mask, a slate frame, ...). A ``predicted`` row carries its points, its
  mask's box and a SAM score >= 0.5. Slate frames (the slate-presence
  detector's p >= 0.5) keep their dot and are never measured as fish;
* ``automatic_laser_calibrations`` -- per dive: the label-free fit, accepted
  or refused, with what it saw (frames, pairs, spread, bootstrap SE, the
  frames used) and the |O| it assumed;
* ``automatic_species_predictions`` -- per capture: BioCLIP's zero-shot
  answer on the automatic mask's crop (the shape of 0034's table, naming the
  automatic head/tail it cropped);
* ``automatic_measurements`` -- per capture: a length (or why not), naming the
  automatic head/tail and the calibration used: the dive's own accepted
  label-free fit, else its calibration link's, else its effective stored
  calibration (`calibration_source` ``label_free`` / ``stored``).

**Current** (PLAN.md §9.13): ``current_automatic_*`` are the latest per
subject; ``automatic_measurement_calibrations`` is the calibration each dive's
lengths use now; ``current_automatic_measurements`` is the latest length per
capture whose head/tail and calibration are still those. Stale rows stay as
history. ``automatic_results_export`` is the research/export view: every
current length with all its inputs and its species, readable by the research
role (0032's lab binding, on these tables too).

Additive: new tables and views only.

Revision ID: autoresults_01
Revises: 0034
"""

from alembic import context, op

from fishsense_services_api.schema_audit import (
    RESEARCH_LAB_BINDING,
    RESEARCH_LAB_READ,
    RESEARCH_ROLE,
)

revision = "autoresults_01"
down_revision = "0034"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_LAB = "tenant_id = (SELECT public.research_tenant_id())"

TABLES = (
    "automatic_head_tail_predictions",
    "automatic_laser_calibrations",
    "automatic_species_predictions",
    "automatic_measurements",
)
VIEWS = (
    "current_automatic_head_tail_predictions",
    "current_automatic_laser_calibrations",
    "current_automatic_species_predictions",
    "automatic_measurement_calibrations",
    "current_automatic_measurements",
    "automatic_results_export",
)

#: The SAM 3.1 score a kept mask needs (paper §6.3).
SAM_SCORE_GATE = 0.5


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _producer(table: str) -> str:
    return f"""producer text NOT NULL DEFAULT 'automatic'
            CONSTRAINT {table}_producer_check CHECK (producer = 'automatic')"""


def _three_vector(column: str) -> str:
    return f"(jsonb_typeof({column}) = 'array' AND jsonb_array_length({column}) = 3)"


def _tables() -> None:
    op.execute(f"""
        CREATE TABLE automatic_head_tail_predictions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            {_producer("automatic_head_tail_predictions")},
            capture_id uuid NOT NULL,
            status text NOT NULL
                CONSTRAINT automatic_head_tail_predictions_status_check
                CHECK (status IN ('predicted', 'no_laser_dot', 'no_detections',
                                  'laser_off_all_fish', 'headtail_failed',
                                  'decode_failed', 'raw_unavailable',
                                  'slate_frame')),
            laser_x double precision,
            laser_y double precision,
            laser_confidence double precision,
            laser_predictor_version integer,
            laser_checkpoint text,
            head_x double precision,
            head_y double precision,
            tail_x double precision,
            tail_y double precision,
            width integer,
            height integer,
            mask_area_px integer,
            silhouette_ratio double precision,
            crop_x integer,
            crop_y integer,
            mask_bbox integer[]
                CONSTRAINT automatic_head_tail_predictions_mask_bbox_check CHECK (
                    mask_bbox IS NULL
                    OR (array_ndims(mask_bbox) = 1 AND cardinality(mask_bbox) = 4
                        AND array_position(mask_bbox, NULL) IS NULL)),
            sam_score double precision
                CONSTRAINT automatic_head_tail_predictions_sam_score_check
                CHECK (sam_score BETWEEN 0 AND 1),
            slate_probability double precision
                CONSTRAINT automatic_head_tail_predictions_slate_probability_check
                CHECK (slate_probability BETWEEN 0 AND 1),
            predictor_version integer NOT NULL,
            checkpoint text,
            core_version text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            CONSTRAINT automatic_head_tail_predictions_dot_check
                CHECK ((laser_x IS NULL) = (laser_y IS NULL)),
            CONSTRAINT automatic_head_tail_predictions_predicted_check CHECK (
                status <> 'predicted'
                OR (head_x IS NOT NULL AND head_y IS NOT NULL
                    AND tail_x IS NOT NULL AND tail_y IS NOT NULL
                    AND laser_x IS NOT NULL AND mask_bbox IS NOT NULL
                    AND checkpoint IS NOT NULL)),
            CONSTRAINT automatic_head_tail_predictions_sam_gate_check CHECK (
                status <> 'predicted' OR sam_score >= {SAM_SCORE_GATE}),
            CONSTRAINT automatic_head_tail_predictions_slate_dot_check CHECK (
                status <> 'slate_frame' OR laser_x IS NOT NULL)
        )
        """)
    op.execute("""
        CREATE INDEX automatic_head_tail_predictions_tenant_id_capture_id_seq_idx
            ON automatic_head_tail_predictions (tenant_id, capture_id, seq)
        """)

    op.execute(f"""
        CREATE TABLE automatic_laser_calibrations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            {_producer("automatic_laser_calibrations")},
            dive_id uuid NOT NULL,
            camera_calibration_id uuid,
            method text NOT NULL DEFAULT 'size_constancy'
                CONSTRAINT automatic_laser_calibrations_method_check
                CHECK (method IN ('size_constancy')),
            algorithm_version text NOT NULL,
            outcome text NOT NULL
                CONSTRAINT automatic_laser_calibrations_outcome_check
                CHECK (outcome IN ('accepted', 'refused')),
            refusal_reason text,
            laser_position jsonb,
            laser_axis jsonb,
            vanishing_px double precision,
            line_direction jsonb,
            line_offset_px double precision,
            o_mag_m double precision
                CONSTRAINT automatic_laser_calibrations_o_mag_m_check
                CHECK (o_mag_m > 0),
            frames_used integer NOT NULL DEFAULT 0
                CONSTRAINT automatic_laser_calibrations_frames_used_check
                CHECK (frames_used >= 0),
            candidate_count integer NOT NULL DEFAULT 0
                CONSTRAINT automatic_laser_calibrations_candidate_count_check
                CHECK (candidate_count >= 0),
            pair_count integer NOT NULL DEFAULT 0
                CONSTRAINT automatic_laser_calibrations_pair_count_check
                CHECK (pair_count >= 0),
            size_ratio double precision,
            se_px double precision,
            pair_residual_sd double precision,
            capture_ids uuid[] NOT NULL DEFAULT '{{}}',
            core_version text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, dive_id) REFERENCES dives (tenant_id, id),
            FOREIGN KEY (tenant_id, camera_calibration_id)
                REFERENCES camera_calibrations (tenant_id, id),
            CONSTRAINT automatic_laser_calibrations_accepted_check CHECK (
                outcome <> 'accepted'
                OR ({_three_vector('laser_position')}
                    AND {_three_vector('laser_axis')})),
            CONSTRAINT automatic_laser_calibrations_refusal_reason_check
                CHECK ((outcome = 'refused') = (refusal_reason IS NOT NULL))
        )
        """)

    op.execute(f"""
        CREATE TABLE automatic_species_predictions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            {_producer("automatic_species_predictions")},
            capture_id uuid NOT NULL,
            automatic_head_tail_prediction_id uuid NOT NULL,
            status text NOT NULL
                CONSTRAINT automatic_species_predictions_status_check
                CHECK (status IN ('predicted', 'decode_failed')),
            predictor_version integer NOT NULL,
            model_id text NOT NULL,
            predicted_choice text,
            top1_probability double precision
                CONSTRAINT automatic_species_predictions_top1_probability_check
                CHECK (top1_probability BETWEEN 0 AND 1),
            margin double precision
                CONSTRAINT automatic_species_predictions_margin_check
                CHECK (margin BETWEEN 0 AND 1),
            top5 jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, automatic_head_tail_prediction_id)
                REFERENCES automatic_head_tail_predictions (tenant_id, id),
            CONSTRAINT automatic_species_predictions_scores_check CHECK (
                (status = 'predicted') = (
                    predicted_choice IS NOT NULL
                    AND top1_probability IS NOT NULL
                    AND margin IS NOT NULL
                    AND jsonb_array_length(top5) > 0))
        )
        """)
    op.execute("""
        CREATE INDEX automatic_species_predictions_tenant_id_capture_id_seq_idx
            ON automatic_species_predictions (tenant_id, capture_id, seq)
        """)

    op.execute(f"""
        CREATE TABLE automatic_measurements (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            {_producer("automatic_measurements")},
            capture_id uuid NOT NULL,
            automatic_head_tail_prediction_id uuid NOT NULL,
            calibration_source text NOT NULL
                CONSTRAINT automatic_measurements_calibration_source_check
                CHECK (calibration_source IN ('label_free', 'stored')),
            automatic_laser_calibration_id uuid,
            laser_calibration_id uuid,
            camera_calibration_id uuid,
            length_m double precision
                CONSTRAINT automatic_measurements_length_m_check
                CHECK (length_m > 0),
            depth_m double precision,
            refusal text
                CONSTRAINT automatic_measurements_refusal_check
                CHECK (refusal IN ('non_positive_depth', 'non_finite_length',
                                   'zero_length')),
            algorithm text NOT NULL,
            algorithm_version text NOT NULL,
            core_version text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, automatic_head_tail_prediction_id)
                REFERENCES automatic_head_tail_predictions (tenant_id, id),
            FOREIGN KEY (tenant_id, automatic_laser_calibration_id)
                REFERENCES automatic_laser_calibrations (tenant_id, id),
            FOREIGN KEY (tenant_id, laser_calibration_id)
                REFERENCES laser_calibrations (tenant_id, id),
            FOREIGN KEY (tenant_id, camera_calibration_id)
                REFERENCES camera_calibrations (tenant_id, id),
            CONSTRAINT automatic_measurements_one_calibration_check CHECK (
                CASE calibration_source
                    WHEN 'label_free' THEN automatic_laser_calibration_id IS NOT NULL
                                       AND laser_calibration_id IS NULL
                    ELSE laser_calibration_id IS NOT NULL
                     AND automatic_laser_calibration_id IS NULL
                END),
            CONSTRAINT automatic_measurements_length_or_refusal_check
                CHECK ((length_m IS NULL) = (refusal IS NOT NULL))
        )
        """)
    op.execute("""
        CREATE INDEX automatic_measurements_tenant_id_capture_id_seq_idx
            ON automatic_measurements (tenant_id, capture_id, seq)
        """)


def _views() -> None:
    for view, table, subject in (
        ("current_automatic_head_tail_predictions",
         "automatic_head_tail_predictions", "capture_id"),
        ("current_automatic_laser_calibrations",
         "automatic_laser_calibrations", "dive_id"),
        ("current_automatic_species_predictions",
         "automatic_species_predictions", "capture_id"),
    ):  # fmt: skip
        op.execute(f"""
            CREATE VIEW {view} WITH (security_invoker = true) AS
                SELECT DISTINCT ON (tenant_id, {subject}) *
                FROM {table}
                ORDER BY tenant_id, {subject}, seq DESC
            """)

    # The calibration a dive's automatic lengths use now: its own accepted
    # label-free fit, else its calibration link's (production's pairing of a
    # fish dive with its calibration session), else its effective stored
    # calibration where the kernel can project it (0026's geometry).
    op.execute("""
        CREATE VIEW automatic_measurement_calibrations
            WITH (security_invoker = true) AS
            SELECT d.tenant_id,
                   d.id AS dive_id,
                   CASE WHEN lf.id IS NOT NULL THEN 'label_free' ELSE 'stored' END
                       AS calibration_source,
                   lf.id AS automatic_laser_calibration_id,
                   CASE WHEN lf.id IS NULL THEN g.laser_calibration_id END
                       AS laser_calibration_id,
                   coalesce(lf.laser_position, g.laser_position) AS laser_position,
                   coalesce(lf.laser_axis, g.laser_axis) AS laser_axis,
                   cc.id AS camera_calibration_id,
                   cc.camera_matrix
            FROM dives d
            JOIN current_camera_calibrations cc
              ON cc.tenant_id = d.tenant_id AND cc.device_id = d.device_id
             AND cc.camera_model = 'pinhole'
            LEFT JOIN LATERAL (
                SELECT a.id, a.laser_position, a.laser_axis
                FROM current_automatic_laser_calibrations a
                WHERE a.tenant_id = d.tenant_id AND a.outcome = 'accepted'
                  AND a.dive_id IN (d.id, d.calibration_source_dive_id)
                ORDER BY a.dive_id = d.id DESC
                LIMIT 1
            ) lf ON true
            LEFT JOIN dive_laser_geometry g
              ON g.tenant_id = d.tenant_id AND g.dive_id = d.id
            WHERE lf.id IS NOT NULL OR g.laser_calibration_id IS NOT NULL
        """)

    op.execute("""
        CREATE VIEW current_automatic_measurements WITH (security_invoker = true) AS
            SELECT DISTINCT ON (m.tenant_id, m.capture_id) m.*
            FROM automatic_measurements m
            JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
            JOIN LATERAL (
                SELECT h.id FROM automatic_head_tail_predictions h
                WHERE h.tenant_id = m.tenant_id AND h.capture_id = m.capture_id
                ORDER BY h.seq DESC LIMIT 1
            ) ht ON ht.id = m.automatic_head_tail_prediction_id
            JOIN automatic_measurement_calibrations mc
              ON mc.tenant_id = c.tenant_id AND mc.dive_id = c.dive_id
             AND mc.calibration_source = m.calibration_source
             AND mc.automatic_laser_calibration_id
                 IS NOT DISTINCT FROM m.automatic_laser_calibration_id
             AND mc.laser_calibration_id IS NOT DISTINCT FROM m.laser_calibration_id
            ORDER BY m.tenant_id, m.capture_id, m.seq DESC
        """)

    op.execute("""
        CREATE VIEW automatic_results_export WITH (security_invoker = true) AS
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


def upgrade() -> None:
    app_role = _app_role()
    _tables()
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = {ACTIVE_TENANT})
                WITH CHECK (tenant_id = {ACTIVE_TENANT})
            """)
        # Append-only: no UPDATE, no DELETE.
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {app_role}")
        # The research role reads the lab's rows (0032's binding).
        op.execute(
            f"CREATE POLICY {RESEARCH_LAB_READ} ON public.{table} FOR SELECT "
            f"TO {RESEARCH_ROLE} USING ({_LAB})"
        )
        op.execute(
            f"CREATE POLICY {RESEARCH_LAB_BINDING} ON public.{table} AS RESTRICTIVE "
            f"FOR SELECT TO {RESEARCH_ROLE} USING ({_LAB})"
        )
    _views()
    op.execute(f"GRANT SELECT ON {', '.join(VIEWS)} TO {app_role}")
    # The export view and everything beneath it that 0032 did not grant
    # (security-invoker views check the caller's rights all the way down).
    op.execute(
        f"GRANT SELECT ON {', '.join((*TABLES, *VIEWS, *RESEARCH_BENEATH))} "
        f"TO {RESEARCH_ROLE}"
    )


#: Views beneath `automatic_measurement_calibrations` the research role could
#: not read before: the stored-calibration fallback's.
RESEARCH_BENEATH = (
    "dive_laser_geometry",
    "effective_laser_calibrations",
    "current_laser_calibrations",
)


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ON {', '.join(RESEARCH_BENEATH)} FROM {RESEARCH_ROLE}")
    for view in reversed(VIEWS):
        op.execute(f"DROP VIEW {view}")
    for table in reversed(TABLES):
        op.execute(f"DROP POLICY {RESEARCH_LAB_BINDING} ON public.{table}")
        op.execute(f"DROP POLICY {RESEARCH_LAB_READ} ON public.{table}")
        op.execute(f"DROP TABLE {table}")
