// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/app/api/triage/image/[taskId]/route.ts.
import { NextResponse } from "next/server";
import { auth } from "@/auth";
import { isPortalAuthorized } from "@/lib/authz";
import { fetchTaskImage } from "@/lib/label-studio-tasks";
import { tenantProjectIds } from "@/lib/tenant-tasks";

/**
 * Streams a task's frame through this server.
 *
 * Two reasons this is a proxy rather than a redirect. Label Studio's
 * `resolve` endpoint is on its API server and is **authenticated**, and the
 * bearer it wants expires in about five minutes — shorter than a labeling
 * session, so a URL handed to the browser would go stale mid-queue. Fetching
 * server-side means the client never holds a Label Studio credential at all.
 *
 * It takes only a task id, never a `fileuri`. Accepting a caller-supplied URI
 * would make this an open proxy that fetches anything the server can reach;
 * resolving the URI from the task itself keeps the reachable set to frames
 * that already belong to a project -- in v2, to one of this tenant's
 * projects, since every tenant's share one Label Studio workspace.
 */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ taskId: string }> },
) {
  const session = await auth();
  if (!session?.user || !isPortalAuthorized(session)) {
    return new NextResponse("Forbidden", { status: 403 });
  }

  const { taskId: raw } = await params;
  const taskId = Number(raw);
  if (!Number.isInteger(taskId) || taskId <= 0) {
    return new NextResponse("Bad task id", { status: 400 });
  }

  const resolved = await fetchTaskImage(taskId, await tenantProjectIds());

  if (resolved.kind === "foreign") {
    // v2: another tenant's task, in the Label Studio workspace every tenant
    // shares (lib/tenant-tasks.ts). Answered as for a task that isn't there,
    // so the route doesn't confirm other tenants' ids.
    return new NextResponse("No such task", { status: 404 });
  }

  if (resolved.kind === "unresolved") {
    // Label Studio handed the URI back unchanged, so the project has no
    // storage connected to resolve it against. Fetching harder will not help,
    // and saying which URI failed is the whole point of reporting it.
    const detail = `Label Studio could not resolve ${resolved.uri || "(no image in task data)"} for task ${taskId}. The project likely has no source storage connected.`;
    console.error("[triage/image] unresolved", { taskId, uri: resolved.uri });
    return new NextResponse(detail, { status: 502 });
  }

  if (resolved.kind === "blocked") {
    // Refused before the request was made. Naming the host is the point: in
    // production the frame is presigned against the object store, whose host
    // this server has no other way to learn, and it belongs in
    // TRIAGE_IMAGE_HOSTS rather than being guessed at.
    const detail = `Refused to fetch task ${taskId}'s frame from ${resolved.host} — not in the allowed image hosts. Add it to TRIAGE_IMAGE_HOSTS if it is expected.`;
    console.error("[triage/image] blocked host", { taskId, host: resolved.host });
    return new NextResponse(detail, { status: 502 });
  }

  const upstream = resolved.response;
  if (!upstream.ok || !upstream.body) {
    // Say what the upstream actually said. A bare "Upstream image fetch
    // failed" is what made the first failure here undiagnosable from the
    // browser, and it cost a deploy to learn nothing.
    const body = await upstream.text().catch(() => "");
    const detail = `Upstream ${upstream.status} ${upstream.statusText} for task ${taskId} at ${resolved.url}${body ? ` — ${body.slice(0, 300)}` : ""}`;
    console.error("[triage/image] upstream failed", {
      taskId,
      url: resolved.url,
      status: upstream.status,
    });
    return new NextResponse(detail, { status: 502 });
  }

  return new NextResponse(upstream.body, {
    status: 200,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "image/jpeg",
      // Frames are immutable once written, and a labeler revisits the same one
      // while zooming. Private because the bytes are behind portal auth.
      "cache-control": "private, max-age=3600",
    },
  });
}
