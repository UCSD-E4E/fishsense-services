"""Personal API tokens, for scripts and agents (the owner, 2026-10-23).

A signed-in user makes a token that acts as them, the way e4e-reference-manager's
``refman_`` tokens do, so an agent can reach the API (and its MCP endpoint)
without an Authentik session. Two kinds, chosen when it is made:

* ``read``, the default: reads what its owner can read, and nothing else;
* ``edit``, an opt-in: writes what its owner's role allows, through the REST
  API only (the MCP endpoint takes read tokens alone).

``api_tokens`` keeps only a SHA-256 of each token, and a token's ``scope`` is
fixed once made (a trigger refuses the change): a read token already sitting in
some agent's config can never quietly gain write access. Every token expires.

The app role has no grant on the table and RLS is forced with no policy, so it
opens nothing: four ``SECURITY DEFINER`` functions (0056's pattern) act for the
caller named by ``app.user_sub``, and one resolves a token by its hash:

* ``api_token_create(name, scope, hash, prefix, days)``: the caller's new token,
  or no row once they hold ``MAX_LIVE`` live ones;
* ``api_tokens_mine()``: the caller's tokens, newest first;
* ``api_token_revoke(id)``: revokes one of the caller's; false if not theirs;
* ``api_token_principal(hash)``: the live token's owner and scope, recording
  its use at most once a minute.

Revision ID: 0066
Revises: 0065
"""

from alembic import context, op

revision = "0066"
down_revision = "0065"

#: Live (unrevoked, unexpired) tokens a user may hold at once.
MAX_LIVE = 25
CALLER = "NULLIF(current_setting('app.user_sub', true), '')"
_PINNED = "SECURITY DEFINER SET search_path = pg_catalog, public"
_VIEW = """id uuid, name text, scope text, prefix text, created_at timestamptz,
           expires_at timestamptz, last_used_at timestamptz, revoked_at timestamptz"""
_COLUMNS = """t.id, t.name, t.scope, t.prefix, t.created_at, t.expires_at,
              t.last_used_at, t.revoked_at"""
FUNCTIONS = (
    "api_token_create(text, text, text, text, integer)",
    "api_tokens_mine()",
    "api_token_revoke(uuid)",
    "api_token_principal(text)",
)


def _app_role() -> str:
    role = context.config.attributes["app_role"]
    return op.get_bind().dialect.identifier_preparer.quote(role)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE api_tokens (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            name text NOT NULL CHECK (btrim(name) <> '' AND length(name) <= 100),
            scope text NOT NULL DEFAULT 'read' CHECK (scope IN ('read', 'edit')),
            prefix text NOT NULL,
            token_hash text NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            last_used_at timestamptz,
            revoked_at timestamptz
        )
        """)
    op.execute("CREATE INDEX api_tokens_user_id_idx ON api_tokens (user_id)")
    op.execute("ALTER TABLE api_tokens ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_tokens FORCE ROW LEVEL SECURITY")

    op.execute("""
        CREATE FUNCTION api_tokens_scope_is_fixed() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            RAISE EXCEPTION 'api_tokens.scope is fixed when a token is made';
        END
        $$
        """)
    op.execute("""
        CREATE TRIGGER api_tokens_scope_is_fixed
        BEFORE UPDATE OF scope ON api_tokens
        FOR EACH ROW WHEN (OLD.scope IS DISTINCT FROM NEW.scope)
        EXECUTE FUNCTION api_tokens_scope_is_fixed()
        """)

    op.execute(f"""
        CREATE FUNCTION api_token_create(
            p_name text, p_scope text, p_hash text, p_prefix text, p_days integer
        ) RETURNS TABLE ({_VIEW})
        LANGUAGE plpgsql {_PINNED} AS $$
        DECLARE
            me uuid;
        BEGIN
            SELECT u.id INTO me FROM public.users u WHERE u.sub = {CALLER};
            IF me IS NULL OR p_days < 1 OR p_days > 365 THEN RETURN; END IF;
            IF (SELECT count(*) FROM public.api_tokens t
                WHERE t.user_id = me AND t.revoked_at IS NULL
                  AND t.expires_at > now()) >= {MAX_LIVE} THEN
                RETURN;
            END IF;
            RETURN QUERY
                INSERT INTO public.api_tokens AS t
                    (user_id, name, scope, prefix, token_hash, expires_at)
                VALUES (me, btrim(p_name), p_scope, p_prefix, p_hash,
                        now() + make_interval(days => p_days))
                RETURNING {_COLUMNS};
        END
        $$
        """)
    op.execute(f"""
        CREATE FUNCTION api_tokens_mine() RETURNS TABLE ({_VIEW})
        LANGUAGE sql STABLE {_PINNED} AS $$
            SELECT {_COLUMNS} FROM public.api_tokens t
            JOIN public.users u ON u.id = t.user_id
            WHERE u.sub = {CALLER}
            ORDER BY t.created_at DESC, t.id
        $$
        """)
    op.execute(f"""
        CREATE FUNCTION api_token_revoke(p_id uuid) RETURNS boolean
        LANGUAGE sql {_PINNED} AS $$
            WITH theirs AS (
                UPDATE public.api_tokens t
                SET revoked_at = coalesce(t.revoked_at, now())
                FROM public.users u
                WHERE t.id = p_id AND u.id = t.user_id AND u.sub = {CALLER}
                RETURNING t.id
            )
            SELECT EXISTS (SELECT 1 FROM theirs)
        $$
        """)
    op.execute(f"""
        CREATE FUNCTION api_token_principal(p_hash text)
        RETURNS TABLE (sub text, scope text)
        LANGUAGE plpgsql {_PINNED} AS $$
        DECLARE
            found record;
        BEGIN
            SELECT t.id, u.sub, t.scope, t.last_used_at INTO found
            FROM public.api_tokens t JOIN public.users u ON u.id = t.user_id
            WHERE t.token_hash = p_hash AND t.revoked_at IS NULL
              AND t.expires_at > now();
            IF found.id IS NULL THEN RETURN; END IF;
            IF found.last_used_at IS NULL
               OR found.last_used_at < now() - interval '1 minute' THEN
                UPDATE public.api_tokens SET last_used_at = now()
                WHERE id = found.id;
            END IF;
            RETURN QUERY SELECT found.sub, found.scope;
        END
        $$
        """)
    for fn in FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {_app_role()}")


def downgrade() -> None:
    for fn in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION {fn}")
    op.execute("DROP TABLE api_tokens")
    op.execute("DROP FUNCTION api_tokens_scope_is_fixed()")
