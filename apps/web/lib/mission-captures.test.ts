// A mission's captures, measured or not (docs/design/README.md: a capture is
// every frame, measured or not). The Fish list is measured fish only, so an
// unmeasured capture is seen on its mission's page or nowhere.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  captureCardLink,
  getMissionCaptures,
  parseCaptureCursor,
  type CaptureCard,
} from "./mission-captures";

type FetchSig = (input: Request, init?: RequestInit) => Promise<Response>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

beforeEach(() => {
  vi.stubEnv("FISHSENSE_API_URL", "http://api.test");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("getMissionCaptures", () => {
  it("asks for a page of the mission's captures, from a cursor", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => jsonResponse({ captures: [], next: null }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await getMissionCaptures("t", "lab", 383, "2026-01-01T00:00:00+00:00~x")).toEqual({
      captures: [],
      next: null,
    });
    const url = new URL(fetchMock.mock.calls[0][0].url);
    expect(url.pathname).toBe("/tenants/lab/missions/383/captures");
    expect(url.searchParams.get("after")).toBe("2026-01-01T00:00:00+00:00~x");
  });

  it("is null for a mission the caller can't see", async () => {
    vi.stubGlobal("fetch", vi.fn<FetchSig>(async () => jsonResponse({ detail: "x" }, 404)));

    expect(await getMissionCaptures("t", "lab", 383)).toBeNull();
  });
});

describe("captureCardLink", () => {
  const card = (fish: CaptureCard["fish"]) =>
    ({ capture_id: "c1", captured_at: "2026-01-01T00:00:00Z", fish }) as unknown as CaptureCard;

  it("goes to the fish's page for a measured capture", () => {
    expect(captureCardLink("lab", card([{ fish_id: "f1", length_m: 0.3 }]), 383)).toBe("/lab/fish/c1/f1");
  });

  it("goes to the frame's own page for an unmeasured one (its automatic result)", () => {
    // ...with its place in the mission, so the frame page can offer the next frame.
    expect(captureCardLink("lab", card([]), 383)).toBe(
      "/lab/data/missions/383/frames/c1?at=2026-01-01T00%3A00%3A00Z~c1",
    );
  });
});

describe("getMissionCaptures's limit", () => {
  it("asks for one capture after a place: the next frame", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => jsonResponse({ captures: [], next: null }));
    vi.stubGlobal("fetch", fetchMock);

    await getMissionCaptures("t", "lab", 383, "2026-01-01T00:00:00Z~x", 1);

    const url = new URL(fetchMock.mock.calls[0][0].url);
    expect(url.searchParams.get("limit")).toBe("1");
  });
});

describe("parseCaptureCursor", () => {
  const id = "11111111-2222-3333-4444-555555555555";
  it("takes a time and a capture id", () => {
    expect(parseCaptureCursor(`2026-01-01T00:00:00Z~${id}`)).toBe(`2026-01-01T00:00:00Z~${id}`);
  });
  it("refuses anything else", () => {
    for (const bad of [undefined, "", "x", `nottime~${id}`, "2026-01-01T00:00:00Z~nope", `2026-01-01~${id}~more`]) {
      expect(parseCaptureCursor(bad)).toBeNull();
    }
  });
});
