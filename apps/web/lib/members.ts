/**
 * A tenant's members and their review levels, for its admins (0063, 0065).
 *
 * Each member is Learning, Trusted or Expert at reviewing the laser dot, the
 * head and tail, and the species (lib/reviews.ts says what each level does).
 * An admin sets the levels here until levels are earned. Members are named
 * as their sign-in names them: the owner chose to keep each person's name and
 * email from their token (2026-10-22), shown to their tenant's admins only.
 */
import { apiClient, detailOf, failure, type Schemas } from "./api/client";
import { safeSlug, safeUuid } from "./ids";

export type Member = Schemas["MemberView"];
export type Kind = "laser" | "head_tail" | "species";
export type Level = Member["skills"][string];

const KINDS: readonly Kind[] = ["laser", "head_tail", "species"];
const LEVELS: readonly Level[] = ["learning", "trusted", "expert"];

/** The tenant's members, or null for a caller who isn't its admin. */
export async function getMembers(accessToken: string, slug: string): Promise<Member[] | null> {
  const { data, error, response } = await apiClient(accessToken).GET("/tenants/{slug}/members", {
    params: { path: { slug: safeSlug(slug) } },
  });
  if (data !== undefined) return data;
  if (response.status === 403 || response.status === 404) return null;
  throw failure("fishsense-api members failed", response, error);
}

/** Set a member's level in one kind; a refusal says why, in the API's words. */
export async function setMemberSkill(
  accessToken: string,
  slug: string,
  userId: string,
  kind: Kind,
  level: Level,
): Promise<{ ok: true } | { ok: false; error: string }> {
  if (!KINDS.includes(kind)) throw new Error(`Not a kind of review: ${kind}`);
  if (!LEVELS.includes(level)) throw new Error(`Not a level: ${level}`);
  const { error, response } = await apiClient(accessToken).PUT(
    "/tenants/{slug}/members/{user_id}/skills/{kind}",
    {
      params: { path: { slug: safeSlug(slug), user_id: safeUuid(userId, "user"), kind } },
      body: { level },
    },
  );
  if (response.ok) return { ok: true };
  return { ok: false, error: detailOf(error) ?? `Not saved (${response.status} ${response.statusText})` };
}

/** How a member is named: their name, else their email, else a short form of
 *  their account id (they haven't signed in since names were kept). */
export function memberLabel(m: Member): string {
  if (m.name) return m.name;
  if (m.email) return m.email;
  if (m.sub.startsWith("service:")) return `Service: ${m.sub.slice("service:".length)}`;
  // Authentik's hashed subject: 64 hex characters, shortened.
  return /^[0-9a-f]{64}$/i.test(m.sub) ? `Account ${m.sub.slice(0, 8)}…` : m.sub;
}
