"""Reviews of the automatic results, and reviewers' skills (the owner,
2026-10-21).

A member reviews what the automatic run made of a frame: they accept it, fix
it (head and tail snap to the mask, as the app does; the dot doesn't), or
reject it with a reason. A review is of one kind -- the laser dot, the head and
tail, or the species -- because a reviewer's skill is per kind: one can be an
expert at dots and still learning species.

* ``reviewer_skills``: a person's level per kind, Learning, Trusted or
  Expert, append-only (the current one is the latest). Earned from agreement
  with known answers, or set by an admin; either way recorded. No row means
  Learning.
* ``reviews``: every action, append-only, with the reviewer's level at the
  time. A Trusted or Expert review counts at once. A Learning one waits: it
  counts once a Trusted or Expert reviewer confirms it, or a second Learning
  reviewer agrees (``confirms_review_id`` names the review either one backs).
  The points are kept as saved, with the raw drop points of a fix, and the
  render they are on.
* A review of the dot or the head and tail that counts becomes a human label
  (``source = 'review'``, naming its review and its render). The capture's
  other live labels of that kind are superseded, never deleted, with
  ``superseded_reason = 'review'``, so the measurement uses the review's.
  A species review is recorded only, until frames are grouped into fish (the
  owner has another agent on grouping).

Revision ID: 0062
Revises: 0061
"""

from alembic import context, op

from fishsense_services_api.schema_audit import (
    RESEARCH_LAB_BINDING,
    RESEARCH_LAB_READ,
    RESEARCH_ROLE,
)

revision = "0062"
down_revision = "0061"

ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_LAB = "tenant_id = (SELECT public.research_tenant_id())"
KINDS = "'laser', 'head_tail', 'species'"
LEVELS = "'learning', 'trusted', 'expert'"
REASONS = "'no_fish', 'fish_unmeasurable', 'wrong_fish', 'bad_mask', 'dot_wrong'"
OLD_SOURCES = "'human', 'auto_accept', 'pre_annotation', 'import'"
OLD_LASER_REASONS = (
    "'validator_3sigma', 'validator_coarse_calibration', 'manual', 'remediation'"
)


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _tenant_scoped(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    # Append-only: no UPDATE, no DELETE.
    op.execute(f"GRANT SELECT, INSERT ON {table} TO {_app_role()}")
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
    op.execute(f"""
        CREATE TABLE reviewer_skills (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            user_id uuid NOT NULL REFERENCES users (id),
            kind text NOT NULL CHECK (kind IN ({KINDS})),
            level text NOT NULL CHECK (level IN ({LEVELS})),
            source text NOT NULL CHECK (source IN ('earned', 'admin')),
            set_by uuid REFERENCES users (id),
            note text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            CHECK ((source = 'admin') = (set_by IS NOT NULL))
        )
        """)
    op.execute(
        "CREATE INDEX reviewer_skills_user_idx ON reviewer_skills "
        "(tenant_id, user_id, kind, seq)"
    )
    _tenant_scoped("reviewer_skills")
    op.execute("""
        CREATE VIEW current_reviewer_skills WITH (security_invoker = true) AS
            SELECT DISTINCT ON (tenant_id, user_id, kind) *
            FROM reviewer_skills
            ORDER BY tenant_id, user_id, kind, seq DESC
        """)

    op.execute(f"""
        CREATE TABLE reviews (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            capture_id uuid NOT NULL,
            reviewer_id uuid NOT NULL REFERENCES users (id),
            kind text NOT NULL CHECK (kind IN ({KINDS})),
            action text NOT NULL
                CHECK (action IN ('accept', 'fix', 'reject', 'confirm')),
            --: The reviewer's level in this kind when they reviewed.
            level text NOT NULL CHECK (level IN ({LEVELS})),
            automatic_head_tail_prediction_id uuid,
            reason text CHECK (reason IN ({REASONS})),
            x double precision,
            y double precision,
            head_x double precision,
            head_y double precision,
            tail_x double precision,
            tail_y double precision,
            --: A fix's points where the reviewer dropped them, before the snap.
            raw jsonb,
            render_id uuid,
            scientific_name text,
            --: The review this one confirms (``confirm``) or agrees with.
            confirms_review_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, automatic_head_tail_prediction_id)
                REFERENCES automatic_head_tail_predictions (tenant_id, id),
            FOREIGN KEY (tenant_id, render_id) REFERENCES capture_renders (tenant_id, id),
            FOREIGN KEY (tenant_id, confirms_review_id) REFERENCES reviews (tenant_id, id),
            CONSTRAINT reviews_reject_has_reason_check
                CHECK ((action = 'reject') = (reason IS NOT NULL)),
            CONSTRAINT reviews_confirm_names_review_check
                CHECK (action <> 'confirm' OR confirms_review_id IS NOT NULL),
            CONSTRAINT reviews_points_check CHECK (
                action NOT IN ('accept', 'fix') OR CASE kind
                    WHEN 'laser' THEN x IS NOT NULL AND y IS NOT NULL
                                      AND render_id IS NOT NULL
                    WHEN 'head_tail' THEN head_x IS NOT NULL AND head_y IS NOT NULL
                                      AND tail_x IS NOT NULL AND tail_y IS NOT NULL
                                      AND render_id IS NOT NULL
                    ELSE scientific_name IS NOT NULL
                END
            )
        )
        """)
    op.execute("CREATE INDEX reviews_capture_idx ON reviews (tenant_id, capture_id, seq)")
    op.execute(
        "CREATE INDEX reviews_confirms_idx ON reviews (tenant_id, confirms_review_id)"
    )
    _tenant_scoped("reviews")

    for table in ("head_tail_labels", "laser_labels"):
        op.execute(f"""
            ALTER TABLE {table}
                DROP CONSTRAINT {table}_source_check,
                ADD CONSTRAINT {table}_source_check
                    CHECK (source IN ({OLD_SOURCES}, 'review')),
                ADD COLUMN review_id uuid,
                ADD COLUMN render_id uuid,
                ADD CONSTRAINT {table}_review_fk FOREIGN KEY (tenant_id, review_id)
                    REFERENCES reviews (tenant_id, id),
                ADD CONSTRAINT {table}_render_fk FOREIGN KEY (tenant_id, render_id)
                    REFERENCES capture_renders (tenant_id, id),
                ADD CONSTRAINT {table}_review_label_check CHECK (
                    (source = 'review')
                    = (review_id IS NOT NULL AND render_id IS NOT NULL))
            """)
    op.execute("""
        ALTER TABLE head_tail_labels ADD COLUMN superseded_reason text
            CONSTRAINT head_tail_labels_superseded_reason_check
            CHECK (superseded_reason IN ('review'))
        """)
    op.execute(f"""
        ALTER TABLE laser_labels
            DROP CONSTRAINT laser_labels_superseded_reason_check,
            ADD CONSTRAINT laser_labels_superseded_reason_check
                CHECK (superseded_reason IN ({OLD_LASER_REASONS}, 'review'))
        """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM reviews) OR EXISTS (SELECT 1 FROM reviewer_skills)
            THEN RAISE EXCEPTION 'reviews or skills exist; 0062 cannot be undone';
            END IF;
        END $$
        """)
    op.execute(f"""
        ALTER TABLE laser_labels
            DROP CONSTRAINT laser_labels_superseded_reason_check,
            ADD CONSTRAINT laser_labels_superseded_reason_check
                CHECK (superseded_reason IN ({OLD_LASER_REASONS}))
        """)
    op.execute("ALTER TABLE head_tail_labels DROP COLUMN superseded_reason")
    for table in ("head_tail_labels", "laser_labels"):
        op.execute(f"""
            ALTER TABLE {table}
                DROP CONSTRAINT {table}_review_label_check,
                DROP COLUMN review_id,
                DROP COLUMN render_id,
                DROP CONSTRAINT {table}_source_check,
                ADD CONSTRAINT {table}_source_check CHECK (source IN ({OLD_SOURCES}))
            """)
    for table in ("reviews", "reviewer_skills"):
        op.execute(f"DROP POLICY {RESEARCH_LAB_BINDING} ON public.{table}")
        op.execute(f"DROP POLICY {RESEARCH_LAB_READ} ON public.{table}")
    op.execute("DROP VIEW current_reviewer_skills")
    op.execute("DROP TABLE reviews")
    op.execute("DROP TABLE reviewer_skills")
