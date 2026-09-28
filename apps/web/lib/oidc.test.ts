// New in v2 (no v1 counterpart): v1 never used its Authentik tokens after
// sign-in, because its API took a Basic-auth password. v2's API takes bearer
// tokens, so the web refreshes the user's and mints its own service token.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  OidcError,
  __resetDiscovery,
  clientCredentialsToken,
  refreshTokens,
} from "./oidc";

const ISSUER = "https://auth.test/application/o/fishsense-web";
const TOKEN_URL = "https://auth.test/application/o/token/";

beforeEach(() => {
  vi.stubEnv("AUTH_AUTHENTIK_ISSUER", ISSUER);
  vi.stubEnv("AUTH_AUTHENTIK_ID", "web-client");
  vi.stubEnv("AUTH_AUTHENTIK_SECRET", "web-secret");
  vi.stubEnv("FISHSENSE_API_SERVICE_USERNAME", "svc-fishsense-web");
  vi.stubEnv("FISHSENSE_API_SERVICE_PASSWORD", "app-password");
  __resetDiscovery();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-09-27T00:00:00Z"));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

const NOW_S = Date.parse("2026-09-27T00:00:00Z") / 1000;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

type FetchSig = (input: string, init?: RequestInit) => Promise<Response>;

function idp(token: (form: URLSearchParams) => Response) {
  const fetchMock = vi.fn<FetchSig>(async (url, init) => {
    if (url === `${ISSUER}/.well-known/openid-configuration`) {
      return json({ issuer: `${ISSUER}/`, token_endpoint: TOKEN_URL });
    }
    if (url === TOKEN_URL) {
      return token(new URLSearchParams(String(init?.body)));
    }
    throw new Error(`unexpected ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("refreshTokens", () => {
  it("exchanges the refresh token at the discovered token endpoint", async () => {
    const fetchMock = idp(() =>
      json({ access_token: "new-at", expires_in: 300, refresh_token: "new-rt" }),
    );

    const tokens = await refreshTokens("old-rt");

    expect(tokens).toEqual({
      accessToken: "new-at",
      expiresAt: NOW_S + 300,
      refreshToken: "new-rt",
    });
    const [, init] = fetchMock.mock.calls.find(([u]) => u === TOKEN_URL)!;
    expect(init?.method).toBe("POST");
    const form = new URLSearchParams(String(init?.body));
    expect(Object.fromEntries(form)).toEqual({
      grant_type: "refresh_token",
      refresh_token: "old-rt",
      client_id: "web-client",
      client_secret: "web-secret",
    });
  });

  it("keeps the old refresh token when the IdP does not rotate it", async () => {
    idp(() => json({ access_token: "new-at", expires_in: 60 }));

    expect((await refreshTokens("old-rt")).refreshToken).toBe("old-rt");
  });

  it("discovers the token endpoint once", async () => {
    const fetchMock = idp(() => json({ access_token: "a", expires_in: 60 }));

    await refreshTokens("rt");
    await refreshTokens("rt");

    const discoveries = fetchMock.mock.calls.filter(([u]) =>
      u.endsWith("/.well-known/openid-configuration"),
    );
    expect(discoveries).toHaveLength(1);
  });

  it("throws with the status when the IdP refuses", async () => {
    idp(() => json({ error: "invalid_grant" }, 400));

    const error = await refreshTokens("revoked").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(OidcError);
    expect((error as OidcError).status).toBe(400);
    expect(String(error)).toMatch(/invalid_grant/);
  });

  it("throws when the answer has no access token", async () => {
    idp(() => json({ token_type: "bearer" }));

    await expect(refreshTokens("rt")).rejects.toThrow(/no access token/);
  });
});

describe("clientCredentialsToken", () => {
  it("mints a token for the web's Authentik service account", async () => {
    // Authentik's machine-to-machine flow: client_credentials with the service
    // account's username and an app password. The token's audience is the
    // web's client id, which the API already accepts.
    const fetchMock = idp(() => json({ access_token: "svc-at", expires_in: 600 }));

    expect(await clientCredentialsToken()).toEqual({
      accessToken: "svc-at",
      expiresAt: NOW_S + 600,
    });
    const [, init] = fetchMock.mock.calls.find(([u]) => u === TOKEN_URL)!;
    expect(Object.fromEntries(new URLSearchParams(String(init?.body)))).toEqual({
      grant_type: "client_credentials",
      client_id: "web-client",
      client_secret: "web-secret",
      username: "svc-fishsense-web",
      password: "app-password",
      scope: "openid",
    });
  });

  it("throws when the IdP refuses", async () => {
    idp(() => json({ error: "invalid_grant" }, 400));

    await expect(clientCredentialsToken()).rejects.toThrow(OidcError);
  });
});
