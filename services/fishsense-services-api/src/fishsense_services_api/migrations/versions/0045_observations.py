"""`observations`: every measured fish, Lite and Mobile alike (PLAN.md §4.9).

Lite and Mobile are the same thing: a device photographs a fish, and a length
comes from the image plus a depth source. Until now the two were read
separately -- Lite only through Superset's views, Mobile only through
``/mobile/captures`` -- and nothing showed them together. This view is the
one read model the API and the web display (Phase 1 of §9.26): one row per
**current** measurement (``current_measurements``, §9.13) on a canonical
capture, whichever device took the photo.

* ``method`` says how the depth was found: ``laser`` for Lite's server
  measurements (laser + calibration), ``lidar`` for a phone's own. Every
  server measurement today is the laser path and every device one LiDAR, so
  it is derived from ``source``; it becomes a column when a third path
  (label-free, a server LiDAR recompute) writes measurements (§4.9).
* The device is the capture's, else its dive's: every measured capture in
  production has its own (3,314 of 3,314, read 2026-10-18).
* A fish is a species *or* a fish model (0012). Most of production's are
  models -- the decoys, 3,111 of 3,314 -- so a model's name and current known
  length (``current_fish_model_references``) are columns too: the length a
  decoy's measurement is judged against.
* ``photo_key`` is the mobile capture's own image in its tenant's bucket.
  Lite frames have none yet: their processed JPEGs are in the shared labels
  bucket, which the API does not read, and Lite photos are to arrive the way
  Mobile's do, into the tenant's bucket (a later phase).
* The automatic results track keeps its own tables (0036) and is not here.

``security_invoker``, like every view (0006): read as the app role, RLS
scopes it to the active tenant.

Revision ID: 0045
Revises: 0044
"""

from alembic import context, op

revision = "0045"
down_revision = "0044"

VIEW_SQL = """
CREATE VIEW observations WITH (security_invoker = true) AS
SELECT
    m.tenant_id,
    m.capture_id,
    m.fish_id,
    m.id AS measurement_id,
    CASE m.source WHEN 'server' THEN 'laser' WHEN 'device' THEN 'lidar' END
        AS method,
    m.length_m,
    c.captured_at,
    c.client_capture_uuid,
    CASE WHEN m.source = 'device' THEN c.raw_object_key END AS photo_key,
    c.latitude,
    c.longitude,
    mc.place_name,
    dv.kind AS device_kind,
    dv.name AS device_name,
    d.id AS dive_id,
    d.number AS dive_number,
    d.name AS dive_name,
    s.scientific_name,
    s.common_name,
    fm.name AS fish_model,
    r.known_length_m
FROM current_measurements m
JOIN captures c ON c.tenant_id = m.tenant_id AND c.id = m.capture_id
JOIN fish f ON f.tenant_id = m.tenant_id AND f.id = m.fish_id
LEFT JOIN dives d ON d.tenant_id = c.tenant_id AND d.id = c.dive_id
LEFT JOIN devices dv
  ON dv.tenant_id = c.tenant_id AND dv.id = COALESCE(c.device_id, d.device_id)
LEFT JOIN mobile_captures mc
  ON mc.tenant_id = c.tenant_id AND mc.capture_id = c.id
LEFT JOIN species s ON s.id = f.species_id
LEFT JOIN fish_models fm ON fm.id = f.fish_model_id
LEFT JOIN current_fish_model_references r ON r.name = fm.name
WHERE c.is_canonical
"""


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    op.execute(VIEW_SQL)
    op.execute(f"GRANT SELECT ON observations TO {_app_role()}")


def downgrade() -> None:
    op.execute("DROP VIEW observations")
