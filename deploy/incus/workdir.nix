# Populate the composeStack working directory.
#
# Kept from fishsense-lite deploy/incus/workdir.nix. The krg composeStack runner
# (krg-infra services/compose-stack.nix) invokes our compose with
# `--project-directory /var/lib/krg/fishsense`, so docker resolves the compose
# file's RELATIVE bind/env_file paths (`./superset_volumes/docker/.env`,
# `./traefik-dynamic.yml`, `./pg_volumes/config`, …) against THAT dir — its doc
# says "relative paths in compose files resolve here" — NOT the Nix-store compose
# dir. That dir is otherwise empty, so `docker compose up` fails
# (`env file .../superset_volumes/docker/.env not found`).
#
# Fix: symlink the repo-committed READ-ONLY config into the working dir, pointing
# at the flake's store copy (the store path changes on every config edit, so
# `L+` refreshes the link each converge — repo-owns-deploy preserved).
#
# v2 has no read-write bind at all (v1's worker log dirs are gone: v2's
# processes log to stdout), so every entry is a link. deploy/tests checks that
# every `./x` the compose names is linked here: a forgotten one is SILENT —
# docker creates a missing bind source as an empty directory, and the container
# comes up with nothing in it (v1's nrp_cert_sync lesson).
#
# Paths (`./x`) are relative to this file (deploy/incus/), so each resolves to
# that subtree's Nix store path.
{
  systemd.tmpfiles.rules = [
    # read-only config (mounted :ro in compose)
    "L+ /var/lib/krg/fishsense/traefik-dynamic.yml    - - - - ${./traefik-dynamic.yml}"
    "L+ /var/lib/krg/fishsense/superset_volumes       - - - - ${./superset_volumes}"
    "L+ /var/lib/krg/fishsense/pg_volumes             - - - - ${./pg_volumes}"
    # db-bootstrap's script, mounted :ro at /bootstrap.
    "L+ /var/lib/krg/fishsense/db_bootstrap           - - - - ${./db_bootstrap}"
    # compose reads `.env` from the project directory for COMPOSE_PROFILES: the
    # one switch that turns Superset on (compose.env).
    "L+ /var/lib/krg/fishsense/.env                   - - - - ${./compose.env}"
    # v1's entries (fishsense_api_volumes, nrp_cert_sync, worker_volumes with
    # its logs) are left where they are: nothing of ours reads them, v1's logs
    # are evidence during the rollback window, and a rollback relinks them.
  ];
}
