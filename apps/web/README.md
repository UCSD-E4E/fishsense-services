# fishsense web portal (`apps/web`)

Next.js (App Router) + React + TypeScript + Tailwind, ported from
fishsense-lite@77e8f8e5 `apps/fishsense-lite-web` onto the v2 API (PLAN.md §6.1).
Two surfaces:

- **Public landing (`/`)**: the Label Studio projects that still hold labeling
  work (laser, head/tail, species, slate), then the Superset and Temporal links.
- **Portal (`/portal/*`)**, for the tenant's **admins**: laser and head/tail
  **triage** (accept or skip a model's prediction, straight into Label Studio),
  and **dive calibration links** (a slate-less dive borrows a sibling's
  calibration).

## What changed from v1

| | v1 | v2 |
|---|---|---|
| API | fishsense-api `/api/v1/...`, hand-typed | `/tenants/{slug}/...`, a client generated from the API's OpenAPI (`openapi-typescript` + `openapi-fetch`) |
| API auth | one Basic-auth service account | the signed-in user's Authentik access token; the landing page uses the web's Authentik service account (client credentials) |
| Portal gate | Authentik group in `PORTAL_ALLOWED_GROUPS` | the user's `admin` role in the tenant, as the API reports it; the API enforces it again on its writes |
| Dive ids | v1 integer ids | `number` (v1's id for a migrated dive) |
| Triage frames | `TRIAGE_IMAGE_HOSTS` only | also the object store's host, from `FISHSENSE_OBJECT_STORE_ENDPOINT_URL` |

The role is read from the API at sign-in and at every token refresh. A portal
page whose access token has expired bounces through `/api/session/refresh`
(a page can't write the session cookie; a route handler can), which renews it
and re-reads the role. Triage writes to Label Studio, as v1 did, so the web's
check is the only gate there.

## Layout

```
app/
  page.tsx                          public landing
  portal/page.tsx                   portal index and its "why not" dead end
  portal/guard.ts                   the gate every portal page shares
  portal/calibration/               dive calibration links (server actions)
  portal/triage/                    mobile triage (server actions)
  api/auth/[...nextauth]/route.ts   Auth.js
  api/session/refresh/route.ts      renew the access token, then go back
  api/triage/image/[taskId]/route.ts  a task's frame, proxied
auth.ts                             Auth.js config (function form, lazy env)
openapi.json                        the API's spec (pinned by the API's tests)
lib/api/schema.d.ts                 generated from it: npm run api:generate
lib/api/client.ts                   the typed client
lib/fishsense-api.ts, dives.ts      what the portal asks the API
lib/auth-callbacks.ts, authz.ts     tokens, role, and the gate
lib/label-studio*.ts, triage*.ts    Label Studio (unchanged from v1)
```

## The API client

`openapi.json` is the API's OpenAPI document. The API's unit tests fail when it
differs from the live spec, and `npm run api:check` (CI) fails when
`lib/api/schema.d.ts` differs from what `openapi-typescript` makes of it. After
changing an API route:

```
uv run python -m fishsense_services_api.openapi > apps/web/openapi.json
cd apps/web && npm run api:generate
```

## Configuration

Everything is read per request (the `env` proxy in `lib/env.ts` throws on first
use of a missing one), so `next build` needs none of it and the landing page
renders without `AUTH_*`. See [.env.example](.env.example). In brief:

- `FISHSENSE_API_URL`, `FISHSENSE_TENANT` (default `lab`);
- `AUTH_SECRET`, `AUTH_AUTHENTIK_ID` / `_SECRET` / `_ISSUER`, `AUTH_URL`:
  the web's confidential Authentik client, whose id must be one of the API's
  `FISHSENSE_OIDC_AUDIENCES`, granting `refresh_token` with the
  `offline_access`, `groups` and `org` scopes mapped;
- `FISHSENSE_API_SERVICE_USERNAME` / `_PASSWORD`: the web's Authentik service
  account (an app password) for the landing page, a member of the tenant;
- `LABEL_STUDIO_ENABLED`, `LABEL_STUDIO_URL`, `LABEL_STUDIO_API_KEY`,
  `LABELER_BASE`; `FISHSENSE_OBJECT_STORE_ENDPOINT_URL`, `TRIAGE_IMAGE_HOSTS`.

## Development

No Node on the host is needed for CI parity: `docker compose up --build web`
builds the image from the repo root (`apps/web/Dockerfile`). With Node 24:

```
cd apps/web
npm ci
npm run dev               # next dev
npm test                  # vitest: lib/** and app/** unit tests
npm run typecheck && npm run lint
npm run build
npm run test:integration  # against a running container (FISHSENSE_WEB_URL)
```

The integration tests mint session cookies with `AUTH_SECRET`, so the
container must run with the same one (compose's dev default, or CI's).

## Caveats kept from v1

- The landing page is public and must keep working without `AUTH_*`.
- `AUTH_URL` is required outside localhost.
- Server actions are public endpoints: each re-checks the session and role
  before any write.
- Skip writes nothing to Label Studio; a head/tail prediction missing Snout or
  Fork is refused, not offered as partial (see `lib/triage.ts`).
- The image proxy takes a task id, never a URI, and only fetches from allowed
  hosts.
