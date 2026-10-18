/**
 * Tenant slugs and uuids, checked before they go into an API path: a value
 * from the URL or the environment that could steer the request elsewhere is
 * refused (request forgery). The one definition: captures.ts, observations.ts
 * and env.ts all check with these.
 */

/** One path segment of lowercase letters, digits and dashes. */
const SLUG = /^[a-z0-9][a-z0-9-]*$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isSlug(value: string): boolean {
  return SLUG.test(value);
}

export function isUuid(value: string): boolean {
  return UUID.test(value);
}

export function safeSlug(slug: string): string {
  if (!isSlug(slug)) throw new Error(`Invalid tenant: ${JSON.stringify(slug)}`);
  return slug;
}

/** `what` names the id in the error ("capture", "fish"). */
export function safeUuid(id: string, what: string): string {
  if (!isUuid(id)) throw new Error(`Invalid ${what} id: ${JSON.stringify(id)}`);
  return id;
}
