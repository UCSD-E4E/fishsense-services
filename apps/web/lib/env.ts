// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/env.ts.
//
// v2 changes: v1's Basic-auth service account (FISHSENSE_API_USERNAME /
// PASSWORD) is gone -- the v2 API takes bearer tokens only. The public landing
// page's calls are made as an Authentik service account
// (FISHSENSE_API_SERVICE_USERNAME / PASSWORD, an app password), and every
// API path names the tenant (`tenantSlug`).

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required env var: ${name}`);
  }
  return value;
}

const ENV_VARS = {
  fishsenseApiUrl: "FISHSENSE_API_URL",
  fishsenseApiServiceUsername: "FISHSENSE_API_SERVICE_USERNAME",
  fishsenseApiServicePassword: "FISHSENSE_API_SERVICE_PASSWORD",
  labelStudioUrl: "LABEL_STUDIO_URL",
  labelStudioApiKey: "LABEL_STUDIO_API_KEY",
  authSecret: "AUTH_SECRET",
  authAuthentikId: "AUTH_AUTHENTIK_ID",
  authAuthentikSecret: "AUTH_AUTHENTIK_SECRET",
  authAuthentikIssuer: "AUTH_AUTHENTIK_ISSUER",
} as const;

type EnvKey = keyof typeof ENV_VARS;
type Env = Record<EnvKey, string>;

export const env: Env = new Proxy({} as Env, {
  get(_target, prop) {
    if (typeof prop === "string" && prop in ENV_VARS) {
      return required(ENV_VARS[prop as EnvKey]);
    }
    return undefined;
  },
});

/** One path segment of lowercase letters, digits and dashes. */
const SLUG = /^[a-z0-9][a-z0-9-]*$/;

/**
 * The tenant every API call acts in (PLAN.md §9.10: the active tenant is named
 * in the URL). At cutover there is one tenant, the lab (§9.11), so that is the
 * default; FISHSENSE_TENANT overrides it (a staging tenant, say).
 *
 * Validated, because it goes into every API path: a value that could steer the
 * request elsewhere is a misconfiguration and fails loudly.
 */
export function tenantSlug(): string {
  const value = process.env.FISHSENSE_TENANT || "lab";
  if (!SLUG.test(value)) {
    throw new Error(`FISHSENSE_TENANT is not a tenant slug: ${JSON.stringify(value)}`);
  }
  return value;
}

const TRUTHY = new Set(["true", "1", "yes"]);

// Kill-switch for the Label Studio integration. **Defaults to off**; prod
// sets `LABEL_STUDIO_ENABLED=true`.
//
// What used to break (v1):
//   1. Auth — this app sent `Authorization: Token <key>` and every
//      `/api/projects/<id>` fetch 401'd. The hosted instance
//      (app.heartex.com) treats `LABEL_STUDIO_API_KEY` as a *refresh*
//      token: it must be exchanged at `/api/token/refresh` for a
//      short-lived access JWT sent as `Bearer`. `lib/label-studio.ts`
//      does that (and caches/dedupes the exchange).
//   2. Blast radius — one dead project id used to 500 the whole landing
//      page. `getProjects` uses `Promise.allSettled` and drops what it
//      can't resolve.
//
// Kept as a switch so the integration can still be cut fast if the hosted
// instance misbehaves. Disabled, `getActiveProjects` returns empty buckets,
// `buildSections` collapses the four labeling sections, and triage is empty.
//
// Compared explicitly against a truthy allowlist rather than
// `Boolean(process.env.X)` — the latter reads the literal string "false"
// as true.
export function labelStudioEnabled(): boolean {
  const value = process.env.LABEL_STUDIO_ENABLED;
  return value !== undefined && TRUTHY.has(value.toLowerCase());
}

export const __test = { required };
