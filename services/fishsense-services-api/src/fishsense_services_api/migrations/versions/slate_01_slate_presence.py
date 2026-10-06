"""Slate presence predictions: append-only, with provenance; and which slate
labels the detector queued.

New in v2 (v1's slate predictor estimated *pose*; it was retired on
2026-08-03, and its `slate_predictions` stay as they migrated). The model is
2026-10-03_slate_detector@95a77d95's presence classifier; see
`fishsense_services_contracts.slate_presence`.

`slate_presence_predictions` follows 0034's prediction table: appended, never
updated or deleted (SELECT and INSERT only), tenant-scoped under forced RLS,
ordered by `seq`, with `current_slate_presence` the latest per capture (led
by the tenant, as 0028's `current_measurements`, so a lookup reaches the
index). Every canonical frame is scored
once per model version, and a later version adds rows beside the earlier
ones. Each row is publication-grade (owner's decision, 2026-10-05): the
capture, `status` (`predicted`, or `decode_failed` with no probability:
recorded so the cohort, which selects on a row's absence, moves on),
`probability` (P(slate), never just a boolean), `model_name` and
`model_version` (`SLATE_DETECTOR_VERSION`), `weights_sha256` (the verified
weights it ran), `core_version` and `processor_version` (the processor
image's release), the render -- `decode_config` (`production`), `rectified`,
`input_width` x `input_height` as columns, and all of it, the decode's every
field included, as `render` -- and `predicted_at` (plus `created_at`). No
`v1_id` and so no `number`: nothing migrates into it (0021, 0025, 0034).

`slate_presence_evaluation` joins every prediction row to its frame's human
answer by the source repo's manifest rules (see `_EVAL`), with dive, image and
camera as v1's numbers, for dive-grouped metrics. It is the paper's view: the
research role (0032) reads it, bound to the lab like every table beneath it;
the app role does not (0031's rule).

`slate_labels.slate_presence_prediction_id` is the provenance of a label row
the detector queued: the prediction that put its frame in the dive's slate
project. NULL is every other row (a frame a person marked `Slate, Laser on
slate`, and everything v1 wrote). Additive, nullable, same-tenant.

Revision ID: slate_01
Revises: 0034
"""

from alembic import context, op

# Frozen at migration time (see 0031/0032): the audit's names for the research
# role and its policies, and the operating point.
from fishsense_services_api.schema_audit import (
    RESEARCH_LAB_BINDING,
    RESEARCH_LAB_READ,
    RESEARCH_ROLE,
)
from fishsense_services_api.slate_presence_store import SLATE_PRESENCE_THRESHOLD

revision = "slate_01"
down_revision = "0034"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_LAB = "tenant_id = (SELECT public.research_tenant_id())"

EVALUATION = "slate_presence_evaluation"

#: Each prediction row beside its frame's human answer. The answers and the
#: label are 2026-10-03_slate_detector@95a77d95's manifest
#: (src/slate_detector/dataset.py `LABELLED_SQL` and `slate_label`): completed,
#: live species labels with a content, plus a completed, live slate label
#: (spelled as that repo spells it); slate when every answer starts `Slate`,
#: no slate when none does and every one starts `Fish` (`Fish Model` too),
#: `Calibration Targets` or `None`, ambiguous otherwise, NULL unanswered.
_EVAL = f"""
    WITH answers AS (
        SELECT s.tenant_id, s.capture_id, s.content_of_image AS answer,
               false AS from_detector
        FROM species_labels s
        WHERE s.completed AND NOT s.superseded
          AND coalesce(s.content_of_image, '') <> ''
        UNION ALL
        SELECT l.tenant_id, l.capture_id, 'Slate (diveslatelabel)',
               l.slate_presence_prediction_id IS NOT NULL
        FROM slate_labels l
        WHERE l.completed AND NOT l.superseded
    ),
    judged AS (
        SELECT tenant_id, capture_id,
               string_agg(DISTINCT answer, '|' ORDER BY answer) AS answers,
               bool_and(answer LIKE 'Slate%') AS all_slate,
               bool_or(answer LIKE 'Slate%') AS any_slate,
               bool_and(answer LIKE 'Slate%' OR answer LIKE 'Fish%'
                        OR answer LIKE 'Calibration Targets%'
                        OR answer LIKE 'None%') AS all_known,
               bool_or(from_detector) AS from_detector
        FROM answers
        GROUP BY tenant_id, capture_id
    )
    SELECT p.id AS prediction_id, p.seq, p.tenant_id, p.capture_id,
           c.number AS image_id, c.is_canonical,
           d.id AS dive_uuid, d.number AS dive_id, d.priority AS dive_priority,
           d.slate_template_id IS NOT NULL AS dive_has_slate_template,
           dev.number AS camera_id, dev.id AS device_id,
           p.status, p.probability,
           p.probability >= {float(SLATE_PRESENCE_THRESHOLD)!r} AS predicted_slate,
           p.model_name, p.model_version, p.weights_sha256, p.core_version,
           p.processor_version, p.decode_config, p.rectified, p.input_width,
           p.input_height, p.render, p.predicted_at, p.created_at,
           p.seq = max(p.seq) OVER (PARTITION BY p.tenant_id, p.capture_id)
               AS is_current,
           j.answers AS human_answers,
           CASE
               WHEN j.capture_id IS NULL THEN NULL
               WHEN j.all_slate THEN 'slate'
               WHEN NOT j.any_slate AND j.all_known THEN 'no_slate'
               ELSE 'ambiguous'
           END AS human_label,
           coalesce(j.from_detector, false) AS slate_label_from_detector
    FROM slate_presence_predictions p
    JOIN captures c ON c.tenant_id = p.tenant_id AND c.id = p.capture_id
    JOIN dives d ON d.tenant_id = c.tenant_id AND d.id = c.dive_id
    LEFT JOIN devices dev ON dev.tenant_id = d.tenant_id AND dev.id = d.device_id
    LEFT JOIN judged j ON j.tenant_id = p.tenant_id AND j.capture_id = p.capture_id
"""


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    app_role = _app_role()
    op.execute("""
        CREATE TABLE slate_presence_predictions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            capture_id uuid NOT NULL,
            status text NOT NULL
                CONSTRAINT slate_presence_predictions_status_check
                CHECK (status IN ('predicted', 'decode_failed')),
            probability double precision
                CONSTRAINT slate_presence_predictions_probability_check
                CHECK (probability BETWEEN 0 AND 1),
            model_name text NOT NULL,
            model_version integer NOT NULL,
            weights_sha256 text NOT NULL
                CONSTRAINT slate_presence_predictions_weights_sha256_check
                CHECK (weights_sha256 ~ '^[0-9a-f]{64}$'),
            core_version text,
            processor_version text,
            decode_config text NOT NULL,
            rectified boolean NOT NULL,
            input_width integer NOT NULL
                CONSTRAINT slate_presence_predictions_input_width_check
                CHECK (input_width > 0),
            input_height integer NOT NULL
                CONSTRAINT slate_presence_predictions_input_height_check
                CHECK (input_height > 0),
            render jsonb NOT NULL,
            predicted_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            CONSTRAINT slate_presence_predictions_scored_check CHECK (
                (status = 'predicted') = (probability IS NOT NULL)
            )
        )
        """)
    op.execute("""
        CREATE INDEX slate_presence_predictions_tenant_id_capture_id_seq_idx
            ON slate_presence_predictions (tenant_id, capture_id, seq)
        """)
    op.execute("ALTER TABLE slate_presence_predictions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE slate_presence_predictions FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON slate_presence_predictions
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    # Append-only: no UPDATE, no DELETE.
    op.execute(f"GRANT SELECT, INSERT ON slate_presence_predictions TO {app_role}")
    op.execute("""
        CREATE VIEW current_slate_presence WITH (security_invoker = true) AS
            SELECT DISTINCT ON (tenant_id, capture_id) *
            FROM slate_presence_predictions
            ORDER BY tenant_id, capture_id, seq DESC
        """)
    op.execute(f"GRANT SELECT ON current_slate_presence TO {app_role}")

    op.execute("""
        ALTER TABLE slate_labels
            ADD COLUMN slate_presence_prediction_id uuid,
            ADD FOREIGN KEY (tenant_id, slate_presence_prediction_id)
                REFERENCES slate_presence_predictions (tenant_id, id)
        """)

    op.execute(f"CREATE VIEW {EVALUATION} WITH (security_invoker = true) AS {_EVAL}")

    # The research role (0032): the lab's rows only, read-only. Everything
    # else beneath the view (captures, dives, devices, species and slate
    # labels) is already bound and granted there.
    op.execute(
        f"CREATE POLICY {RESEARCH_LAB_READ} ON public.slate_presence_predictions "
        f"FOR SELECT TO {RESEARCH_ROLE} USING ({_LAB})"
    )
    op.execute(
        f"CREATE POLICY {RESEARCH_LAB_BINDING} ON public.slate_presence_predictions "
        f"AS RESTRICTIVE FOR SELECT TO {RESEARCH_ROLE} USING ({_LAB})"
    )
    op.execute(
        "GRANT SELECT ON public.slate_presence_predictions, "
        f"public.current_slate_presence, public.{EVALUATION} TO {RESEARCH_ROLE}"
    )


def downgrade() -> None:
    op.execute(f"DROP VIEW {EVALUATION}")
    op.execute("ALTER TABLE slate_labels DROP COLUMN slate_presence_prediction_id")
    op.execute("DROP VIEW current_slate_presence")
    op.execute("DROP TABLE slate_presence_predictions")
