#!/usr/bin/env bash
# Put a worker machine's credentials in the operator's account, never printed.
#
#   deploy/workers/fetch_credentials.sh user@host [dir]   (dir: ~/.fishsense-worker)
#
# New in v2 (2026-10-19), for processor workers off NRP (deploy/workers). The
# two things an NRP processor pod mounts, streamed from the slot to `host` as a
# tar on tmpfs, never touching a disk here or a terminal anywhere:
#   certs/{client.pem,client.key,root-ca.pem}  the slot's Temporal leaf -- the
#       identity cert_sync mirrors to NRP (a 7-day cert: re-run this after
#       each rotation, then `docker compose restart`);
#   processor.env  the processor's Secret (Garage keys, weight pins), as the
#       NRP pods' envFrom gives it, KEY=VALUE one per line.
# On `host`: the directory 0700, the files 0600, owned by the operator. Only
# the number of processor keys is printed.
set -euo pipefail
target="${1:?usage: fetch_credentials.sh user@host [dir]}"
dir="${2:-.fishsense-worker}"

slot_script='set -euo pipefail
umask 077
d=$(mktemp -d /dev/shm/fsw.XXXXXX)
trap "rm -rf \"$d\"" EXIT
mkdir "$d/certs"
cp /run/tenant/temporal/tls.crt "$d/certs/client.pem"
cp /run/tenant/temporal/tls.key "$d/certs/client.key"
cp /run/tenant/temporal/ca.crt "$d/certs/root-ca.pem"
KUBECONFIG=/run/tenant/nrp/kubeconfig nix shell nixpkgs#kubectl -c kubectl \
  -n e4e-fishsense get secret fishsense-processor-secrets \
  -o go-template="{{range \$k, \$v := .data}}{{\$k}}={{\$v | base64decode}}{{\"\\n\"}}{{end}}" \
  > "$d/processor.env"
# One line per key: a value with a newline cannot be an env_file line.
keys=$(KUBECONFIG=/run/tenant/nrp/kubeconfig nix shell nixpkgs#kubectl -c kubectl \
  -n e4e-fishsense get secret fishsense-processor-secrets \
  -o go-template="{{len .data}}")
[ "$(grep -c "=" "$d/processor.env")" = "$keys" ] || { echo "a processor value spans lines" >&2; exit 1; }
echo "processor keys: $keys" >&2
tar -C "$d" -cf - certs processor.env'

ssh krg-admin@krg-nat.ucsd.edu \
  "incus exec fishsense --project fishsense -- bash -lc $(printf %q "$slot_script")" \
  | ssh "$target" "umask 077; mkdir -p \"\$HOME/$dir\" && chmod 700 \"\$HOME/$dir\" \
      && tar -x -C \"\$HOME/$dir\" && chmod 600 \"\$HOME/$dir/processor.env\" \"\$HOME/$dir\"/certs/* \
      && echo \"credentials in \$HOME/$dir on \$(hostname)\""
