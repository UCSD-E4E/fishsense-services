/**
 * Every tenant page lives under its tenant's slug (docs/design/README.md,
 * Tenants; the owner, 2026-10-20): `/lab/fish`, `/lab/data/missions/383`.
 * The path says whose data a page shows, so a link can be shared as it is.
 *
 * The pages before this took the tenant from `?tenant=` or a middle segment
 * (`/fish/lab/…`); `legacyRedirect` moves those to their new home, and the
 * middleware remembers the last tenant a user opened (`LAST_TENANT_COOKIE`),
 * which a link that names none goes to.
 */

/** Never a tenant: the web's own top-level routes and files. The API refuses
 *  them too (`tenant_slugs.py`); tests/test_reserved_tenant_slugs.py keeps the
 *  two lists the same. */
export const RESERVED_SLUGS = [
  "api",
  "captures",
  "deployments",
  "fish",
  "missions",
  "portal",
  "favicon",
  "logo",
  "admin",
  "map",
  "settings",
] as const;

import { isSlug } from "./ids";

export const LAST_TENANT_COOKIE = "fishsense-tenant";

const enc = encodeURIComponent;

export const paths = {
  tenant: (slug: string) => `/${enc(slug)}`,
  fish: (slug: string) => `/${enc(slug)}/fish`,
  fishDetail: (slug: string, capture: string, fish: string) =>
    `/${enc(slug)}/fish/${enc(capture)}/${enc(fish)}`,
  data: (slug: string) => `/${enc(slug)}/data`,
  deployment: (slug: string, number: number) => `/${enc(slug)}/data/deployments/${number}`,
  missions: (slug: string) => `/${enc(slug)}/data/missions`,
  mission: (slug: string, number: number) => `/${enc(slug)}/data/missions/${number}`,
  captures: (slug: string) => `/${enc(slug)}/data/captures`,
  capture: (slug: string, uuid: string) => `/${enc(slug)}/data/captures/${enc(uuid)}`,
};

export function isReserved(slug: string): boolean {
  return (RESERVED_SLUGS as readonly string[]).includes(slug) || slug.startsWith("_");
}

/** A string that can be a tenant: well formed, and not the web's own. */
function isTenant(slug: string): boolean {
  return isSlug(slug) && !isReserved(slug);
}

function segments(pathname: string): string[] {
  return pathname.split("/").filter(Boolean).map(decodeURIComponent);
}

/** The tenant a page belongs to, or null for a page that is nobody's. */
export function tenantOf(pathname: string): string | null {
  const [first] = segments(pathname);
  // Only a well-formed slug: a browser's ask for a file the app does not
  // serve (`/favicon.ico`) renders a 404 that is nobody's.
  return first && isTenant(first) ? first : null;
}

/** The same section in another tenant: a record does not carry over. */
export function switchHref(pathname: string, slug: string): string {
  const [, section, part] = tenantOf(pathname) ? segments(pathname) : [];
  if (section === "data") {
    return part === "missions" || part === "captures"
      ? `${paths.data(slug)}/${part}`
      : paths.data(slug);
  }
  return paths.fish(slug);
}

/** Where an old link belongs, or null when it is not an old link (or names no
 *  tenant and none was used before: the page signs in and picks one). */
export function legacyRedirect(
  pathname: string,
  search: URLSearchParams,
  lastTenant: string | null,
): string | null {
  const [root, slug, ...rest] = segments(pathname);
  const section: Record<string, (s: string) => string> = {
    fish: paths.fish,
    deployments: paths.data,
    missions: paths.missions,
    captures: paths.captures,
  };
  if (!root || !(root in section)) return null;

  if (slug !== undefined) {
    if (!isTenant(slug)) return null;
    const tail = [slug, ...rest].slice(1).map(enc).join("/");
    const base = root === "deployments" ? `${paths.data(slug)}/deployments` : section[root](slug);
    return tail ? `${base}/${tail}` : base;
  }

  const named = search.get("tenant");
  const tenant = named ?? lastTenant;
  if (!tenant || !isTenant(tenant)) return null;
  const kept = new URLSearchParams(search);
  kept.delete("tenant");
  const query = kept.toString();
  return `${section[root](tenant)}${query ? `?${query}` : ""}`;
}

export type Routing = { redirect?: string; remember?: string };

/**
 * The middleware's decision. It moves an old link that names its tenant, and
 * remembers the tenant of each tenant page opened. An old link that names
 * none is its page's to settle: only the page knows the user's memberships,
 * so only it can trust the cookie.
 */
export function routeRequest(
  pathname: string,
  search: URLSearchParams,
  cookie: string | null,
): Routing {
  const redirect = legacyRedirect(pathname, search, null);
  if (redirect) return { redirect };
  const tenant = tenantOf(pathname);
  const section = segments(pathname)[1];
  const isPage = section === undefined || section === "fish" || section === "data";
  return tenant && isPage && tenant !== cookie ? { remember: tenant } : {};
}
