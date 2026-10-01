// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/label-studio-tasks.ts.
import { env } from "./env";
import { getAccessToken } from "./label-studio";
import { lsFetch } from "./label-studio-limiter";
import type { LsRegion, LsTask } from "./triage";

export type TaskPage = {
  tasks: LsTask[];
  total: number;
  /** True when the page is past the end of the result set. */
  drained: boolean;
};

/**
 * Authenticated Label Studio request, refreshing the access token once on 401.
 *
 * The configured key is a *personal access token* — itself a refresh token,
 * good for a JWT that lives about five minutes. That is shorter than a
 * labeling session, so every call has to be able to re-mint mid-flight. Doing
 * it here rather than per-caller is why nothing above this layer knows the
 * token exists.
 */
async function authed(path: string, init: RequestInit = {}): Promise<Response> {
  const url = `${env.labelStudioUrl}${path}`;
  const send = async (token: string) =>
    lsFetch(url, {
      ...init,
      headers: {
        ...(init.headers ?? {}),
        Authorization: `Bearer ${token}`,
      },
      cache: "no-store",
    });

  // 429s are handled by the shared throttle, which also holds back the
  // requests queued behind this one. Retrying here as well would restore the
  // amplification that throttle exists to remove.
  const response = await send(await getAccessToken());
  if (response.status === 401 || response.status === 403) {
    return send(await getAccessToken(true));
  }

  return response;
}

const DEFAULT_PAGE_SIZE = 50;

/**
 * One page of a project's tasks, with predictions and annotations inlined.
 *
 * A 404 here is not a failure. DRF answers a page past the end of a result set
 * with `{"detail": "Invalid page."}` and status 404 rather than an empty list,
 * so a paging loop that treats it as an error dies mid-scan instead of moving
 * on to the next project.
 */
export async function listTasks(
  projectId: number,
  page: number,
  pageSize: number = DEFAULT_PAGE_SIZE,
): Promise<TaskPage> {
  const response = await authed(
    `/api/tasks/?project=${projectId}&page=${page}&page_size=${pageSize}`,
  );

  if (response.status === 404) {
    return { tasks: [], total: 0, drained: true };
  }
  if (!response.ok) {
    throw new Error(
      `Label Studio tasks fetch failed for project ${projectId} page ${page}: ` +
        `${response.status} ${response.statusText}`,
    );
  }

  const body = (await response.json()) as
    | LsTask[]
    | { tasks?: LsTask[]; total?: number };

  // Older instances return a bare array; newer ones wrap it.
  const tasks = Array.isArray(body) ? body : (body.tasks ?? []);
  const total = Array.isArray(body) ? body.length : (body.total ?? tasks.length);
  return { tasks, total, drained: tasks.length === 0 };
}

/**
 * Accept: store the prediction's regions verbatim as a human annotation.
 *
 * `result` is passed through untouched and MUST be the array read straight off
 * the prediction. The sync activity reads `original_width`, `original_height`,
 * `value.x`, `value.y` and `value.keypointlabels[0]` back out of what is
 * stored here, so copying is correct by construction where rebuilding is a new
 * chance to be wrong.
 *
 * `lead_time` is Label Studio's own "seconds spent on this task" field. It is
 * the cheapest signal available for rubber-stamping — a labeler averaging a
 * few hundred milliseconds is not inspecting anything — so it is always sent.
 *
 * There is deliberately no skip counterpart. See the note in `triage.ts`.
 */
export async function acceptPrediction(
  taskId: number,
  result: LsRegion[],
  leadTimeMs: number,
): Promise<number> {
  const response = await authed(`/api/tasks/${taskId}/annotations/`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      result,
      lead_time: leadTimeMs,
      was_cancelled: false,
      // Label Studio stamps `origin: "prediction"` on the stored regions
      // itself, which is what the auto-accept gate also produces. Nothing
      // downstream distinguishes the two, by design.
    }),
  });

  if (!response.ok) {
    throw new Error(
      `Label Studio annotation POST failed for task ${taskId}: ` +
        `${response.status} ${response.statusText}`,
    );
  }

  const data = (await response.json()) as { id?: number };
  return data.id ?? 0;
}

/** One annotation's id and the task it is on; null when there is none. */
export async function getAnnotation(
  annotationId: number,
): Promise<{ id: number; task: number } | null> {
  const response = await authed(`/api/annotations/${annotationId}/`);
  if (response.status === 404) return null;
  if (!response.ok) {
    throw new Error(
      `Label Studio annotation ${annotationId} fetch failed: ` +
        `${response.status} ${response.statusText}`,
    );
  }
  const { id, task } = (await response.json()) as { id: number; task: number };
  return { id, task };
}

/** Delete an annotation — used to undo an accept made moments ago. */
export async function deleteAnnotation(annotationId: number): Promise<void> {
  const response = await authed(`/api/annotations/${annotationId}/`, { method: "DELETE" });
  if (!response.ok && response.status !== 404) {
    throw new Error(
      `Label Studio annotation DELETE failed for ${annotationId}: ` +
        `${response.status} ${response.statusText}`,
    );
  }
}

/**
 * Fetch a task's frame, letting Label Studio say where it lives.
 *
 * The URL is NOT constructed here any more. It used to be built by hand as
 * `/tasks/{id}/resolve/?fileuri={base64}`, copied from a note about hosted
 * Label Studio — and every fetch came back non-OK, which the route turned into
 * a bare 502 with nothing to diagnose from.
 *
 * The supported route is to ask: request the task with `resolve_uri=true` and
 * read whatever `data.image` becomes. What comes back varies by deployment and
 * by how the project's storage is configured, so all three shapes are handled:
 *
 *   * an absolute `http(s)` URL — a presigned link, fetched WITHOUT our
 *     Authorization header, because sending a bearer to S3 can itself be
 *     rejected;
 *   * a root-relative path on Label Studio's own server — fetched WITH auth,
 *     since that endpoint is authenticated;
 *   * still `s3://` — Label Studio could not resolve it, which means the
 *     project has no storage connected, and no amount of fetching will help.
 *     Reported as such rather than retried.
 */
/**
 * Hosts this server is allowed to fetch a frame from.
 *
 * The resolved URL comes out of Label Studio task data, and a portal user
 * chooses which task — so without this the route is a server-side request
 * forgery: ask for a task whose `data.image` points at `fishsense-api:8000`,
 * or at a cloud metadata endpoint, and this server fetches it from inside the
 * private network and streams the body back.
 *
 * Label Studio is trusted, but task data is writable by anyone who can create
 * a task, so "it came from Label Studio" is not the same as "it is safe to
 * fetch". The previous implementation was accidentally safe here because it
 * only ever hit a fixed path on a fixed host; resolving properly removed that
 * accident and had to replace it deliberately.
 *
 * Defaults to the Label Studio host. `TRIAGE_IMAGE_HOSTS` adds others —
 * production presigns against the object store, whose host is not otherwise
 * known here, and the rejection message names the host so it can be added
 * rather than guessed.
 */
function allowedImageHosts(): Set<string> {
  const hosts = new Set<string>();
  try {
    hosts.add(new URL(env.labelStudioUrl).host);
  } catch {
    // A malformed base is reported elsewhere; do not widen the allowlist.
  }
  // v2: projects' storage presigns against the object store (PLAN.md §9.11),
  // whose endpoint the stack already configures. v1 relied on
  // TRIAGE_IMAGE_HOSTS alone, which production never set.
  const objectStore = process.env.FISHSENSE_OBJECT_STORE_ENDPOINT_URL;
  if (objectStore) {
    try {
      hosts.add(new URL(objectStore).host);
    } catch {
      // Same: a malformed endpoint must not widen the allowlist.
    }
  }
  for (const extra of (process.env.TRIAGE_IMAGE_HOSTS ?? "").split(",")) {
    const host = extra.trim();
    if (host) hosts.add(host);
  }
  return hosts;
}

export type ResolvedImage =
  | { kind: "response"; response: Response; url: string }
  | { kind: "unresolved"; uri: string }
  | { kind: "blocked"; uri: string; host: string }
  | { kind: "foreign" };

/**
 * `ownedProjects` is the tenant's (`lib/tenant-tasks.ts`). v2 change: every
 * tenant's projects share one Label Studio workspace, so a task outside them
 * -- or one Label Studio doesn't have -- is `foreign`, decided on the task
 * this fetches anyway and before any frame is.
 */
export async function fetchTaskImage(
  taskId: number,
  ownedProjects: ReadonlySet<number>,
): Promise<ResolvedImage> {
  const task = await getTask(taskId, { resolveUri: true });
  if (typeof task?.project !== "number" || !ownedProjects.has(task.project)) {
    return { kind: "foreign" };
  }
  const uri = typeof task.data?.image === "string" ? task.data.image : "";

  if (!uri || uri.startsWith("s3://") || uri.startsWith("gs://")) {
    return { kind: "unresolved", uri };
  }

  if (/^https?:\/\//i.test(uri)) {
    let host: string;
    try {
      host = new URL(uri).host;
    } catch {
      return { kind: "blocked", uri, host: "(unparseable)" };
    }
    if (!allowedImageHosts().has(host)) {
      return { kind: "blocked", uri, host };
    }
    // Presigned: the signature IS the credential, and adding ours can trip
    // S3's "only one auth mechanism" rule.
    return { kind: "response", response: await fetch(uri, { cache: "no-store" }), url: uri };
  }

  const path = uri.startsWith("/") ? uri : `/${uri}`;
  return { kind: "response", response: await authed(path), url: path };
}

/** One task, with its predictions and annotations. */
export async function getTask(
  taskId: number,
  { resolveUri = false }: { resolveUri?: boolean } = {},
): Promise<LsTask | null> {
  // `resolve_uri=true` asks Label Studio to rewrite storage URIs in `data`
  // into something fetchable. It is off by default because the queue only
  // needs predictions, and resolving costs the server work per task.
  const query = resolveUri ? "?resolve_uri=true" : "";
  const response = await authed(`/api/tasks/${taskId}/${query}`);
  if (response.status === 404) return null;
  if (!response.ok) {
    throw new Error(
      `Label Studio task ${taskId} fetch failed: ${response.status} ${response.statusText}`,
    );
  }
  return (await response.json()) as LsTask;
}
