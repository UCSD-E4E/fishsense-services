/**
 * Personal API tokens, for scripts and agents (0066; the owner, 2026-10-23).
 *
 * A token acts as the person who made it. It is read-only unless they opt into
 * an edit token, and its kind is fixed once made. Agents connect to the API's
 * MCP endpoint with a read token (the endpoint refuses edit tokens); an edit
 * token is for scripts that call the REST API directly. The secret is shown
 * once, when the token is made, and never again.
 */
import { apiClient, detailOf, failure, type Schemas } from "./api/client";
import { safeUuid } from "./ids";

export type ApiToken = Schemas["TokenView"];
export type NewToken = Schemas["NewToken"];
export type Scope = ApiToken["scope"];

const SCOPES: readonly Scope[] = ["read", "edit"];
/** Where the API is reached from outside, for the MCP command. */
const PUBLIC_API_URL = "https://api.fishsense.e4e.ucsd.edu";

/** The signed-in user's tokens, newest first; never their secrets. */
export async function listTokens(accessToken: string): Promise<ApiToken[]> {
  const { data, error, response } = await apiClient(accessToken).GET("/me/tokens");
  if (data !== undefined) return data;
  throw failure("fishsense-api tokens failed", response, error);
}

/** Make a token; a refusal says why, in the API's words. */
export async function createToken(
  accessToken: string,
  { name, scope, expiresInDays }: { name: string; scope: Scope; expiresInDays: number },
): Promise<{ ok: true; token: NewToken } | { ok: false; error: string }> {
  if (!SCOPES.includes(scope)) throw new Error(`Not a kind of token: ${scope}`);
  const { data, error, response } = await apiClient(accessToken).POST("/me/tokens", {
    body: { name, scope, expires_in_days: expiresInDays },
  });
  if (data !== undefined) return { ok: true, token: data };
  return { ok: false, error: detailOf(error) ?? `Not made (${response.status} ${response.statusText})` };
}

export async function revokeToken(
  accessToken: string,
  tokenId: string,
): Promise<{ ok: true } | { ok: false; error: string }> {
  const { error, response } = await apiClient(accessToken).DELETE("/me/tokens/{token_id}", {
    params: { path: { token_id: safeUuid(tokenId, "token") } },
  });
  if (response.ok) return { ok: true };
  return { ok: false, error: detailOf(error) ?? `Not revoked (${response.status} ${response.statusText})` };
}

/** The MCP endpoint, on the public API (FISHSENSE_API_PUBLIC_URL overrides). */
export function mcpUrl(): string {
  const base = process.env.FISHSENSE_API_PUBLIC_URL || PUBLIC_API_URL;
  return `${base.replace(/\/+$/, "")}/mcp`;
}

/** The command that connects Claude Code to FishSense with a read token. */
export function mcpCommand(token: string): string {
  return `claude mcp add --transport http fishsense ${mcpUrl()} --header "Authorization: Bearer ${token}"`;
}
