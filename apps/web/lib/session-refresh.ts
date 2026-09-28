/**
 * Where a portal page sends a session whose access token has expired.
 *
 * New in v2 (fishsense-lite@77e8f8e5's web never spent its access token, so
 * never refreshed it). A page render can't write the session cookie, so the
 * refresh happens in `/api/session/refresh`, a route handler, which then
 * sends the browser back. Its `callbackUrl` comes from the query string, so
 * only a local path is honoured -- anything else would be an open redirect.
 */

export const REFRESH_ROUTE = "/api/session/refresh";

/** `value` if it is a path on this site, else `/portal`. */
export function safeCallbackPath(value: string | null | undefined): string {
  if (!value || !value.startsWith("/")) return "/portal";
  // `//host` and `/\host` are protocol-relative to a browser.
  if (value.startsWith("//") || value.startsWith("/\\")) return "/portal";
  return value;
}

export function refreshPath(callbackPath: string): string {
  return `${REFRESH_ROUTE}?callbackUrl=${encodeURIComponent(callbackPath)}`;
}
