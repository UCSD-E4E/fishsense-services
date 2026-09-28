// New in v2. A page can't write the session cookie, so a portal page whose
// access token has expired bounces through /api/session/refresh, which can.
// Its `callbackUrl` comes from the query string, so it must never become an
// open redirect.
import { describe, expect, it } from "vitest";
import { refreshPath, safeCallbackPath } from "./session-refresh";

describe("safeCallbackPath", () => {
  it.each(["/portal", "/portal/calibration", "/portal/triage?kind=headtail"])(
    "keeps a local path: %s",
    (path) => {
      expect(safeCallbackPath(path)).toBe(path);
    },
  );

  it.each([
    "https://evil.example/portal",
    "//evil.example/portal",
    "/\\evil.example",
    "\\\\evil.example",
    "javascript:alert(1)",
    "portal",
    "",
    null,
  ])("falls back to /portal for %j", (value) => {
    expect(safeCallbackPath(value)).toBe("/portal");
  });
});

describe("refreshPath", () => {
  it("sends the page back to itself after the refresh", () => {
    expect(refreshPath("/portal/triage?kind=laser")).toBe(
      "/api/session/refresh?callbackUrl=%2Fportal%2Ftriage%3Fkind%3Dlaser",
    );
  });
});
