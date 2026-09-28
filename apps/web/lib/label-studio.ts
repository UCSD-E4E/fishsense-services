// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/label-studio.ts.
import { env } from "./env";
import { lsFetch } from "./label-studio-limiter";

// Re-exported: the backoff arithmetic moved into the shared throttle, but the
// name is part of this module's surface.
export { retryAfterMs } from "./label-studio-limiter";

export type LabelStudioProject = {
  id: number;
  title: string;
  /** LS publish state. Unpublished projects are drafts or deliberately held
   *  and must not be surfaced — see `getActiveProjects`.
   *
   *  Optional because only `getProject` sources it; consumers that merely
   *  render id/title (e.g. `buildSections`) shouldn't have to carry it.
   *  Absent is treated as published, so filtering fails open. */
  isPublished?: boolean;
  /** Label Studio's `task_number` — every task in the project. */
  taskCount?: number;
  /** Label Studio's `finished_task_number` — tasks it considers labeled,
   *  the same `is_labeled` our sync copies into `<kind>Label.completed`.
   *
   *  Optional for the same reason as `isPublished`, and absent is treated as
   *  "unknown" rather than zero — see `hasOutstandingTasks`. */
  finishedTaskCount?: number;
};

// Hosted Label Studio (app.heartex.com) does NOT accept the configured key
// as a bearer credential. `LABEL_STUDIO_API_KEY` is a *personal access
// token* — a refresh token — which must be exchanged at
// `/api/token/refresh` for a short-lived access JWT that is then sent as
// `Authorization: Bearer <jwt>`.
//
// This is why the integration was originally kill-switched off: the app
// sent `Authorization: Token <key>`, which 401s on every request. Verified
// against prod 2026-07-21 — `Token <key>` and `Bearer <key>` both 401,
// while refresh -> `Bearer <jwt>` returns 200.
const DEFAULT_TOKEN_TTL_SECONDS = 240;
const EXPIRY_SKEW_SECONDS = 30;

type CachedToken = { token: string; expiresAtMs: number };
let cachedToken: CachedToken | null = null;
// Deduplicates concurrent refreshes. `getProjects` fans out over every id at
// once, so without this each one races on an empty cache and fires its own
// refresh POST — a dozen redundant round trips per page render.
let inFlightRefresh: Promise<string> | null = null;

/** Seconds until a JWT expires, from its `exp` claim; null if unreadable. */
function jwtLifetimeSeconds(token: string): number | null {
  const payload = token.split(".")[1];
  if (!payload) return null;
  try {
    const json = JSON.parse(
      Buffer.from(payload.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString(
        "utf8",
      ),
    ) as { exp?: number };
    if (typeof json.exp !== "number") return null;
    return json.exp - Math.floor(Date.now() / 1000);
  } catch {
    return null;
  }
}

async function refreshAccessToken(): Promise<string> {
  const url = `${env.labelStudioUrl}/api/token/refresh`;
  // Through the shared throttle like everything else. The token endpoint
  // spends from the same per-account budget as the resource calls, so a
  // refresh issued during a rate-limit window is what turns a slow page into
  // "Accept failed: token refresh failed: 429" on a frame already judged.
  const response = await lsFetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ refresh: env.labelStudioApiKey }),
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error(
      `Label Studio token refresh failed: ${response.status} ${response.statusText}`,
    );
  }

  const data = (await response.json()) as { access?: string };
  if (!data.access) {
    throw new Error("Label Studio token refresh returned no access token");
  }

  const lifetime = jwtLifetimeSeconds(data.access) ?? DEFAULT_TOKEN_TTL_SECONDS;
  const ttl = Math.max(lifetime - EXPIRY_SKEW_SECONDS, 1);
  cachedToken = { token: data.access, expiresAtMs: Date.now() + ttl * 1000 };
  return data.access;
}

export async function getAccessToken(forceRefresh = false): Promise<string> {
  if (!forceRefresh && cachedToken && cachedToken.expiresAtMs > Date.now()) {
    return cachedToken.token;
  }

  // Share an in-flight refresh even when forced.
  //
  // This used to be `!forceRefresh && inFlightRefresh`, so a burst of forced
  // refreshes each fired its own POST at the token endpoint — the queue and
  // the image proxy issue many calls at once, and one expired token turned
  // into N simultaneous refreshes. That is a good way to earn the 429 this
  // now retries: the stampede caused the rate limit it then failed on.
  if (inFlightRefresh) {
    return inFlightRefresh;
  }

  const pending = refreshAccessToken();
  inFlightRefresh = pending;
  try {
    return await pending;
  } finally {
    if (inFlightRefresh === pending) {
      inFlightRefresh = null;
    }
  }
}

/** Test seam — drops the cached access token and any in-flight refresh. */
export function __resetTokenCache(): void {
  cachedToken = null;
  inFlightRefresh = null;
}

/** A project fetch that failed, carrying the status so the caller can tell
 *  "this id is gone" (404) from "Label Studio would not answer" (429, 5xx). */
export class ProjectFetchError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ProjectFetchError";
    this.status = status;
  }
}

export async function getProject(
  id: number,
  revalidate: number,
): Promise<LabelStudioProject> {
  const url = `${env.labelStudioUrl}/api/projects/${id}`;

  // Through the shared throttle: this is the call that was 429ing in prod.
  // `getProjects` fetches every id at once, and a rate-limited response used
  // to throw straight out of here -- where `getProjects` could not tell it
  // apart from a dead legacy id and silently dropped the card.
  const attempt = async (token: string) =>
    lsFetch(url, {
      headers: { Authorization: `Bearer ${token}` },
      next: { revalidate },
    } as RequestInit);

  let response = await attempt(await getAccessToken());
  if (response.status === 401 || response.status === 403) {
    // Cached JWT went stale early (or was revoked) — refresh once.
    response = await attempt(await getAccessToken(true));
  }

  if (!response.ok) {
    console.error(`[label-studio] project ${id} fetch failed`, {
      url,
      status: response.status,
      statusText: response.statusText,
    });
    throw new ProjectFetchError(
      `Label Studio project ${id} fetch failed: ${response.status} ${response.statusText}`,
      response.status,
    );
  }

  const data = (await response.json()) as {
    id: number;
    title: string;
    is_published?: boolean;
    task_number?: number;
    finished_task_number?: number | null;
    num_tasks_with_annotations?: number | null;
  };
  // A missing `is_published` counts as published: the landing page should
  // fail OPEN (show the card) rather than silently hide real labeling work
  // if LS ever stops returning the field.
  //
  // The counts get no such default. They are passed through exactly as given
  // — a missing one stays `undefined`, because `hasOutstandingTasks` reads a
  // zero as a real answer and would hide the card.
  //
  // Two names, in this order, and the order is the load-bearing part.
  // `finished_task_number` counts tasks Label Studio calls *labeled* — the
  // same `is_labeled` our sync copies into `completed`, so the two surfaces
  // agree by construction. `num_tasks_with_annotations` counts tasks merely
  // *carrying* an annotation, which under an overlap > 1 or a skipped
  // annotation is reached earlier; preferring it would hide a project that
  // still has work. It is here only so a rename cannot silently switch the
  // filter off. Hosted LS returned BOTH on 2026-09-08 (project 285759:
  // 56/56/56, project 285676: 47 tasks, 32 and 32).
  const finished = data.finished_task_number ?? data.num_tasks_with_annotations;
  return {
    id: data.id,
    title: data.title,
    isPublished: data.is_published !== false,
    taskCount: data.task_number,
    finishedTaskCount: finished ?? undefined,
  };
}

export type ResolvedProjects = {
  projects: LabelStudioProject[];
  /** Ids Label Studio would not answer for — rate limit, outage, transport.
   *  NOT 404s: those ids really are gone. Non-zero means the list below is
   *  short, and the page has to say so instead of passing it off as the
   *  answer. */
  degraded: number;
};

export async function getProjects(
  ids: number[],
  revalidate: number,
): Promise<ResolvedProjects> {
  // Tolerate individual failures. fishsense-api still stores legacy project
  // ids (57-117) from the retired self-hosted instance, and every one of
  // them 404s on the hosted one. Under `Promise.all` a single dead id
  // rejected out of the server component and 500'd the entire landing page,
  // which is the other half of why this integration got kill-switched off.
  //
  // But "drop what we can't resolve" covered a rate limit as well as a dead
  // id, and those mean opposite things: ask again versus gone. Conflating
  // them is what emptied the Head/Tail section on 2026-09-07 — all 45
  // projects 429'd, all 45 silently dropped, and the page reported no
  // head/tail work while labelers had a full queue. The throttle makes that
  // burst unlikely now; this makes it impossible for it to be *silent*.
  // Same principle `triage-queue.ts` states for its own discovery: treating
  // "cannot ask" as "nothing to do" is the failure to avoid.
  const settled = await Promise.allSettled(ids.map((id) => getProject(id, revalidate)));

  const projects: LabelStudioProject[] = [];
  const goneIds: number[] = [];
  const unreachableIds: number[] = [];
  settled.forEach((result, index) => {
    if (result.status === "fulfilled") {
      projects.push(result.value);
    } else if (
      result.reason instanceof ProjectFetchError &&
      result.reason.status === 404
    ) {
      goneIds.push(ids[index]);
    } else {
      unreachableIds.push(ids[index]);
    }
  });

  if (goneIds.length > 0) {
    console.warn(
      `[label-studio] skipped ${goneIds.length} dead project id(s): ${goneIds.join(", ")}`,
    );
  }
  if (unreachableIds.length > 0) {
    console.error(
      `[label-studio] ${unreachableIds.length} project id(s) UNREACHABLE — the page is ` +
        `showing a short list: ${unreachableIds.join(", ")}`,
    );
  }

  return { projects, degraded: unreachableIds.length };
}
