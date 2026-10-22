"""The shadow size-constancy laser calibration's results (2026-10-21).

docs/shadow-size-constancy.md: size constancy (SAM 3.1 slate masks, a port of
cscw's size_constancy_sam.py) runs beside production's laser calibration on
every mission with slate frames, as evidence for whether it can replace it.
**Its results feed nothing**: these tables are read by no view, function or
production query (pinned by tests), so measurement output is identical with
the stage on or off.

* ``shadow_laser_calibrations`` -- one row per mission x dot source
  (``labels``: production's own live-dot rule; ``detector``: the automatic
  track's dots, gated) x variant (``port``: cscw's script exactly; ``gated``:
  the committed gate and refusals) x run: the fit or its refusal, its
  difference from the mission's production calibration, and everything the
  backfill report's checks need.
* ``shadow_calibration_frames`` -- every slate frame each row considered, with
  its source, dot, SAM result, place on the line, range, and whether it was
  used or why not, so every check can be recomputed without re-running SAM.

Append-only, tenant RLS, research read on the lab's rows. ``current_shadow_
laser_calibrations`` picks the latest row per (mission, source, variant,
algorithm version).

Revision ID: 0061
Revises: 0060
"""

from alembic import context, op

from fishsense_services_api.schema_audit import (
    RESEARCH_LAB_BINDING,
    RESEARCH_LAB_READ,
    RESEARCH_ROLE,
)

revision = "0061"
down_revision = "0060"

CALIBRATIONS = "shadow_laser_calibrations"
FRAMES = "shadow_calibration_frames"
ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_LAB = "tenant_id = (SELECT public.research_tenant_id())"


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _append_only(table: str, app_role: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    op.execute(f"GRANT SELECT, INSERT ON {table} TO {app_role}")
    op.execute(
        f"CREATE POLICY {RESEARCH_LAB_READ} ON public.{table} FOR SELECT "
        f"TO {RESEARCH_ROLE} USING ({_LAB})"
    )
    op.execute(
        f"CREATE POLICY {RESEARCH_LAB_BINDING} ON public.{table} AS RESTRICTIVE "
        f"FOR SELECT TO {RESEARCH_ROLE} USING ({_LAB})"
    )
    op.execute(f"GRANT SELECT ON {table} TO {RESEARCH_ROLE}")


def upgrade() -> None:
    app_role = _app_role()
    op.execute(f"""
        CREATE TABLE {CALIBRATIONS} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            mission_id uuid NOT NULL,
            dot_source text NOT NULL CONSTRAINT {CALIBRATIONS}_dot_source_check
                CHECK (dot_source IN ('labels', 'detector')),
            variant text NOT NULL CONSTRAINT {CALIBRATIONS}_variant_check
                CHECK (variant IN ('port', 'gated')),
            outcome text NOT NULL CONSTRAINT {CALIBRATIONS}_outcome_check
                CHECK (outcome IN ('accepted', 'refused')),
            refusal_reason text,
            refusals text[] NOT NULL DEFAULT '{{}}',
            laser_position double precision[],
            laser_axis double precision[],
            o_mag_m double precision NOT NULL,
            vanishing_px double precision,
            line_direction double precision[],
            line_offset_px double precision,
            line_source text CONSTRAINT {CALIBRATIONS}_line_source_check
                CHECK (line_source IN ('mission_line', 'detector_line', 'dots')),
            camera_calibration_id uuid,
            reference_laser_calibration_id uuid,
            reference_borrowed boolean,
            angle_diff_deg double precision,
            vp_offset_px double precision,
            vp_offset_deg_fx double precision,
            vp_offset_deg_f2850 double precision,
            fit_residual double precision,
            se_px double precision,
            se_deg double precision,
            frames_used integer NOT NULL CHECK (frames_used >= 0),
            frames_rejected jsonb NOT NULL DEFAULT '{{}}',
            size_spread double precision,
            range_min_m double precision,
            range_max_m double precision,
            lever_m double precision,
            lever_m_production_offset double precision,
            size_cv_pct double precision,
            size_maxdev_pct double precision,
            sam_score_min double precision,
            sam_score_median double precision,
            no_mask_share double precision,
            sam_model text NOT NULL,
            sam_prompts text[] NOT NULL,
            sam_threshold double precision NOT NULL,
            render_long_side integer NOT NULL,
            gate jsonb NOT NULL DEFAULT '{{}}',
            algorithm_version text NOT NULL,
            code_version text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, mission_id) REFERENCES missions (tenant_id, id),
            FOREIGN KEY (tenant_id, reference_laser_calibration_id)
                REFERENCES laser_calibrations (tenant_id, id),
            CONSTRAINT {CALIBRATIONS}_accepted_check CHECK (
                outcome <> 'accepted' OR (
                    coalesce(cardinality(laser_position), 0) = 3 AND coalesce(cardinality(laser_axis), 0) = 3)),
            CONSTRAINT {CALIBRATIONS}_refused_check CHECK (
                (outcome = 'refused') = (refusal_reason IS NOT NULL))
        )
        """)
    op.execute(f"""
        CREATE INDEX {CALIBRATIONS}_tenant_id_mission_id_idx
            ON {CALIBRATIONS} (tenant_id, mission_id)
        """)
    _append_only(CALIBRATIONS, app_role)
    op.execute(f"""
        CREATE TABLE {FRAMES} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            shadow_laser_calibration_id uuid NOT NULL,
            capture_id uuid NOT NULL,
            frame_source text NOT NULL CONSTRAINT {FRAMES}_frame_source_check
                CHECK (frame_source IN ('label', 'detector', 'both')),
            slate_probability double precision,
            dot_x double precision,
            dot_y double precision,
            dot_label_id uuid,
            dot_label_source text,
            dot_prediction_id uuid,
            dot_confidence double precision,
            line_distance_px double precision,
            sam_prompt text,
            sam_score double precision,
            mask_area_px double precision,
            size double precision,
            t_px double precision,
            range_m double precision,
            used boolean NOT NULL,
            rejected_reason text,
            capture_number integer,
            captured_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, shadow_laser_calibration_id)
                REFERENCES {CALIBRATIONS} (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            CONSTRAINT {FRAMES}_used_check CHECK (used = (rejected_reason IS NULL))
        )
        """)
    op.execute(f"""
        CREATE INDEX {FRAMES}_tenant_id_calibration_idx
            ON {FRAMES} (tenant_id, shadow_laser_calibration_id)
        """)
    _append_only(FRAMES, app_role)
    op.execute(f"""
        CREATE VIEW current_{CALIBRATIONS} WITH (security_invoker = true) AS
            SELECT DISTINCT ON (tenant_id, mission_id, dot_source, variant,
                                algorithm_version) *
            FROM {CALIBRATIONS}
            ORDER BY tenant_id, mission_id, dot_source, variant, algorithm_version,
                     seq DESC
        """)
    op.execute(f"GRANT SELECT ON current_{CALIBRATIONS} TO {app_role}")
    op.execute(f"GRANT SELECT ON current_{CALIBRATIONS} TO {RESEARCH_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP VIEW current_{CALIBRATIONS}")
    for table in (FRAMES, CALIBRATIONS):
        op.execute(f"DROP POLICY {RESEARCH_LAB_BINDING} ON public.{table}")
        op.execute(f"DROP POLICY {RESEARCH_LAB_READ} ON public.{table}")
        op.execute(f"DROP TABLE {table}")
