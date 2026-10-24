// Personal API tokens (0066; the owner, 2026-10-23). Pinned here:
//  * tokens are listed, made and revoked as the signed-in user;
//  * a token is read-only unless the form asks for edit;
//  * a refusal comes back in the API's own words;
//  * the MCP endpoint is on the public API.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createToken, listTokens, mcpUrl, revokeToken } from "./api-tokens";

type FetchSig = (input: Request, init?: RequestInit) => Promise<Response>;
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const ID = "11111111-2222-3333-4444-555555555555";

beforeEach(() => vi.stubEnv("FISHSENSE_API_URL", "http://api.test"));
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("listTokens", () => {
  it("asks as the signed-in user", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => json([]));
    vi.stubGlobal("fetch", fetchMock);

    expect(await listTokens("tok")).toEqual([]);
    const [request] = fetchMock.mock.calls[0];
    expect(request.url).toBe("http://api.test/me/tokens");
    expect(request.headers.get("authorization")).toBe("Bearer tok");
  });
});

describe("createToken", () => {
  it("is read-only unless asked", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => json({ token: "fishsense_x" }, 201));
    vi.stubGlobal("fetch", fetchMock);

    const made = await createToken("tok", { name: "agent", scope: "read", expiresInDays: 30 });

    expect(made).toEqual({ ok: true, token: { token: "fishsense_x" } });
    const [request] = fetchMock.mock.calls[0];
    expect(request.method).toBe("POST");
    expect(await request.json()).toEqual({ name: "agent", scope: "read", expires_in_days: 30 });
  });

  it("gives a refusal's own reason", async () => {
    vi.stubGlobal("fetch", vi.fn<FetchSig>(async () => json({ detail: "too many live tokens" }, 409)));

    expect(await createToken("tok", { name: "a", scope: "read", expiresInDays: 90 })).toEqual({
      ok: false,
      error: "too many live tokens",
    });
  });

  it("refuses a kind that isn't read or edit before any request", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await expect(createToken("tok", { name: "a", scope: "admin" as never, expiresInDays: 90 })).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("revokeToken", () => {
  it("deletes the token by id", async () => {
    const fetchMock = vi.fn<FetchSig>(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await revokeToken("tok", ID)).toEqual({ ok: true });
    const [request] = fetchMock.mock.calls[0];
    expect(request.method).toBe("DELETE");
    expect(request.url).toBe(`http://api.test/me/tokens/${ID}`);
  });

  it("refuses an id that isn't one before any request", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await expect(revokeToken("tok", "../admin")).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("mcpUrl", () => {
  it("is the public API's MCP endpoint", () => {
    expect(mcpUrl()).toBe("https://api.fishsense.e4e.ucsd.edu/mcp");
  });

  it("follows the public API's address when it is set", () => {
    vi.stubEnv("FISHSENSE_API_PUBLIC_URL", "https://api.example.test/");
    expect(mcpUrl()).toBe("https://api.example.test/mcp");
  });
});
