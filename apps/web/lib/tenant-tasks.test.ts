// New in v2. v1 served one tenant from its own Label Studio projects; v2's
// tenants share one Label Studio workspace, so a task or
// annotation id a caller hands the portal is checked against the tenant's
// own projects before anything is written or streamed (docs/port-plan.md,
// "Every tenant shares one Label Studio workspace").
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { getTenantProjectIds, getTask, getAnnotation } = vi.hoisted(() => ({
  getTenantProjectIds: vi.fn(),
  getTask: vi.fn(),
  getAnnotation: vi.fn(),
}));

vi.mock("./fishsense-api", () => ({ getTenantProjectIds }));
vi.mock("./label-studio-tasks", () => ({ getTask, getAnnotation }));

import {
  NOT_THIS_TENANTS,
  requireTenantAnnotation,
  requireTenantTask,
  tenantProjectIds,
} from "./tenant-tasks";

const BY_KIND: Record<string, number[]> = {
  laser: [10],
  species: [20],
  headtail: [30],
  "dive-slate": [40],
};

beforeEach(() => {
  getTenantProjectIds.mockImplementation(async (kind: string) => BY_KIND[kind]);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("tenantProjectIds", () => {
  it("is every project of every kind the tenant has", async () => {
    expect([...(await tenantProjectIds())].sort()).toEqual([10, 20, 30, 40]);
    expect(getTenantProjectIds.mock.calls.map(([kind]) => kind).sort()).toEqual([
      "dive-slate",
      "headtail",
      "laser",
      "species",
    ]);
  });

  it("fails rather than answering with fewer projects", async () => {
    getTenantProjectIds.mockRejectedValueOnce(new Error("fishsense-api down"));
    await expect(tenantProjectIds()).rejects.toThrow("fishsense-api down");
  });
});

describe("requireTenantTask", () => {
  it.each([10, 20, 30, 40])("admits a task in the tenant's project %i", async (project) => {
    getTask.mockResolvedValue({ id: 41, project });
    await expect(requireTenantTask(41)).resolves.toBeUndefined();
    expect(getTask).toHaveBeenCalledExactlyOnceWith(41);
  });

  it.each([
    ["another tenant's project", { id: 41, project: 99 }],
    ["a task Label Studio doesn't say the project of", { id: 41 }],
    ["no such task", null],
  ])("refuses %s", async (_case, task) => {
    getTask.mockResolvedValue(task);
    await expect(requireTenantTask(41)).rejects.toThrow(NOT_THIS_TENANTS);
  });
});

describe("requireTenantAnnotation", () => {
  it("admits an annotation on one of the tenant's tasks", async () => {
    getAnnotation.mockResolvedValue({ id: 77, task: 41 });
    getTask.mockResolvedValue({ id: 41, project: 10 });

    await expect(requireTenantAnnotation(77)).resolves.toBeUndefined();
    expect(getTask).toHaveBeenCalledExactlyOnceWith(41);
  });

  it("refuses an annotation on another tenant's task", async () => {
    getAnnotation.mockResolvedValue({ id: 77, task: 41 });
    getTask.mockResolvedValue({ id: 41, project: 99 });

    await expect(requireTenantAnnotation(77)).rejects.toThrow(NOT_THIS_TENANTS);
  });

  it("refuses an annotation that isn't there", async () => {
    getAnnotation.mockResolvedValue(null);

    await expect(requireTenantAnnotation(77)).rejects.toThrow(NOT_THIS_TENANTS);
    expect(getTask).not.toHaveBeenCalled();
  });
});
