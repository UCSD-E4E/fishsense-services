/**
 * The web's own token for the v2 API, for callers with no user.
 *
 * New in v2: replaces fishsense-lite@77e8f8e5's lib/api-auth.ts (a Basic-auth
 * header for a shared service account). The public landing page asks the API
 * which Label Studio projects hold work, and a public page has no user token
 * to ask with, so it asks as the web's Authentik service account -- a member
 * of the tenant like any other principal (PLAN.md §9.11).
 *
 * Cached until shortly before it expires and deduplicated across concurrent
 * callers, the same way lib/label-studio.ts treats Label Studio's token.
 */
import { clientCredentialsToken } from "./oidc";

const EXPIRY_SKEW_SECONDS = 30;

let cached: { token: string; expiresAtMs: number } | null = null;
let inFlight: Promise<string> | null = null;

async function mint(): Promise<string> {
  const { accessToken, expiresAt } = await clientCredentialsToken();
  cached = { token: accessToken, expiresAtMs: (expiresAt - EXPIRY_SKEW_SECONDS) * 1000 };
  return accessToken;
}

export async function getServiceToken(forceRefresh = false): Promise<string> {
  if (!forceRefresh && cached && cached.expiresAtMs > Date.now()) {
    return cached.token;
  }
  // Shared even when forced: a burst of 401s must not become a burst of mints.
  if (inFlight) return inFlight;

  const pending = mint();
  inFlight = pending;
  try {
    return await pending;
  } finally {
    if (inFlight === pending) inFlight = null;
  }
}

/** Test seam: drops the cached token and any mint in flight. */
export function __resetServiceToken(): void {
  cached = null;
  inFlight = null;
}
