# Handoff to krg-infra: FishSense Mobile sync

**From:** fishsense-services. **For:** whoever owns `terraform/authentik`, OpenBao and the e4e Garage.
**Why:** FishSense Mobile is starting to sync captures to fishsense-services. Two things that only krg-infra can create stand between the app and that sync:

1. a sign-in for the mobile app;
2. a Garage bucket and key of each tenant's own.

The services side is built: fishsense-services PR "feat: FishSense Mobile sync and display", design in PLAN.md §9.6, §9.11 and §9.19. Until the steps below are done, the API keeps accepting web tokens only and no tenant can sync. Nothing breaks, **except** for the ordering rule in step 0.

> **Status:** implemented in **KastnerRG/krg-infra#566**, with one change agreed: the stores secret is `tenants/fishsense/generated/object_stores`, under krg-deploy's `tenants/+/generated` glob, not `tenants/fishsense/object_stores`. fishsense-services follows that path.

## 0. Order: krg-infra #566 is applied before the services release converges

The slot's vault-agent renders `oidc/mobile` and `generated/object_stores` as **soft** renders. Soft mode tolerates a missing *field*, but not a missing *path*. If either path is absent when a fishsense-services release with mobile sync converges, the API's render never appears and the API won't start.

krg-infra#566 creates both paths with their real values, so **apply it first**. No hand-made `bao kv put` is needed, and none should be done.

## 1. An Authentik application for the mobile app

The app is changing to **require a sign-in** (fishsense-mobile handoff). It's a native iOS app, so it's a **public client with PKCE**, as PLAN.md §4.2 planned ("copy the `incus` provider", `applications_krg.tf`). It should be **its own application**.

Authentik's issuer is per application (`/application/o/<slug>/`). The API now accepts a second issuer, with that issuer's own audience and keys.

| Setting | Value | Why |
|---|---|---|
| application slug | `fishsense-mobile` | gives the issuer `https://auth.krg.ucsd.edu/application/o/fishsense-mobile/` |
| `client_type` | `public` | a phone can't keep a secret |
| `client_id` | `fishsense-mobile` | becomes the `aud` the API checks |
| `grant_types` | `["authorization_code", "refresh_token"]` | goauthentik 2026.x defaults this to empty on create |
| redirect URI | `com.aqua3d.fishsense:/oauthredirect`, strict | the iOS bundle id `com.aqua3d.fishsense` as a custom scheme; agree the exact string with the mobile team, since theirs must match exactly |
| `signing_key` | the default RS256 certificate | the API accepts RS256 only; without a key Authentik signs HS256 and serves an empty JWKS, as it did on `fishsense_oauth` (#491) |
| `sub_mode` | `hashed_user_id` | **must match `fishsense_oauth`**, so a person has the same `sub` on the web and on the phone |
| property mappings | the standard scopes, plus `fishsense_org` (`org` scope), plus `offline_access` | `org` makes a partner's account join its org's tenant (fishsense-services 0037), on the phone as on the web |
| `access_token_validity` | `minutes=60` | as the web |
| `refresh_token_validity` | `days=90` | divers capture offline for weeks (PLAN.md §9.6); the app syncs once it's back on Wi-Fi |
| `include_claims_in_id_token` | `true` (the default; don't override it) | Authentik builds the access token from the ID token's claims, so this is what puts `org` on the token the API sees |

**Access:** bind the same policies as `fishsense_oauth`, meaning the `FishSense` AD group **and** the collaborator policy. Add the new application to `local.fishsense_collab_targets` in `fishsense_collaborators.tf`, or partners can't sign in on the phone.

**Then** write the client to OpenBao, with tofu next to the web client's (`vault_secrets.tf`):

```hcl
resource "vault_kv_secret_v2" "fishsense_oidc_mobile" {
  mount = "secret"
  name  = "tenants/fishsense/oidc/mobile"
  data_json = jsonencode({
    client_id  = authentik_provider_oauth2.fishsense_mobile.client_id
    issuer_url = "${var.authentik_url}/application/o/fishsense-mobile/"
  })
}
```

There's no client secret, because it's a public client. The API reads `issuer_url` and `client_id`.

## 2. A Garage bucket and key per tenant

PLAN.md §9.11 says bucket-per-tenant was due before the first partner upload, and mobile sync is that upload. Garage grants keys per bucket, so a key granted on one bucket is the isolation. Every capture a tenant's members sync lands in that tenant's bucket, under `mobile/captures/<uuid>/`.

For each tenant:

- **bucket** `fishsense-tenant-<slug>`;
- **key** `fishsense-tenant-<slug>`, granted `read` and `write` on that bucket **only**;
- no lifecycle rule for now. Objects are append-only, the API never deletes, and the data is scientific record.

The tenants today:

| slug | for |
|---|---|
| `lab` | the E4E lab |
| `conservation-angler` | the first partner org (fishsense-services `docs/partner-tenants.md`) |

**Then** write all of them as **one** OpenBao secret, the JSON the API reads. One secret means the slot's agent needs no `list` permission:

```hcl
resource "vault_kv_secret_v2" "fishsense_object_stores" {
  mount = "secret"
  name  = "tenants/fishsense/generated/object_stores"   # as built in krg-infra#566
  data_json = jsonencode({
    stores_json = jsonencode({
      for slug, key in garage_key.fishsense_tenant : slug => {
        bucket            = "fishsense-tenant-${slug}"
        access_key_id     = key.access_key_id
        secret_access_key = key.secret_access_key
      }
    })
  })
}
```

(As built, #566 derives this from every `fishsense-tenant-<slug>` key in `spec/e4e-nas/garage.yml` and its credential at `e4e-nas/garage-keys/<key>`. The shape of `stores_json` is the contract.)

**Adding a tenant later** takes three steps:

1. add its `fishsense-tenant-<slug>` bucket and key to `spec/e4e-nas/garage.yml` (#566 derives the `stores_json` entry);
2. `fishsense-services-api add-tenant` on the slot;
3. restart `openbao-agent` and recreate the `api` container, which reads the file at start.

## 3. Facts the services side relies on, to confirm

1. **`https://s3.e4e.ucsd.edu` is reachable from the public internet.** Phones `PUT` their files there through presigned URLs, and browsers load images from it. The orchestrator already uses this endpoint, but only from the slot.
   - *Answered in #566:* **from the US only.** The NAS firewall rule `geoip-US-web` allows 443 from US addresses only, so a phone abroad can't upload until it's back in the US. Widening it is an owner decision, not made here.
2. **Garage enforces `x-amz-checksum-sha256` on a presigned PUT, and returns it on `HEAD` with `x-amz-checksum-mode: ENABLED`.** This is the upload integrity check (PLAN.md §9.19). It was verified on Garage **v2.1.0** in the services tests. Please confirm the e4e Garage's version, or run the check below against it.
   - *Answered in #566:* the e4e Garage runs **v2.3.0**. Run the acceptance check below after apply to confirm it on the real instance.
3. No CORS rule is needed. The app's PUTs are native, not a browser's, and `<img>` GETs need no CORS.

## Acceptance checks

**Garage**, with one tenant's key:

```bash
uv run --with boto3 python - <<'EOF'
import base64, hashlib, urllib.request, boto3
s3 = boto3.client("s3", endpoint_url="https://s3.e4e.ucsd.edu", region_name="garage",
                  aws_access_key_id="<key id>", aws_secret_access_key="<secret>")
body = b"fishsense"; sha = base64.b64encode(hashlib.sha256(body).digest()).decode()
url = s3.generate_presigned_url("put_object", ExpiresIn=600, Params={
    "Bucket": "fishsense-tenant-lab", "Key": "acceptance/x", "ChecksumSHA256": sha})
put = lambda data: urllib.request.urlopen(urllib.request.Request(
    url, data=data, method="PUT", headers={"x-amz-checksum-sha256": sha})).status
print("right bytes:", put(body))          # 200
try: put(body + b"!"); print("WRONG: accepted")
except urllib.error.HTTPError as e: print("wrong bytes:", e.code)   # 400
print(s3.head_object(Bucket="fishsense-tenant-lab", Key="acceptance/x",
                     ChecksumMode="ENABLED").get("ChecksumSHA256") == sha)  # True
EOF
```

**The key is scoped to its bucket.** The `lab` key must get 403 on `fishsense-tenant-conservation-angler`.

**Authentik.** Sign in through the new application with PKCE as a lab member and as a conservation-angler account. Each access token should carry:

- `iss` = `…/application/o/fishsense-mobile/`;
- `aud` = `fishsense-mobile`;
- the same `sub` the web gets for that person;
- for the partner, `org` = `conservation-angler`.

**End to end,** once fishsense-services is deployed: `GET https://api.fishsense.e4e.ucsd.edu/me/memberships` with a mobile token answers 200, and lists `conservation-angler` for the partner account.
