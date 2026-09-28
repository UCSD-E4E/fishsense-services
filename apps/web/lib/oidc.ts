/**
 * Talking to Authentik's token endpoint.
 *
 * New in v2 (fishsense-lite@77e8f8e5's web kept the user's access token but
 * never used it; its API took a Basic-auth password). The v2 API validates
 * bearer tokens itself (PLAN.md §9.10), so the web needs two things here:
 *
 *  * `refreshTokens` -- a fresh access token for a signed-in user, from their
 *    refresh token (Authentik's web provider grants `refresh_token`, §4.2);
 *  * `clientCredentialsToken` -- a token for the web's own Authentik service
 *    account, for the public landing page, which has no user.
 *
 * The web is a confidential client, so both authenticate with its secret
 * (`client_secret_post`). The endpoint is discovered from the issuer.
 */
import { env } from "./env";

export class OidcError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "OidcError";
    this.status = status;
  }
}

export type RefreshedTokens = {
  accessToken: string;
  /** Seconds since the epoch. */
  expiresAt: number;
  refreshToken: string;
};

export type ServiceToken = { accessToken: string; expiresAt: number };

let discovered: Promise<string> | null = null;

/** The issuer's token endpoint, from its discovery document (fetched once). */
async function tokenEndpoint(): Promise<string> {
  if (!discovered) {
    const url = `${env.authAuthentikIssuer.replace(/\/+$/, "")}/.well-known/openid-configuration`;
    discovered = (async () => {
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) {
        throw new OidcError(
          `OIDC discovery failed: ${response.status} ${response.statusText}`,
          response.status,
        );
      }
      const body = (await response.json()) as { token_endpoint?: unknown };
      if (typeof body.token_endpoint !== "string") {
        throw new OidcError("OIDC discovery returned no token_endpoint", 502);
      }
      return body.token_endpoint;
    })();
    // A failed discovery is retried on the next call, not remembered.
    discovered.catch(() => {
      discovered = null;
    });
  }
  return discovered;
}

/** Test seam: forget the discovered endpoint. */
export function __resetDiscovery(): void {
  discovered = null;
}

type TokenResponse = {
  access_token?: unknown;
  expires_in?: unknown;
  refresh_token?: unknown;
};

async function requestToken(
  what: string,
  form: Record<string, string>,
): Promise<{ accessToken: string; expiresAt: number; refreshToken?: string }> {
  const response = await fetch(await tokenEndpoint(), {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      ...form,
      client_id: env.authAuthentikId,
      client_secret: env.authAuthentikSecret,
    }).toString(),
    cache: "no-store",
  });
  const body = (await response.json().catch(() => ({}))) as TokenResponse & {
    error?: unknown;
  };
  if (!response.ok) {
    const reason = typeof body.error === "string" ? ` (${body.error})` : "";
    throw new OidcError(
      `${what} failed: ${response.status} ${response.statusText}${reason}`,
      response.status,
    );
  }
  if (typeof body.access_token !== "string" || !body.access_token) {
    throw new OidcError(`${what} returned no access token`, 502);
  }
  const lifetime = typeof body.expires_in === "number" ? body.expires_in : 0;
  return {
    accessToken: body.access_token,
    expiresAt: Math.floor(Date.now() / 1000) + lifetime,
    refreshToken: typeof body.refresh_token === "string" ? body.refresh_token : undefined,
  };
}

/** A fresh access token for a signed-in user. Authentik may rotate the
 *  refresh token; when it does not, the old one stays good. */
export async function refreshTokens(refreshToken: string): Promise<RefreshedTokens> {
  const tokens = await requestToken("Token refresh", {
    grant_type: "refresh_token",
    refresh_token: refreshToken,
  });
  return {
    accessToken: tokens.accessToken,
    expiresAt: tokens.expiresAt,
    refreshToken: tokens.refreshToken ?? refreshToken,
  };
}

/**
 * A token for the web's Authentik service account.
 *
 * Authentik's machine-to-machine flow: `client_credentials` carrying the
 * service account's username and an app password. Its audience is the web's
 * client id, which the API already accepts; what it may read is whatever its
 * membership in the tenant allows, granted like anyone's.
 */
export async function clientCredentialsToken(): Promise<ServiceToken> {
  const { accessToken, expiresAt } = await requestToken("Service token", {
    grant_type: "client_credentials",
    username: env.fishsenseApiServiceUsername,
    password: env.fishsenseApiServicePassword,
    scope: "openid",
  });
  return { accessToken, expiresAt };
}
