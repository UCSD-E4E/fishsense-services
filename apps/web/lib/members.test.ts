// The tenant's members and their review levels, for its admins (0065; the
// owner, 2026-10-22: name and email from each person's sign-in). Pinned here:
//  * the list is asked for as the signed-in admin; a refusal (not an admin,
//    not a member) is null, for the page to say so;
//  * a level is set with the API's own words on a refusal;
//  * someone who hasn't signed in since names were kept is shown by what
//    there is: their email, else a short form of their account id.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getMembers, memberLabel, setMemberSkill, type Member } from "./members";

type FetchSig = (input: Request, init?: RequestInit) => Promise<Response>;
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const USER = "11111111-2222-3333-4444-555555555555";

beforeEach(() => vi.stubEnv("FISHSENSE_API_URL", "http://api.test"));
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("getMembers", () => {
  it("asks as the signed-in admin", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => json([]));
    vi.stubGlobal("fetch", fetchMock);

    expect(await getMembers("tok", "lab")).toEqual([]);
    const [request] = fetchMock.mock.calls[0];
    expect(request.url).toBe("http://api.test/tenants/lab/members");
    expect(request.headers.get("authorization")).toBe("Bearer tok");
  });

  it("is null for someone who isn't the tenant's admin", async () => {
    vi.stubGlobal("fetch", vi.fn<FetchSig>(async () => json({ detail: "admins only" }, 403)));
    expect(await getMembers("tok", "lab")).toBeNull();
    vi.stubGlobal("fetch", vi.fn<FetchSig>(async () => json({ detail: "x" }, 404)));
    expect(await getMembers("tok", "lab")).toBeNull();
  });
});

describe("setMemberSkill", () => {
  it("puts the level for one kind", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await setMemberSkill("tok", "lab", USER, "head_tail", "trusted")).toEqual({ ok: true });
    const [request] = fetchMock.mock.calls[0];
    expect(request.method).toBe("PUT");
    expect(request.url).toBe(`http://api.test/tenants/lab/members/${USER}/skills/head_tail`);
    expect(await request.json()).toEqual({ level: "trusted" });
  });

  it("gives a refusal's own reason", async () => {
    vi.stubGlobal("fetch", vi.fn<FetchSig>(async () => json({ detail: "not a member" }, 404)));
    expect(await setMemberSkill("tok", "lab", USER, "laser", "expert")).toEqual({ ok: false, error: "not a member" });
  });

  it("refuses a level or kind that isn't one before any request", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await expect(setMemberSkill("tok", "lab", USER, "laser", "god" as never)).rejects.toThrow();
    await expect(setMemberSkill("tok", "lab", USER, "fins" as never, "expert")).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("memberLabel", () => {
  const m = (fields: Partial<Member>): Member => ({
    user_id: USER,
    sub: "ff63243036514c6490afa08e5a41c747e70087a089e3fd191cca2a383a951656",
    role: "member",
    name: null,
    email: null,
    skills: { laser: "learning", head_tail: "learning", species: "learning" },
    ...fields,
  });

  it("is the name, else the email, else a short account id", () => {
    expect(memberLabel(m({ name: "Ada", email: "ada@x.org" }))).toBe("Ada");
    expect(memberLabel(m({ email: "ada@x.org" }))).toBe("ada@x.org");
    expect(memberLabel(m({}))).toBe("Account ff632430…");
    expect(memberLabel(m({ sub: "service:fishsense-orchestrator" }))).toBe("Service: fishsense-orchestrator");
    // A readable id is shown whole; only a hash is shortened.
    expect(memberLabel(m({ sub: "web-service" }))).toBe("web-service");
  });
});
