/**
 * System admins in the web (docs/design/README.md, Tenants; the owner,
 * 2026-10-20): one may open any tenant. In one they are not a member of they
 * arrive read-only, under a banner; changing it takes a deliberate unlock,
 * behind a warning, that lasts a while. The API enforces all of it (0056);
 * the web only says so and asks.
 */
import { apiClient, failure, type Schemas } from "./api/client";
import { safeSlug } from "./ids";

export type TenantAccess = {
  role: string;
  isAdmin: boolean;
  access: string;
  unlockedUntil: string | null;
};

export type Banner = { kind: "read_only" } | { kind: "unlocked"; until: string };

/** Nothing for a member; read-only for anything it does not know (fail closed). */
export function banner(a: Pick<TenantAccess, "access" | "unlockedUntil">): Banner | null {
  if (a.access === "member") return null;
  if (a.access === "system_admin_unlocked" && a.unlockedUntil) {
    return { kind: "unlocked", until: a.unlockedUntil };
  }
  return { kind: "read_only" };
}

type Named = { slug: string; name: string };

/** Every tenant a system admin is not a member of, by name. */
export function otherTenants(mine: Named[], all: Named[]): Named[] {
  const own = new Set(mine.map((t) => t.slug));
  return all.filter((t) => !own.has(t.slug)).sort((a, b) => a.name.localeCompare(b.name));
}

export async function getIsSystemAdmin(accessToken: string): Promise<boolean> {
  const { data, error, response } = await apiClient(accessToken).GET("/me");
  if (data === undefined) throw failure("fishsense-api me failed", response, error);
  return data.is_system_admin;
}

/** Every tenant, for a system admin; none for anyone else (the API's 404). */
export async function getAllTenants(accessToken: string): Promise<Named[]> {
  const { data, error, response } = await apiClient(accessToken).GET("/admin/tenants");
  if (data !== undefined) return data;
  if (response.status === 404) return [];
  throw failure("fishsense-api tenants failed", response, error);
}

/** How the caller is in `slug`, or null where they may not be at all. */
export async function getTenantAccess(accessToken: string, slug: string): Promise<TenantAccess | null> {
  const { data, error, response } = await apiClient(accessToken).GET("/tenants/{slug}/membership", {
    params: { path: { slug: safeSlug(slug) } },
  });
  if (data !== undefined) {
    const m: Schemas["MyMembership"] = data;
    return {
      role: m.role,
      isAdmin: m.is_admin === true,
      access: m.access ?? "member",
      unlockedUntil: m.unlocked_until ?? null,
    };
  }
  if (response.status === 404) return null;
  throw failure("fishsense-api membership failed", response, error);
}

/** The deliberate unlock: the caller becomes the tenant's admin until the time returned. */
export async function unlockTenant(accessToken: string, slug: string): Promise<string> {
  const { data, error, response } = await apiClient(accessToken).POST(
    "/tenants/{slug}/system-admin/unlock",
    { params: { path: { slug: safeSlug(slug) } } },
  );
  if (data === undefined) throw failure("Could not unlock the tenant", response, error);
  return data.until;
}
