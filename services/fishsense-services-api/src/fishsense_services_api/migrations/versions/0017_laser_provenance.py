"""Laser provenance carried over from fishsense-lite #927/#932.

**laser_labels.superseded_reason**: why a label was superseded, as v1 records
it from alembic e5a9c3d71b24 on: `validator_3sigma`,
`validator_coarse_calibration`, `manual`, or `remediation`. NULL is unknown
(everything superseded before #932, and whatever manual SQL does).
`remediation` also appears on *live* rows: it means "last changed by the
reviewed remediation", which revives labels.

**dive_laser_lines.noise_estimator**: which estimator produced
`label_noise_mad`, whose scale changed. Before fishsense-core 4.1.0, v1 took
1.4826 * MAD over *absolute* residuals, about 0.59 sigma, so its "3 sigma"
outlier cut sat near 1.78 sigma and superseded good labels. From 4.1.0 it is
over *signed* residuals, about 1.0 sigma. v1 rewrites a dive's line on every
run, stamping `fitted_at`, and deployed 4.1.0 at 2026-09-26T22:19:45Z with no
validator running in between (NRP had deleted the workers on 09-21). So
`fitted_at` says which, for rows already here.

Revision ID: 0017
Revises: 0016
"""

from alembic import op

revision = "0017"
down_revision = "0016"

#: When v1's data-worker began fitting with fishsense-core 4.1.0.
SIGNED_MAD_SINCE = "2026-09-26T22:19:45Z"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE laser_labels ADD COLUMN superseded_reason text
            CONSTRAINT laser_labels_superseded_reason_check CHECK (
                superseded_reason IN ('validator_3sigma',
                                      'validator_coarse_calibration',
                                      'manual', 'remediation')
            )
        """)
    op.execute("ALTER TABLE dive_laser_lines ADD COLUMN noise_estimator text")
    op.execute(f"""
        UPDATE dive_laser_lines SET noise_estimator = CASE
            WHEN fitted_at >= '{SIGNED_MAD_SINCE}' THEN 'signed_residual_mad'
            ELSE 'absolute_residual_mad' END
        """)
    op.execute("""
        ALTER TABLE dive_laser_lines
            ALTER COLUMN noise_estimator SET NOT NULL,
            ADD CONSTRAINT dive_laser_lines_noise_estimator_check CHECK (
                noise_estimator IN ('absolute_residual_mad', 'signed_residual_mad')
            )
        """)


def downgrade() -> None:
    op.execute("ALTER TABLE dive_laser_lines DROP COLUMN noise_estimator")
    op.execute("ALTER TABLE laser_labels DROP COLUMN superseded_reason")
