// What "/" does with the visitor (app/page.tsx).
//
// The bug (2026-10-22): signed in with an expired access token, the header
// showed the user's name while home asked the API for their tenants with the
// dead token, got a 401 (prod: 2 of 9 such calls in 6 h), and fell back to the
// public page -- whose Sign in started a fresh Authentik login. Every other
// signed-in page renews the token first (capturesGate); so does home now.
import type { Session } from "next-auth";
import { describe, expect, it } from "vitest";
import { homeStep } from "./home";

const NOW = 1_800_000_000;

function session(fields: Partial<Session> = {}): Session {
  return {
    expires: "2099-01-01T00:00:00Z",
    user: { name: "Ada" },
    accessToken: "t",
    accessTokenExpiresAt: NOW + 300,
    ...fields,
  } as Session;
}

describe("homeStep", () => {
  it("shows the public page to someone signed out", () => {
    expect(homeStep(null, NOW)).toEqual({ kind: "public" });
    expect(homeStep({ expires: "2099-01-01T00:00:00Z" } as Session, NOW)).toEqual({ kind: "public" });
  });

  it("goes on to the user's tenant with a token the API will take", () => {
    expect(homeStep(session(), NOW)).toEqual({ kind: "tenant", accessToken: "t" });
  });

  it("renews an expired token first, and comes back home", () => {
    expect(homeStep(session({ accessTokenExpiresAt: NOW - 1 }), NOW)).toEqual({
      kind: "redirect",
      to: "/api/session/refresh?callbackUrl=%2F",
    });
  });

  it("signs in again only when the renewal itself failed", () => {
    expect(homeStep(session({ error: "RefreshAccessTokenError" } as Partial<Session>), NOW)).toEqual({
      kind: "redirect",
      to: "/api/auth/signin?callbackUrl=%2F",
    });
  });
});
