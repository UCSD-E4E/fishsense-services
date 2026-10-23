// The public home: what FishSense does, for anyone. Someone already signed in
// (an account in no tenant yet) is not offered a fresh sign-in (2026-10-22).
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PublicHome } from "./public-home";

beforeEach(() => {
  vi.stubEnv("FISHSENSE_API_URL", "http://api.test");
  vi.stubGlobal("fetch", vi.fn(async () => new Response("down", { status: 503 })));
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("PublicHome", () => {
  it("offers sign-in to someone signed out", async () => {
    expect(renderToStaticMarkup(await PublicHome({ signedIn: false }))).toContain(">Sign in<");
  });

  it("does not offer it again to someone signed in", async () => {
    expect(renderToStaticMarkup(await PublicHome({ signedIn: true }))).not.toContain("Sign in");
  });
});
