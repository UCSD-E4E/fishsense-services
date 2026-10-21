// The unlock is a server action, a public endpoint: it checks the session and
// the owner's confirmation itself; the API decides whether the caller may.
import { beforeEach, describe, expect, it, vi } from "vitest";

const { auth, revalidatePath, unlockTenant } = vi.hoisted(() => ({
  auth: vi.fn(),
  revalidatePath: vi.fn(),
  unlockTenant: vi.fn(),
}));

vi.mock("@/auth", () => ({ auth }));
vi.mock("next/cache", () => ({ revalidatePath }));
vi.mock("@/lib/system-admin", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/system-admin")>()),
  unlockTenant,
}));

import { unlockTenantAction } from "./unlock-action";

const FRESH = { user: { name: "ops" }, accessToken: "t", accessTokenExpiresAt: Math.floor(Date.now() / 1000) + 3600 };

beforeEach(() => {
  vi.clearAllMocks();
  auth.mockResolvedValue(FRESH);
  unlockTenant.mockResolvedValue("2026-10-20T22:00:00Z");
});

describe("unlocking a tenant as a system admin", () => {
  it("asks the API, and refreshes the tenant's pages", async () => {
    expect(await unlockTenantAction("partner", true)).toBe("2026-10-20T22:00:00Z");
    expect(unlockTenant).toHaveBeenCalledWith("t", "partner");
    expect(revalidatePath).toHaveBeenCalledWith("/partner", "layout");
  });

  it("does nothing without the confirmation", async () => {
    await expect(unlockTenantAction("partner", false)).rejects.toThrow("Confirm");
    expect(unlockTenant).not.toHaveBeenCalled();
  });

  it("does nothing on a stale session", async () => {
    auth.mockResolvedValue({ ...FRESH, accessTokenExpiresAt: 1 });
    await expect(unlockTenantAction("partner", true)).rejects.toThrow("Sign in again");
    expect(unlockTenant).not.toHaveBeenCalled();
  });

  it("refuses what is not a tenant", async () => {
    await expect(unlockTenantAction("../api", true)).rejects.toThrow("Not a tenant");
    expect(unlockTenant).not.toHaveBeenCalled();
  });
});
