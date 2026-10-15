#!/usr/bin/env bash
# Create (or replace) the NRP processor's Secret, `fishsense-processor-secrets`,
# and make sure the model weights it names are in Garage. docs/cutover.md §1.4–1.5.
#
# Every credential is read from OpenBao: nothing is typed, nothing lands in argv,
# shell history or the repo. Idempotent: re-run it after a key rotation.
#
# Needs: kubectl on the NRP context (namespace admin), aws, sha256sum, uv in
# this repo (for fishsense-core's verified download, and Hugging Face's for
# BioCLIP: about 5.7 GB the first time), and bao -- here, or on
# another host reached by the ssh command given as the first argument:
#   nix develop                                   # kubectl
#   nix shell nixpkgs#awscli2 -c deploy/nrp/processor-secret.sh "ssh krg-admin@krg-deploy.ucsd.edu"
# Without an argument, bao runs locally.
set -euo pipefail

# The command that reaches bao: empty for local, else e.g. `ssh user@host`.
# Word-split on purpose, so it may carry ssh options.
read -r -a BAO_VIA <<<"${1:-}"

NS=${NS:-e4e-fishsense}
ENDPOINT=https://s3.e4e.ucsd.edu
KV=secret/tenants/fishsense
WEIGHTS_BUCKET=model-weights
SAM_KEY=sam3/3.1/sam3.1_multiplex.pt
RUN3_KEY=laser-detector/run3/run3_epoch_021.pt
RUN3_SHA256=bd3ab8f5e273   # prefix of fishsense-core 4.1.0's manifest entry
RUN3_SIZE=294473278
# The slate presence detector (2026-10-03_slate_detector, runs/final-q1). Its
# weights are uploaded by hand; this verifies the copy in Garage and pins it.
SLATE_KEY=slate-detector/q1/slate_efficientnet_b0.pt
SLATE_SHA256=b8d377ba22d155e7056a5e9ae747fdd0970c7c73dee981bbee17d95c8156cf78
SLATE_SIZE=16339455
# BioCLIP zero-shot species (the automatic-results track, on since 2026-10-15),
# where the processor fetches them (species_predict/weights.py). Pinned to the
# revisions weights.py's architectures were taken from; sha256 and size are
# Hugging Face's own for those files (its ETag is the file's sha256).
BIOCLIP_REPO=imageomics/bioclip-2.5-vith14
BIOCLIP_REV=191d741545e4c741cdef4b22c6eb69c945c1e592
BIOCLIP_KEY=bioclip/2.5-vith14/open_clip_model.safetensors
BIOCLIP_SHA256=ac2e37c2f89ef8e6b889176a9a3f418970ad9db15a218bd29e3321e95c46ae97
BIOCLIP_SIZE=3944517804
BIOCLIP_FALLBACK_REPO=imageomics/bioclip-2
BIOCLIP_FALLBACK_REV=2957b322090f9cb17ae72c71981c7218a28d81e0
BIOCLIP_FALLBACK_KEY=bioclip/2/open_clip_model.safetensors
BIOCLIP_FALLBACK_SHA256=b7b2bf6fbc95799e42630e394cf95803892ab447c1a8ab629dbc82fbeaf7dfef
BIOCLIP_FALLBACK_SIZE=1710517724

work=$(mktemp -d)
trap 'rm -rf "${work:?}"' EXIT
umask 077

# One field, printed by bao. Over ssh, the remote command is fixed text plus
# path and field names from this script -- never a secret on any argv.
kv() { "${BAO_VIA[@]}" bao kv get -field="$2" "$KV/$1" </dev/null; }

# Fail early (and before downloading gigabytes) if bao can't be reached.
kv model_weights access_key >/dev/null

# Garage over S3, path-style, as the model-weights identity (the processor's own).
cat > "$work/aws.cfg" <<EOF
[default]
region = garage
s3 =
    addressing_style = path
EOF
export AWS_CONFIG_FILE="$work/aws.cfg"
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
AWS_ACCESS_KEY_ID=$(kv model_weights access_key)
AWS_SECRET_ACCESS_KEY=$(kv model_weights secret_key)
s3() { aws --endpoint-url "$ENDPOINT" "$@"; }

echo "== laser-detector run3"
if s3 s3api head-object --bucket "$WEIGHTS_BUCKET" --key "$RUN3_KEY" >/dev/null 2>&1; then
  echo "already in $WEIGHTS_BUCKET/$RUN3_KEY"
else
  # fishsense-core downloads it from Hugging Face and checks it against its
  # pinned manifest. huggingface_hub is core's `laser-detector` extra, which
  # only the GPU image installs, so it's added for this one download.
  uv run --package fishsense-services-processor --with huggingface_hub \
    python -m fishsense_core.models prefetch --dest "$work/weights" --name laser-detector
  f="$work/weights/laser-detector/run3/run3_epoch_021.pt"
  [ "$(stat -c %s "$f")" = "$RUN3_SIZE" ] || { echo "run3: wrong size" >&2; exit 1; }
  sha256sum "$f" | grep -q "^$RUN3_SHA256" || { echo "run3: wrong sha256" >&2; exit 1; }
  s3 s3 cp "$f" "s3://$WEIGHTS_BUCKET/$RUN3_KEY"
  echo "uploaded $WEIGHTS_BUCKET/$RUN3_KEY"
fi

echo "== SAM 3.1 (measured from Garage, the copy production fetches)"
s3 s3 cp "s3://$WEIGHTS_BUCKET/$SAM_KEY" "$work/sam3.pt" --only-show-errors
SAM_SHA256=$(sha256sum "$work/sam3.pt" | cut -d' ' -f1)
SAM_SIZE=$(stat -c %s "$work/sam3.pt")
rm -f "$work/sam3.pt"
echo "sha256 $SAM_SHA256  size $SAM_SIZE"

echo "== slate detector (verified against its pin)"
s3 s3 cp "s3://$WEIGHTS_BUCKET/$SLATE_KEY" "$work/slate.pt" --only-show-errors
[ "$(stat -c %s "$work/slate.pt")" = "$SLATE_SIZE" ] || { echo "slate detector: wrong size" >&2; exit 1; }
[ "$(sha256sum "$work/slate.pt" | cut -d' ' -f1)" = "$SLATE_SHA256" ] || { echo "slate detector: wrong sha256" >&2; exit 1; }
rm -f "$work/slate.pt"
echo "ok"

# A pinned file in Garage: uploaded from Hugging Face (at its revision, checked
# against its pin) when absent; when present, its size is checked here and its
# sha256 by the processor on every fetch.
pinned() { # repo rev key sha256 size
  local repo=$1 rev=$2 key=$3 sha=$4 size=$5 got
  got=$(s3 s3api head-object --bucket "$WEIGHTS_BUCKET" --key "$key" \
    --query ContentLength --output text 2>/dev/null || true)
  if [ "$got" = "$size" ]; then
    echo "already in $WEIGHTS_BUCKET/$key"
    return
  fi
  uv run --no-project --with huggingface_hub python - "$repo" "$rev" "$work/hf" <<'PY'
import sys
from huggingface_hub import hf_hub_download
repo, rev, dest = sys.argv[1:]
hf_hub_download(repo, "open_clip_model.safetensors", revision=rev, local_dir=dest)
PY
  local f="$work/hf/open_clip_model.safetensors"
  [ "$(stat -c %s "$f")" = "$size" ] || { echo "$key: wrong size" >&2; exit 1; }
  [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$sha" ] || { echo "$key: wrong sha256" >&2; exit 1; }
  s3 s3 cp "$f" "s3://$WEIGHTS_BUCKET/$key" --only-show-errors
  rm -rf "${work:?}/hf"
  echo "uploaded $WEIGHTS_BUCKET/$key"
}

echo "== BioCLIP 2.5 (species) and BioCLIP 2 (its fallback)"
pinned "$BIOCLIP_REPO" "$BIOCLIP_REV" "$BIOCLIP_KEY" "$BIOCLIP_SHA256" "$BIOCLIP_SIZE"
pinned "$BIOCLIP_FALLBACK_REPO" "$BIOCLIP_FALLBACK_REV" "$BIOCLIP_FALLBACK_KEY" \
  "$BIOCLIP_FALLBACK_SHA256" "$BIOCLIP_FALLBACK_SIZE"

echo "== the Secret"
cat > "$work/processor.env" <<EOF
FISHSENSE_OBJECT_STORE_ENDPOINT_URL=$ENDPOINT
FISHSENSE_OBJECT_STORE_REGION=garage
FISHSENSE_OBJECT_STORE_BUCKET=labels-fishsense-lite
FISHSENSE_OBJECT_STORE_LEGACY_LABELS_PREFIX=fishsense-lite
FISHSENSE_OBJECT_STORE_ACCESS_KEY_ID=$(kv object_store access_key)
FISHSENSE_OBJECT_STORE_SECRET_ACCESS_KEY=$(kv object_store secret_key)
FISHSENSE_MODEL_WEIGHTS_ENDPOINT_URL=$ENDPOINT
FISHSENSE_MODEL_WEIGHTS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID
FISHSENSE_MODEL_WEIGHTS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY
FISHSENSE_SAM3_SHA256=$SAM_SHA256
FISHSENSE_SAM3_SIZE=$SAM_SIZE
FISHSENSE_SLATE_DETECTOR_SHA256=$SLATE_SHA256
FISHSENSE_SLATE_DETECTOR_SIZE=$SLATE_SIZE
FISHSENSE_BIOCLIP_SHA256=$BIOCLIP_SHA256
FISHSENSE_BIOCLIP_SIZE=$BIOCLIP_SIZE
FISHSENSE_BIOCLIP_FALLBACK_SHA256=$BIOCLIP_FALLBACK_SHA256
FISHSENSE_BIOCLIP_FALLBACK_SIZE=$BIOCLIP_FALLBACK_SIZE
EOF
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
# Server-side: a client-side `apply` keeps a full copy of the data in the
# last-applied-configuration annotation, readable by anyone who can `get` the
# Secret (2026-10-14). Any copy an earlier run left is removed.
kubectl -n "$NS" create secret generic fishsense-processor-secrets \
  --from-env-file="$work/processor.env" --dry-run=client -o yaml \
  | kubectl -n "$NS" apply --server-side --force-conflicts \
      --field-manager=processor-secret -f -
kubectl -n "$NS" annotate secret fishsense-processor-secrets \
  kubectl.kubernetes.io/last-applied-configuration- >/dev/null
echo "keys: $(kubectl -n "$NS" get secret fishsense-processor-secrets -o go-template='{{range $k, $v := .data}}{{$k}} {{end}}')"
