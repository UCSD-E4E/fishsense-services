/**
 * The typed client for the v2 API, generated from its OpenAPI document.
 *
 * New in v2: fishsense-lite@77e8f8e5's web hand-typed each response and built
 * URLs by string. PLAN.md §3 / §6.1: the portal talks to the v2 API through
 * `openapi-typescript` types (`./schema.d.ts`, generated from
 * `apps/web/openapi.json`, which the API's tests pin to the live spec) and
 * `openapi-fetch`.
 */
import createClient from "openapi-fetch";
import { env } from "../env";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

/** How a call is cached by Next: `revalidate` seconds, or never. */
export type Caching = { revalidate: number } | "no-store";

/**
 * A client that calls the API as the holder of `token`.
 *
 * Built per call, so the env stays lazily read and nothing about one caller
 * (the token) can outlive the request. The `fetch` wrapper is how Next's
 * `next.revalidate` reaches its patched fetch: openapi-fetch hands `fetch` a
 * `Request`, which has no room for it.
 */
export function apiClient(token: string, caching: Caching = "no-store") {
  const init: RequestInit & { next?: { revalidate: number } } =
    caching === "no-store" ? { cache: "no-store" } : { next: caching };
  return createClient<paths>({
    baseUrl: env.fishsenseApiUrl,
    headers: { Authorization: `Bearer ${token}` },
    fetch: (request: Request) => fetch(request, init),
  });
}

/** A refusal from the API, with its status and, when it gave one, its reason. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** The API's `detail`, when its error body carries one as text. */
export function detailOf(body: unknown): string | null {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return null;
}

/** `"{what}: {status} {statusText}[ — {detail}]"`, v1's message shape plus the
 *  API's reason, which is what a person reading the page needs. */
export function failure(what: string, response: Response, body: unknown): ApiError {
  const detail = detailOf(body);
  return new ApiError(
    `${what}: ${response.status} ${response.statusText}${detail ? ` — ${detail}` : ""}`,
    response.status,
  );
}
