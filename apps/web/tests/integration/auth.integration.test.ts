// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/tests/integration/auth.integration.test.ts (unchanged).
import { describe, expect, it } from "vitest";

const WEB_URL = process.env.FISHSENSE_WEB_URL ?? "http://localhost:3000";

// Light-touch coverage of the next-auth handler routes that don't need
// a real OIDC IdP — pins that AUTH_* env vars are read at request time
// without throwing, the Authentik provider is wired in, and the Auth.js
// response cycle isn't being eaten by some downstream cascade (the
// failure mode that originally turned PR #189 into a hotfix). Full OAuth
// callback / session-creation coverage is gated on standing up a mock
// OIDC IdP — tracked in UCSD-E4E/fishsense-lite#190.

describe("next-auth handler routes (against the local stack)", () => {
  it("registers the Authentik provider at /api/auth/providers", async () => {
    const res = await fetch(`${WEB_URL}/api/auth/providers`, {
      cache: "no-store",
    });
    expect(res.status).toBe(200);
    const body = (await res.json()) as Record<
      string,
      { id?: string; type?: string }
    >;
    expect(body).toHaveProperty("authentik");
    expect(body.authentik).toMatchObject({
      id: "authentik",
      type: "oidc",
    });
  });

  it("issues a csrf token cookie at /api/auth/csrf", async () => {
    // The csrf endpoint is the smallest Auth.js route that writes a
    // Set-Cookie. The original /portal incident's cascading
    // not-found-page error was eating Set-Cookie headers, which broke
    // the OAuth PKCE flow downstream — a regression of that shape would
    // surface here as a missing or empty Set-Cookie.
    const res = await fetch(`${WEB_URL}/api/auth/csrf`, { cache: "no-store" });
    expect(res.status).toBe(200);
    const setCookie = res.headers.get("set-cookie") ?? "";
    expect(setCookie).toMatch(/authjs\.csrf-token=[^;]+/);
    const body = (await res.json()) as { csrfToken?: unknown };
    expect(typeof body.csrfToken).toBe("string");
    expect((body.csrfToken as string).length).toBeGreaterThan(16);
  });
});
