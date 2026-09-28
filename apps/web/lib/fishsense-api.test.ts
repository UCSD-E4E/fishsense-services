// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/fishsense-api.test.ts.
//
// v2 changes, each pinned below:
//  * one tenant-scoped route, `/tenants/{slug}/label-studio-projects?kind=`,
//    called through the client generated from the API's OpenAPI document;
//  * a bearer token from the web's Authentik service account, not v1's
//    Basic-auth password, re-minted once if the API refuses it;
//  * v2 spells the kinds as the database does (`head_tail`, `slate`);
//  * the caller's own membership, which is what the portal gate now asks.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { getServiceToken } = vi.hoisted(() => ({
  getServiceToken: vi.fn(async (_force?: boolean) => "svc-token"),
}));
vi.mock("./service-token", () => ({ getServiceToken }));

import { ApiError, getMyMembership, getProjectIds } from "./fishsense-api";

const KINDS = ["laser", "species", "headtail", "dive-slate"] as const;

beforeEach(() => {
  vi.stubEnv("FISHSENSE_API_URL", "http://api.test");
  vi.stubEnv("FISHSENSE_TENANT", "lab");
  getServiceToken.mockClear();
  getServiceToken.mockImplementation(async () => "svc-token");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    statusText: init.statusText ?? "OK",
    headers: { "content-type": "application/json", ...(init.headers ?? {}) },
  });
}

type NextFetchInit = RequestInit & { next?: { revalidate?: number } };
type FetchSig = (input: Request, init?: NextFetchInit) => Promise<Response>;

const urlOf = (request: Request) => new URL(request.url);

describe("getProjectIds", () => {
  it("calls the tenant's projects route for all four kinds, with incomplete=true and a bearer", async () => {
    const fetchMock = vi.fn<FetchSig>(async (request) => {
      const kind = urlOf(request).searchParams.get("kind");
      if (kind === "laser") return jsonResponse([42, 43]);
      if (kind === "species") return jsonResponse([70]);
      if (kind === "head_tail") return jsonResponse([44, 45]);
      if (kind === "slate") return jsonResponse([66]);
      throw new Error(`unexpected url: ${request.url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = {
      laser: await getProjectIds("laser", 60),
      species: await getProjectIds("species", 60),
      headtail: await getProjectIds("headtail", 60),
      "dive-slate": await getProjectIds("dive-slate", 60),
    };

    expect(result).toEqual({
      laser: [42, 43],
      species: [70],
      headtail: [44, 45],
      "dive-slate": [66],
    });

    expect(fetchMock).toHaveBeenCalledTimes(4);
    for (const [request] of fetchMock.mock.calls) {
      const url = urlOf(request);
      expect(`${url.origin}${url.pathname}`).toBe(
        "http://api.test/tenants/lab/label-studio-projects",
      );
      expect(url.searchParams.get("incomplete")).toBe("true");
      expect(request.headers.get("Authorization")).toBe("Bearer svc-token");
    }
  });

  it("maps the web's kinds onto v2's spelling", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await Promise.all(KINDS.map((k) => getProjectIds(k, 60)));

    expect(
      fetchMock.mock.calls.map(([request]) => urlOf(request).searchParams.get("kind")).sort(),
    ).toEqual(["head_tail", "laser", "slate", "species"]);
  });

  it("names the configured tenant in the path", async () => {
    vi.stubEnv("FISHSENSE_TENANT", "staging");
    const fetchMock = vi.fn<FetchSig>(async () => jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await getProjectIds("laser", 60);

    expect(urlOf(fetchMock.mock.calls[0][0]).pathname).toBe(
      "/tenants/staging/label-studio-projects",
    );
  });

  it("forwards revalidate to fetch's next option", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await Promise.all(KINDS.map((k) => getProjectIds(k, 123)));

    for (const [, init] of fetchMock.mock.calls) {
      expect(init?.next?.revalidate).toBe(123);
    }
  });

  it("throws when any endpoint returns non-OK", async () => {
    const fetchMock = vi.fn<FetchSig>(async (request) => {
      if (urlOf(request).searchParams.get("kind") === "laser") {
        return new Response("nope", { status: 500, statusText: "Server Error" });
      }
      return jsonResponse([]);
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getProjectIds("laser", 60)).rejects.toThrow(/laser.*500/);
  });

  it("hits all four endpoints in parallel (single Promise.all)", async () => {
    let inFlight = 0;
    let maxInFlight = 0;
    const fetchMock = vi.fn<FetchSig>(async () => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await new Promise((r) => setTimeout(r, 5));
      inFlight -= 1;
      return jsonResponse([]);
    });
    vi.stubGlobal("fetch", fetchMock);

    await Promise.all(KINDS.map((k) => getProjectIds(k, 60)));

    expect(maxInFlight).toBe(4);
  });

  it("re-mints the service token once when the API refuses it", async () => {
    // The cached token can be revoked or expire early; a 401 is "ask again",
    // not "no projects".
    getServiceToken.mockImplementation(async (force?: boolean) =>
      force ? "fresh-token" : "stale-token",
    );
    const fetchMock = vi.fn<FetchSig>(async (request) =>
      request.headers.get("Authorization") === "Bearer fresh-token"
        ? jsonResponse([9])
        : new Response("", { status: 401, statusText: "Unauthorized" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    expect(await getProjectIds("laser", 60)).toEqual([9]);
    expect(getServiceToken).toHaveBeenCalledWith(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not loop when the fresh token is refused too", async () => {
    const fetchMock = vi.fn<FetchSig>(
      async () => new Response("", { status: 401, statusText: "Unauthorized" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getProjectIds("laser", 60)).rejects.toThrow(/laser.*401/);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("the auto-accept gate filter", () => {
  // The gate decides which laser predictions a human never needs to see.
  // Surfacing a project it has not finished with sends a labeler at frames
  // the machine is about to accept for them — duplicated work, and worse,
  // work that looks voluntary. `gated=true` means "the gate is done here",
  // not merely "the gate has run here"; the API owns that distinction.
  it("asks only for gated laser projects", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await Promise.all(KINDS.map((k) => getProjectIds(k, 60)));

    const laserCall = fetchMock.mock.calls.find(
      ([request]) => urlOf(request).searchParams.get("kind") === "laser",
    );
    expect(urlOf(laserCall![0]).searchParams.get("gated")).toBe("true");
  });

  // Only laser predictions carry `gate_verdict`. v2's API refuses `gated` for
  // any other kind (422) rather than blanking the section, so these three
  // must never send it.
  it.each([
    ["species", "species"],
    ["headtail", "head_tail"],
    ["dive-slate", "slate"],
  ])("does not send gated for %s, which has no gate", async (_kind, apiKind) => {
    const fetchMock = vi.fn<FetchSig>(async () => jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await Promise.all(KINDS.map((k) => getProjectIds(k, 60)));

    const call = fetchMock.mock.calls.find(
      ([request]) => urlOf(request).searchParams.get("kind") === apiKind,
    );
    expect(urlOf(call![0]).searchParams.has("gated")).toBe(false);
  });

  it("still asks for incomplete work alongside the gate filter", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await Promise.all(KINDS.map((k) => getProjectIds(k, 60)));

    for (const [request] of fetchMock.mock.calls) {
      expect(urlOf(request).searchParams.get("incomplete")).toBe("true");
    }
  });
});

describe("getMyMembership", () => {
  it("asks the API, as the signed-in user, what they may do in the tenant", async () => {
    const fetchMock = vi.fn<FetchSig>(async () =>
      jsonResponse({ role: "admin", is_admin: true }),
    );
    vi.stubGlobal("fetch", fetchMock);

    expect(await getMyMembership("user-token")).toEqual({ role: "admin", isAdmin: true });

    const [request, init] = fetchMock.mock.calls[0];
    expect(request.url).toBe("http://api.test/tenants/lab/membership");
    expect(request.headers.get("Authorization")).toBe("Bearer user-token");
    // A role is never cached across users or requests.
    expect(request.cache === "no-store" || init?.cache === "no-store").toBe(true);
    expect(getServiceToken).not.toHaveBeenCalled();
  });

  it("is null when the user is not a member of the tenant", async () => {
    // The API answers 404 for "not a member" and "no such tenant" alike.
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchSig>(async () => jsonResponse({ detail: "Not Found" }, { status: 404 })),
    );

    expect(await getMyMembership("user-token")).toBeNull();
  });

  it.each([401, 500, 503])("throws on %i, which is not an answer", async (status) => {
    // A refused token or an outage must not read as "not a member": the
    // caller decides, and it fails closed with a reason it can show.
    vi.stubGlobal(
      "fetch",
      vi.fn<FetchSig>(async () => new Response("", { status, statusText: "x" })),
    );

    const error = await getMyMembership("user-token").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(status);
  });
});
