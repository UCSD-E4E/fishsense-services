"""Automatic head/tail v5: SimCC keypoints beside the SAM rule (owner,
2026-10-24/25).

The SAM rule (the mask's extreme points) is broken on real fish; fishsense-
core's SimCC snout/fork model, run on the same SAM box, replaces it for every
frame but the fish models', which keep the SAM rule (their straight bodies
are what it was built on). A model frame is one in a ``known_length``
mission or whose human species label names a Fish Model: exactly the 29
model missions (2,717 frames). Where SimCC and the SAM rule disagree on the
length by more than 15% the frame is sent to review rather than measured:
3,981 of 36,196 non-model frames (11%) on the 2026-10-24 replay; SimCC
refused no box.

* ``automatic_keypoint_predictions``: append-only, tenant RLS, research read
  of the lab's rows (as 0036's tables). One row per automatic head/tail
  prediction it was made on: SimCC's head and tail (render px), its
  confidence and crop, both lengths in pixels, their disagreement, and a
  status -- ``predicted``, ``keypoints_disagree`` (over the threshold: to
  review), ``keypoints_refused`` (the model refused the box). The head/tail
  rows are untouched, so species, masks and reviews keep theirs.
  ``current_automatic_keypoint_predictions``: the latest per prediction.
* ``automatic_measurements.automatic_keypoint_prediction_id``: the keypoints
  a length was measured from (NULL: the SAM rule's, a model frame's). A
  length whose frame's keypoints changed is owed again.
* ``automatic_measurements.refusal`` may be ``keypoints_disagree`` or
  ``keypoints_refused``: a frame to review has no length, and its SAM-rule
  length stops being current.

The downgrade drops the table and column and restores 0073's refusals; it
fails while a keypoint refusal exists, rather than delete history.

Revision ID: 0074
Revises: 0073
"""

from alembic import context, op
from sqlalchemy import text

from fishsense_services_api.schema_audit import (
    RESEARCH_LAB_BINDING,
    RESEARCH_LAB_READ,
    RESEARCH_ROLE,
)

revision = "0074"
down_revision = "0073"

TABLE = "automatic_keypoint_predictions"
VIEW = f"current_{TABLE}"
ACTIVE_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_LAB = "tenant_id = (SELECT public.research_tenant_id())"
#: The automatic refusals as 0073 left them.
_REFUSALS = ("non_positive_depth", "non_finite_length", "zero_length", "zoomed",
             "low_parallax", "non_finite_depth", "beam_moved")  # fmt: skip
KEYPOINT_REFUSALS = ("keypoints_disagree", "keypoints_refused")


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def _refusal_check(reasons) -> None:
    listed = ", ".join(f"'{r}'" for r in reasons)
    op.execute(f"""
        ALTER TABLE automatic_measurements
            DROP CONSTRAINT automatic_measurements_refusal_check,
            ADD CONSTRAINT automatic_measurements_refusal_check
                CHECK (refusal IN ({listed}))
        """)


#: `current_automatic_measurements` lists its columns (0036's ``m.*``,
#: expanded when it was made): the new one is added at the end.
_LAST = "m.created_at\n   FROM automatic_measurements m"
_ADDED = ("m.created_at,\n    m.automatic_keypoint_prediction_id\n"
          "   FROM automatic_measurements m")  # fmt: skip


def _edit_view(view: str, old: str, new: str) -> None:
    held = (
        op.get_bind()
        .execute(
            text("""
                SELECT pg_get_viewdef(c.oid, true) AS definition, c.reloptions
                FROM pg_class c WHERE c.oid = CAST(:v AS regclass)
                """),
            {"v": view},
        )
        .one()
    )
    if held.definition.count(old) != 1:
        raise RuntimeError(f"{view}: expected one {old!r} in its definition")
    options = f" WITH ({', '.join(held.reloptions)})" if held.reloptions else ""
    body = held.definition.replace(old, new).rstrip(";")
    op.execute(f"CREATE OR REPLACE VIEW {view}{options} AS {body}")


def upgrade() -> None:
    app_role = _app_role()
    op.execute(f"""
        CREATE TABLE {TABLE} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
            seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            producer text NOT NULL DEFAULT 'automatic'
                CONSTRAINT {TABLE}_producer_check CHECK (producer = 'automatic'),
            capture_id uuid NOT NULL,
            automatic_head_tail_prediction_id uuid NOT NULL,
            status text NOT NULL CONSTRAINT {TABLE}_status_check
                CHECK (status IN ('predicted', 'keypoints_disagree',
                                  'keypoints_refused')),
            head_x double precision,
            head_y double precision,
            tail_x double precision,
            tail_y double precision,
            confidence double precision,
            crop integer[],
            sam_rule_length_px double precision,
            simcc_length_px double precision,
            disagreement double precision,
            predictor_version integer NOT NULL,
            checkpoint text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, id),
            FOREIGN KEY (tenant_id, capture_id) REFERENCES captures (tenant_id, id),
            FOREIGN KEY (tenant_id, automatic_head_tail_prediction_id)
                REFERENCES automatic_head_tail_predictions (tenant_id, id),
            CONSTRAINT {TABLE}_points_check CHECK (
                (status = 'keypoints_refused') = (head_x IS NULL)
                AND (head_x IS NULL) = (head_y IS NULL)
                AND (head_x IS NULL) = (tail_x IS NULL)
                AND (head_x IS NULL) = (tail_y IS NULL))
        )
        """)
    op.execute(
        f"CREATE INDEX {TABLE}_tenant_prediction_idx "
        f"ON {TABLE} (tenant_id, automatic_head_tail_prediction_id, seq)"
    )
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY tenant_isolation ON {TABLE}
            USING (tenant_id = {ACTIVE_TENANT})
            WITH CHECK (tenant_id = {ACTIVE_TENANT})
        """)
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO {app_role}")
    op.execute(
        f"CREATE POLICY {RESEARCH_LAB_READ} ON public.{TABLE} FOR SELECT "
        f"TO {RESEARCH_ROLE} USING ({_LAB})"
    )
    op.execute(
        f"CREATE POLICY {RESEARCH_LAB_BINDING} ON public.{TABLE} AS RESTRICTIVE "
        f"FOR SELECT TO {RESEARCH_ROLE} USING ({_LAB})"
    )
    op.execute(f"""
        CREATE VIEW {VIEW} WITH (security_invoker = true) AS
            SELECT DISTINCT ON (tenant_id, automatic_head_tail_prediction_id) *
            FROM {TABLE}
            ORDER BY tenant_id, automatic_head_tail_prediction_id, seq DESC
        """)
    op.execute(f"GRANT SELECT ON {VIEW} TO {app_role}")
    op.execute(f"GRANT SELECT ON {TABLE}, {VIEW} TO {RESEARCH_ROLE}")
    op.execute(f"""
        ALTER TABLE automatic_measurements
            ADD COLUMN automatic_keypoint_prediction_id uuid,
            ADD CONSTRAINT automatic_measurements_keypoints_fkey
                FOREIGN KEY (tenant_id, automatic_keypoint_prediction_id)
                REFERENCES {TABLE} (tenant_id, id)
        """)
    _edit_view("current_automatic_measurements", _LAST, _ADDED)
    _refusal_check(_REFUSALS + KEYPOINT_REFUSALS)


def _held(view: str):
    """A view's definition, options and SELECT grants, as Postgres holds them."""
    bind = op.get_bind()
    row = bind.execute(
        text("""
            SELECT pg_get_viewdef(c.oid, true) AS definition, c.reloptions,
                   ARRAY(SELECT DISTINCT a.grantee::regrole::text
                         FROM aclexplode(c.relacl) a
                         WHERE a.privilege_type = 'SELECT'
                           AND a.grantee <> c.relowner) AS readers
            FROM pg_class c WHERE c.oid = CAST(:v AS regclass)
            """),
        {"v": view},
    ).one()
    return row.definition, row.reloptions, row.readers


def _create(view: str, definition: str, options, readers) -> None:
    with_ = f" WITH ({', '.join(options)})" if options else ""
    op.execute(f"CREATE VIEW {view}{with_} AS {definition.rstrip(';')}")
    for reader in readers:
        op.execute(f"GRANT SELECT ON {view} TO {reader}")


def downgrade() -> None:
    _refusal_check(_REFUSALS)
    # A view's column cannot be dropped by CREATE OR REPLACE, so the view is
    # rebuilt without it -- and the export view over it with it, each from
    # the definition, options and readers Postgres holds.
    current = _held("current_automatic_measurements")
    export = _held("automatic_results_export")
    if current[0].count(_ADDED) != 1:
        raise RuntimeError("current_automatic_measurements: not 0074's definition")
    op.execute("DROP VIEW automatic_results_export")
    op.execute("DROP VIEW current_automatic_measurements")
    _create("current_automatic_measurements", current[0].replace(_ADDED, _LAST),
            *current[1:])  # fmt: skip
    _create("automatic_results_export", *export)
    op.execute(
        "ALTER TABLE automatic_measurements DROP COLUMN automatic_keypoint_prediction_id"
    )
    op.execute(f"DROP VIEW {VIEW}")
    op.execute(f"DROP POLICY {RESEARCH_LAB_BINDING} ON public.{TABLE}")
    op.execute(f"DROP POLICY {RESEARCH_LAB_READ} ON public.{TABLE}")
    op.execute(f"DROP TABLE {TABLE}")
