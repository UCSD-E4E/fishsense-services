"use server";

import { revalidatePath } from "next/cache";
import { auth, unstable_update } from "@/auth";
import { accessTokenIsFresh } from "@/lib/authz";
import { createToken, revokeToken, type NewToken, type Scope } from "@/lib/api-tokens";

/** The signed-in user's access token, refreshed if it has gone stale. */
async function signedInToken(): Promise<string | { error: string }> {
  let session = await auth();
  if (!session?.user) return { error: "Not signed in" };
  if (!accessTokenIsFresh(session)) {
    session = await unstable_update({});
    if (!session || session.error || !accessTokenIsFresh(session)) {
      return { error: "Your sign-in has expired. Reload the page to sign in again." };
    }
  }
  return session.accessToken!;
}

/**
 * Make a personal API token (0066). Server actions are public endpoints: this
 * re-checks the sign-in and passes the API's answer back. The API refuses any
 * token-held request to make tokens, so only a signed-in person can.
 */
export async function createTokenAction(
  name: string,
  scope: Scope,
  expiresInDays: number,
): Promise<{ ok: true; token: NewToken } | { ok: false; error: string }> {
  const token = await signedInToken();
  if (typeof token !== "string") return { ok: false, ...token };
  try {
    const result = await createToken(token, { name, scope, expiresInDays });
    if (result.ok) revalidatePath("/tokens");
    return result;
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Not made" };
  }
}

export async function revokeTokenAction(tokenId: string): Promise<{ ok: true } | { ok: false; error: string }> {
  const token = await signedInToken();
  if (typeof token !== "string") return { ok: false, ...token };
  try {
    const result = await revokeToken(token, tokenId);
    if (result.ok) revalidatePath("/tokens");
    return result;
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Not revoked" };
  }
}
