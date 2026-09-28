// New in v2; covers fishsense-lite@77e8f8e5
// apps/fishsense-lite-web/app/api/triage/image/[taskId]/route.ts, which v1 left
// untested. v2 change pinned here: every tenant's projects share one Label
// Studio workspace, and this route fetches with the portal's own Label Studio
// key, so a task id is streamed only when the task is in one of this tenant's
// projects.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { __resetTokenCache } from "@/lib/label-studio";

const { auth, getTenantProjectIds } = vi.hoisted(() => ({
  auth: vi.fn(),
  getTenantProjectIds: vi.fn(),
}));
vi.mock("@/auth", () => ({ auth }));
vi.mock("@/lib/fishsense-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/fishsense-api")>()),
  getTenantProjectIds,
}));

import { GET } from "./route";

const ADMIN = { user: { name: "A" }, role: "admin", isAdmin: true };
const OURS = 9;
const THEIRS = 99;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Label Studio: task 41 is in `project`, its frame on Label Studio itself. */
function labelStudioWithTaskIn(project: number) {
  const fetchMock = vi.fn(async (url: string) => {
    if (url.endsWith("/api/token/refresh")) return json({ access: "jwt" });
    if (url.includes("/api/tasks/41/")) {
      return json({ id: 41, project, data: { image: "/data/upload/1/frame.JPG" } });
    }
    if (url.endsWith("/data/upload/1/frame.JPG")) {
      return new Response("jpegbytes", { headers: { "content-type": "image/jpeg" } });
    }
    throw new Error(`unexpected ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const get = (taskId: string) =>
  GET(new Request(`http://portal.test/api/triage/image/${taskId}`), {
    params: Promise.resolve({ taskId }),
  });

beforeEach(() => {
  vi.stubEnv("LABEL_STUDIO_URL", "http://ls.test");
  vi.stubEnv("LABEL_STUDIO_API_KEY", "pat");
  __resetTokenCache();
  auth.mockResolvedValue(ADMIN);
  getTenantProjectIds.mockImplementation(async (kind: string) => (kind === "laser" ? [OURS] : []));
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("GET /api/triage/image/[taskId]", () => {
  it("streams the frame of a task in one of the tenant's projects", async () => {
    labelStudioWithTaskIn(OURS);

    const response = await get("41");

    expect(response.status).toBe(200);
    expect(await response.text()).toBe("jpegbytes");
  });

  it("refuses another tenant's task, without fetching its frame", async () => {
    const fetchMock = labelStudioWithTaskIn(THEIRS);

    const response = await get("41");

    // As for a task that isn't there: another tenant's ids aren't confirmed.
    expect(response.status).toBe(404);
    expect(fetchMock.mock.calls.some(([u]) => u.includes("/data/upload/"))).toBe(false);
  });

  it("refuses a member who is not an admin before asking anyone", async () => {
    auth.mockResolvedValue({ ...ADMIN, role: "member", isAdmin: false });
    const fetchMock = labelStudioWithTaskIn(OURS);

    expect((await get("41")).status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(getTenantProjectIds).not.toHaveBeenCalled();
  });
});
