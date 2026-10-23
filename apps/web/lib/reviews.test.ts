// Reviewing the automatic results on a frame (the owner, 2026-10-21; the API
// is 0063). Pinned here:
//  * calls go to the API as the signed-in reviewer, ids checked first; a
//    refusal comes back as the API's own reason, for the reviewer to read;
//  * where a kind stands: the review that decided it (a confirm or an
//    agreement decides for the review it backs), and the Learning reviews
//    still waiting since;
//  * a Trusted or Expert reviewer may confirm a waiting review, never their
//    own; a learner's review names the latest waiting review by someone else,
//    so agreeing with it backs it;
//  * accept sends the model's points exactly, on the render they're on.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  acceptBody,
  agreeTarget,
  canConfirm,
  getCaptureReviews,
  getMySkills,
  kindState,
  postReview,
  reasonsFor,
  type Review,
} from "./reviews";
import type { AutomaticFrame } from "./automatic";

type FetchSig = (input: Request, init?: RequestInit) => Promise<Response>;
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const CAPTURE = "11111111-2222-3333-4444-555555555555";

beforeEach(() => vi.stubEnv("FISHSENSE_API_URL", "http://api.test"));
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

let seq = 0;
function review(fields: Partial<Review>): Review {
  seq += 1;
  return {
    review_id: `r${seq}`,
    reviewer_id: `u${seq}`,
    kind: "head_tail",
    action: "fix",
    level: "learning",
    counts: false,
    reason: null,
    point: null,
    head: { x: 1, y: 2 },
    tail: { x: 3, y: 4 },
    scientific_name: null,
    confirms_review_id: null,
    created_at: "2026-10-22T00:00:00Z",
    mine: false,
    ...fields,
  };
}

describe("the API calls", () => {
  it("lists a frame's reviews as the signed-in reviewer", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => json([]));
    vi.stubGlobal("fetch", fetchMock);

    expect(await getCaptureReviews("tok", "lab", CAPTURE)).toEqual([]);
    const [request] = fetchMock.mock.calls[0];
    expect(request.url).toBe(`http://api.test/tenants/lab/captures/${CAPTURE}/reviews`);
    expect(request.headers.get("authorization")).toBe("Bearer tok");
  });

  it("reads the reviewer's levels", async () => {
    vi.stubGlobal("fetch", vi.fn<FetchSig>(async () => json({ laser: "expert", head_tail: "learning", species: "trusted" })));

    expect((await getMySkills("tok", "lab")).laser).toBe("expert");
  });

  it("posts a review and says what it did", async () => {
    const fetchMock = vi.fn<FetchSig>(async () =>
      json({ review_id: "r", level: "expert", counts: true, label_id: "l" }, 201),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await postReview("tok", "lab", CAPTURE, { kind: "laser", action: "reject", reason: "dot_wrong" });

    expect(result).toEqual({ ok: true, recorded: { review_id: "r", level: "expert", counts: true, label_id: "l" } });
    const [request] = fetchMock.mock.calls[0];
    expect(request.method).toBe("POST");
    expect(await request.json()).toEqual({ kind: "laser", action: "reject", reason: "dot_wrong" });
  });

  it("gives a refusal's own reason", async () => {
    vi.stubGlobal("fetch", vi.fn<FetchSig>(async () => json({ detail: "a newer review of this frame already counts" }, 422)));

    expect(await postReview("tok", "lab", CAPTURE, { kind: "laser", action: "accept" })).toEqual({
      ok: false,
      error: "a newer review of this frame already counts",
    });
  });

  it("refuses an id that isn't one before any request", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await expect(getCaptureReviews("tok", "lab", "../x")).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("kindState", () => {
  it("is undecided with no reviews", () => {
    expect(kindState([], "head_tail")).toEqual({ decided: null, waiting: [] });
  });

  it("names the review that counts, and ignores other kinds", () => {
    const expert = review({ level: "expert", counts: true });
    const dot = review({ kind: "laser", level: "learning" });
    expect(kindState([expert, dot], "head_tail")).toEqual({ decided: expert, waiting: [] });
  });

  it("decides for the backed review on a confirm or an agreement", () => {
    const first = review({ counts: true });
    const confirm = review({ action: "confirm", level: "trusted", counts: true, confirms_review_id: first.review_id });
    expect(kindState([first, confirm], "head_tail").decided).toBe(first);
  });

  it("lists the Learning reviews still waiting since the decision", () => {
    const before = review({});
    const expert = review({ level: "expert", counts: true });
    const after = review({});
    expect(kindState([before, expert, after], "head_tail")).toEqual({ decided: expert, waiting: [after] });
  });
});

describe("who may back a waiting review", () => {
  it("a Trusted or Expert reviewer confirms someone else's", () => {
    expect(canConfirm("trusted", review({}))).toBe(true);
    expect(canConfirm("expert", review({}))).toBe(true);
    expect(canConfirm("learning", review({}))).toBe(false);
    expect(canConfirm("expert", review({ mine: true }))).toBe(false);
  });

  it("a learner's review names the latest waiting one by someone else", () => {
    const older = review({});
    const newer = review({});
    const own = review({ mine: true });
    expect(agreeTarget("learning", [older, newer, own])).toBe(newer.review_id);
    expect(agreeTarget("learning", [own])).toBeNull();
    expect(agreeTarget("trusted", [older])).toBeNull();
  });
});

describe("acceptBody", () => {
  const frame = {
    prediction_id: "p1",
    render_id: "rd1",
    laser: { x: 2000, y: 1500 },
    head: { x: 1800, y: 1500 },
    tail: { x: 2300, y: 1480 },
    species: "Fish, Hogfish (Lachnolaimus maximus)",
  } as AutomaticFrame;

  it("sends the model's points exactly, on their render", () => {
    expect(acceptBody("head_tail", frame)).toEqual({
      kind: "head_tail",
      action: "accept",
      automatic_head_tail_prediction_id: "p1",
      render_id: "rd1",
      head: { x: 1800, y: 1500 },
      tail: { x: 2300, y: 1480 },
    });
    expect(acceptBody("laser", frame)).toMatchObject({ point: { x: 2000, y: 1500 }, render_id: "rd1" });
    expect(acceptBody("species", frame)).toEqual({
      kind: "species",
      action: "accept",
      automatic_head_tail_prediction_id: "p1",
      scientific_name: "Fish, Hogfish (Lachnolaimus maximus)",
    });
  });

  it("is nothing to accept without the points or the render", () => {
    expect(acceptBody("head_tail", { ...frame, head: null })).toBeNull();
    expect(acceptBody("laser", { ...frame, render_id: null })).toBeNull();
    expect(acceptBody("species", { ...frame, species: null })).toBeNull();
  });
});

describe("reasonsFor", () => {
  it("offers each kind the reasons that fit it", () => {
    expect(reasonsFor("laser").map((r) => r.value)).toEqual(["dot_wrong", "no_fish"]);
    expect(reasonsFor("head_tail").map((r) => r.value)).toEqual([
      "bad_mask",
      "wrong_fish",
      "fish_unmeasurable",
      "no_fish",
    ]);
    expect(reasonsFor("species").map((r) => r.value)).toEqual(["wrong_fish", "no_fish"]);
  });
});
