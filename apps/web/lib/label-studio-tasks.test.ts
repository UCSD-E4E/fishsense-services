// Ported from fishsense-lite@77e8f8e5 apps/fishsense-lite-web/lib/label-studio-tasks.test.ts.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { __resetTokenCache } from "./label-studio";
import {
  acceptPrediction,
  fetchTaskImage,
  getAnnotation,
  listTasks,
} from "./label-studio-tasks";

/** The tenant's projects, for `fetchTaskImage`'s ownership check. */
const OWNED: ReadonlySet<number> = new Set([9]);

beforeEach(() => {
  vi.stubEnv("LABEL_STUDIO_URL", "http://ls.test");
  vi.stubEnv("LABEL_STUDIO_API_KEY", "pat");
  __resetTokenCache();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Answers the token refresh, then delegates everything else to `handler`. */
function mockFetch(handler: (url: string, init?: RequestInit) => Promise<Response>) {
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/api/token/refresh")) return json({ access: "jwt" });
    return handler(url, init);
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

describe("listTasks", () => {
  it("returns the page's tasks", async () => {
    mockFetch(async () => json({ tasks: [{ id: 1 }, { id: 2 }], total: 2 }));
    const page = await listTasks(9, 1);
    expect(page.tasks.map((t) => t.id)).toEqual([1, 2]);
    expect(page.total).toBe(2);
  });

  // DRF answers a page past the end of a result set with 404
  // `{"detail": "Invalid page."}` rather than an empty list. Treating that as
  // a failure surfaced a fatal error mid-scan in the Android app.
  it("reads an out-of-range page as drained, not as an error", async () => {
    mockFetch(async () => json({ detail: "Invalid page." }, 404));
    const page = await listTasks(9, 7);
    expect(page.tasks).toEqual([]);
    expect(page.drained).toBe(true);
  });

  it("still throws on a real server error", async () => {
    mockFetch(async () => new Response("boom", { status: 500 }));
    await expect(listTasks(9, 1)).rejects.toThrow(/500/);
  });

  it("refreshes the access token once on 401 and retries", async () => {
    let calls = 0;
    const fetchMock = mockFetch(async () => {
      calls += 1;
      return calls === 1 ? json({ detail: "unauthorised" }, 401) : json({ tasks: [{ id: 3 }] });
    });
    const page = await listTasks(9, 1);
    expect(page.tasks.map((t) => t.id)).toEqual([3]);
    // refresh, 401, refresh, success
    expect(fetchMock.mock.calls.filter(([u]) => u.endsWith("/api/token/refresh"))).toHaveLength(2);
  });

  it("accepts a bare array body", async () => {
    // Some Label Studio versions return a list rather than {tasks, total}.
    mockFetch(async () => json([{ id: 4 }]));
    const page = await listTasks(9, 1);
    expect(page.tasks.map((t) => t.id)).toEqual([4]);
  });
});

describe("acceptPrediction", () => {
  it("posts the prediction result verbatim", async () => {
    let sent: unknown = null;
    mockFetch(async (url, init) => {
      if (url.includes("/annotations/")) {
        sent = JSON.parse(String(init?.body));
        return json({ id: 555 });
      }
      throw new Error(`unexpected ${url}`);
    });

    const result = [
      {
        from_name: "laser",
        to_name: "img",
        type: "keypointlabels",
        original_width: 4000,
        original_height: 3000,
        image_rotation: 0,
        value: { x: 57.925, y: 46.966, keypointlabels: ["Red Laser"] },
      },
    ];

    const id = await acceptPrediction(42, result, 3200);
    expect(id).toBe(555);

    const body = sent as { result: unknown; was_cancelled?: boolean; lead_time?: number };
    // Byte-for-byte: this equality IS the safety argument for accepting.
    expect(body.result).toEqual(result);
    expect(body.lead_time).toBe(3200);
    // A cancelled annotation would flip `completed` with no coordinates.
    expect(body.was_cancelled).toBeFalsy();
  });

  it("throws when Label Studio rejects the annotation", async () => {
    mockFetch(async () => new Response("nope", { status: 400 }));
    await expect(acceptPrediction(42, [], 10)).rejects.toThrow(/400/);
  });
});

describe("fetchTaskImage", () => {
  // The previous test asserted that a hand-built
  // `/tasks/{id}/resolve/?fileuri={base64}` URL was constructed correctly. It
  // passed for weeks and the fetch 502'd every time in production, because it
  // verified our assumption rather than Label Studio's behaviour. Ask Label
  // Studio where the frame is; handle each shape it can answer with.

  function taskWith(image: string) {
    return async (url: string) => {
      if (url.includes("/api/tasks/")) {
        expect(url).toContain("resolve_uri=true");
        return json({ id: 42, project: 9, data: { image } });
      }
      return new Response("jpegbytes", {
        status: 200,
        headers: { "content-type": "image/jpeg" },
      });
    };
  }

  it("fetches a presigned URL WITHOUT our Authorization header", async () => {
    vi.stubEnv("TRIAGE_IMAGE_HOSTS", "s3.example");
    const fetchMock = mockFetch(taskWith("https://s3.example/frame.JPG?sig=abc"));
    const out = await fetchTaskImage(42, OWNED);

    expect(out.kind).toBe("response");
    // Compare the parsed host, not a string prefix: "https://s3.example.evil"
    // satisfies startsWith and is a different server. CodeQL flags the prefix
    // form as incomplete URL sanitization, and it is right to.
    const call = fetchMock.mock.calls.find(
      ([u]) => u.startsWith("http") && new URL(u).host === "s3.example",
    );
    expect(call).toBeDefined();
    // Sending a bearer alongside a presigned signature can be rejected
    // outright by S3.
    const headers = (call?.[1]?.headers ?? {}) as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });

  it("fetches a Label Studio path WITH auth, resolved against the base", async () => {
    const fetchMock = mockFetch(taskWith("/data/upload/1/frame.JPG"));
    const out = await fetchTaskImage(42, OWNED);

    expect(out.kind).toBe("response");
    const call = fetchMock.mock.calls.find(([u]) => u.includes("/data/upload/"));
    expect(call?.[0]).toBe("http://ls.test/data/upload/1/frame.JPG");
    const headers = (call?.[1]?.headers ?? {}) as Record<string, string>;
    expect(headers.Authorization).toMatch(/^Bearer /);
  });

  // Label Studio hands the URI straight back when the project has no storage
  // connected. Fetching harder cannot fix that, so it is reported rather than
  // attempted — the case that produced a bare 502 with nothing to read.
  // Server-side request forgery. The task id comes from a portal user and the
  // URL comes from task data, so without an allowlist this route fetches
  // anything the server can reach — including services on the private docker
  // network — and streams the body back.
  it("refuses a host that is not allowed, without fetching it", async () => {
    const fetchMock = mockFetch(taskWith("http://fishsense-api:8000/api/v1/dives/"));
    const out = await fetchTaskImage(42, OWNED);

    expect(out).toMatchObject({ kind: "blocked", host: "fishsense-api:8000" });
    expect(fetchMock.mock.calls.some(([u]) => u.includes("fishsense-api:8000"))).toBe(false);
  });

  it("refuses a cloud metadata address", async () => {
    const fetchMock = mockFetch(taskWith("http://169.254.169.254/latest/meta-data/"));
    const out = await fetchTaskImage(42, OWNED);

    expect(out).toMatchObject({ kind: "blocked" });
    expect(fetchMock.mock.calls.some(([u]) => u.includes("169.254"))).toBe(false);
  });

  it("allows the Label Studio host itself", async () => {
    mockFetch(taskWith("http://ls.test/data/frame.JPG"));
    const out = await fetchTaskImage(42, OWNED);
    expect(out.kind).toBe("response");
  });

  // v2 change. v2-created projects' storage presigns against the object
  // store (PLAN.md §9.11), whose endpoint is already configured for the
  // stack. v1 left it to TRIAGE_IMAGE_HOSTS, which production never set, so
  // presigned frames were blocked. The endpoint's host is allowed by default.
  it("allows the configured object store's host", async () => {
    vi.stubEnv("FISHSENSE_OBJECT_STORE_ENDPOINT_URL", "https://garage.e4e.test:3900");
    mockFetch(taskWith("https://garage.e4e.test:3900/fishsense-lite/tenants/x/f.JPG?X-Amz-Signature=s"));
    const out = await fetchTaskImage(42, OWNED);
    expect(out.kind).toBe("response");
  });

  it("still refuses other hosts when the object store is configured", async () => {
    vi.stubEnv("FISHSENSE_OBJECT_STORE_ENDPOINT_URL", "https://garage.e4e.test:3900");
    mockFetch(taskWith("https://garage.e4e.test.evil.example/f.JPG"));
    const out = await fetchTaskImage(42, OWNED);
    expect(out).toMatchObject({ kind: "blocked", host: "garage.e4e.test.evil.example" });
  });

  it("allows a host named in TRIAGE_IMAGE_HOSTS", async () => {
    vi.stubEnv("TRIAGE_IMAGE_HOSTS", "garage.internal:3900");
    mockFetch(taskWith("https://garage.internal:3900/bucket/frame.JPG?sig=x"));
    const out = await fetchTaskImage(42, OWNED);
    expect(out.kind).toBe("response");
  });

  it("reports an unresolved s3 URI instead of fetching it", async () => {
    mockFetch(taskWith("s3://bucket/preprocess_jpeg/abc.JPG"));
    const out = await fetchTaskImage(42, OWNED);

    expect(out).toEqual({ kind: "unresolved", uri: "s3://bucket/preprocess_jpeg/abc.JPG" });
  });

  it("reports a task carrying no image at all", async () => {
    mockFetch(async (url) =>
      url.includes("/api/tasks/") ? json({ id: 42, project: 9, data: {} }) : json({}),
    );
    const out = await fetchTaskImage(42, OWNED);
    expect(out).toEqual({ kind: "unresolved", uri: "" });
  });

  // v2: every tenant's projects share one Label Studio workspace, so a task id
  // is not the tenant's just because Label Studio has it. Checked on the task
  // this already fetches, before the frame is.
  it("refuses another tenant's task without fetching its frame", async () => {
    const fetchMock = mockFetch(async (url) =>
      url.includes("/api/tasks/")
        ? json({ id: 42, project: 77, data: { image: "http://ls.test/data/frame.JPG" } })
        : new Response("jpegbytes", { status: 200 }),
    );

    expect(await fetchTaskImage(42, OWNED)).toEqual({ kind: "foreign" });
    expect(fetchMock.mock.calls.some(([u]) => u.includes("/data/frame.JPG"))).toBe(false);
  });

  it.each([
    ["a task with no project", { id: 42, data: { image: "http://ls.test/f.JPG" } }],
    ["no such task", null],
  ])("refuses %s", async (_case, task) => {
    mockFetch(async (url) =>
      url.includes("/api/tasks/") && task ? json(task) : json({ detail: "Not found." }, 404),
    );

    expect(await fetchTaskImage(42, OWNED)).toEqual({ kind: "foreign" });
  });
});

describe("getAnnotation", () => {
  it("reads which task an annotation is on", async () => {
    mockFetch(async (url) => {
      expect(url).toBe("http://ls.test/api/annotations/77/");
      return json({ id: 77, task: 41, result: [] });
    });

    expect(await getAnnotation(77)).toEqual({ id: 77, task: 41 });
  });

  it("answers null for an annotation that isn't there", async () => {
    mockFetch(async () => json({ detail: "Not found." }, 404));
    expect(await getAnnotation(77)).toBeNull();
  });

  it("throws on a real failure", async () => {
    mockFetch(async () => new Response("boom", { status: 500 }));
    await expect(getAnnotation(77)).rejects.toThrow(/500/);
  });
});

describe("rate limiting", () => {
  // Hosted Label Studio 429s a burst. Surfacing it aborts the whole queue load
  // over a condition that clears by itself — which is what took the triage page
  // down on its first real run.
  it("retries a 429 and returns the eventual success", async () => {
    let calls = 0;
    mockFetch(async () => {
      calls += 1;
      return calls === 1
        ? new Response("slow down", { status: 429, headers: { "retry-after": "0" } })
        : json({ tasks: [{ id: 7 }] });
    });

    const page = await listTasks(9, 1);
    expect(page.tasks.map((t) => t.id)).toEqual([7]);
    expect(calls).toBe(2);
  });

  it("gives up after a bounded number of retries rather than hanging", async () => {
    let calls = 0;
    mockFetch(async () => {
      calls += 1;
      return new Response("slow down", { status: 429, headers: { "retry-after": "0" } });
    });

    await expect(listTasks(9, 1)).rejects.toThrow(/429/);
    // First attempt plus a bounded number of retries — never unbounded.
    expect(calls).toBeGreaterThan(1);
    expect(calls).toBeLessThanOrEqual(5);
  });
});
