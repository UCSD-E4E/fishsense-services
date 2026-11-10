"""The ``fish_model`` refusal: a pool trial's fish model withdrawn (the owner,
2026-11-10).

The lab's automatic map showed 11 fish in Southern California: automatic
lengths on unlabeled frames of mission 509 (Nathan's Pool, 2023-08-18), a pool
trial whose 162 labeled fish are all fish models. They are decoys, not wild
fish. Automatic results are append-only, so the CLI's
``withdraw-fish-model-lengths`` (a dry run first) appends a ``fish_model``
refusal after each current automatic length on a frame nobody measured in the
missions it is given; the length stays in the history.

* ``automatic_measurements.refusal`` may be ``fish_model``.

The downgrade restores 0088's refusals; it fails while a ``fish_model``
refusal exists, rather than delete history. Pinned by
tests/test_migration_0089.py.

Revision ID: 0089
Revises: 0088
"""

from alembic import op

revision = "0089"
down_revision = "0088"

#: The automatic refusals as 0074 left them (0088 did not change them).
_REFUSALS = ("non_positive_depth", "non_finite_length", "zero_length", "zoomed",
             "low_parallax", "non_finite_depth", "beam_moved",
             "keypoints_disagree", "keypoints_refused")  # fmt: skip


def _refusal_check(reasons) -> None:
    listed = ", ".join(f"'{r}'" for r in reasons)
    op.execute(f"""
        ALTER TABLE automatic_measurements
            DROP CONSTRAINT automatic_measurements_refusal_check,
            ADD CONSTRAINT automatic_measurements_refusal_check
                CHECK (refusal IN ({listed}))
        """)


def upgrade() -> None:
    _refusal_check(_REFUSALS + ("fish_model",))


def downgrade() -> None:
    _refusal_check(_REFUSALS)
