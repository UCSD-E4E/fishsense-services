// The mission and deployment pages' writes (PLAN.md §4.9, Phase 2b).
//
// Server actions are public endpoints: each re-checks, on every call, that
// the caller is signed in and an admin of the tenant the URL names -- these
// pages span tenants, so the session's own role says nothing -- and checks
// its values before any request. The API checks the role again; a refusal
// comes back in its own words.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const {
  auth,
  unstable_update,
  getMyTenants,
  setMissionPort,
  setMissionPurpose,
  setMissionDeployment,
  createDeployment,
  revalidatePath,
} = vi.hoisted(() => ({
  auth: vi.fn(),
  unstable_update: vi.fn(),
  getMyTenants: vi.fn(),
  setMissionPort: vi.fn(),
  setMissionPurpose: vi.fn(),
  setMissionDeployment: vi.fn(),
  createDeployment: vi.fn(),
  revalidatePath: vi.fn(),
}));

vi.mock("@/auth", () => ({ auth, unstable_update }));
vi.mock("@/lib/fishsense-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/fishsense-api")>()),
  getMyTenants,
}));
vi.mock("next/cache", () => ({ revalidatePath }));
vi.mock("@/lib/mission-facts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/mission-facts")>()),
  setMissionPort,
  setMissionPurpose,
  setMissionDeployment,
  createDeployment,
}));

import {
  createDeploymentAction,
  setDeploymentAction,
  setPortAction,
  setPurposeAction,
} from "./actions";

const FAR_FUTURE = Math.floor(Date.now() / 1000) + 3600;
const SIGNED_IN = {
  user: { name: "Alice", email: "a@e.com" },
  accessToken: "alice-token",
  accessTokenExpiresAt: FAR_FUTURE,
};

beforeEach(() => {
  auth.mockResolvedValue(SIGNED_IN);
  unstable_update.mockResolvedValue(SIGNED_IN);
  getMyTenants.mockResolvedValue([
    { slug: "lab", name: "Lab", role: "admin", isAdmin: true },
    { slug: "partner", name: "Partner", role: "member", isAdmin: false },
  ]);
  setMissionPort.mockResolvedValue({});
  setMissionPurpose.mockResolvedValue({});
  setMissionDeployment.mockResolvedValue(undefined);
  createDeployment.mockResolvedValue({ number: 1 });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("the gate", () => {
  it("refuses a signed-out caller", async () => {
    auth.mockResolvedValue(null);

    const result = await setPortAction("lab", 7, "flat", "");

    expect(result.ok).toBe(false);
    expect(setMissionPort).not.toHaveBeenCalled();
  });

  it("refuses a member who is not that tenant's admin", async () => {
    const result = await setPortAction("partner", 7, "flat", "");

    expect(result).toEqual({ ok: false, error: "Not authorized" });
    expect(setMissionPort).not.toHaveBeenCalled();
  });

  it("refuses a tenant the caller is not in", async () => {
    const result = await setPurposeAction("elsewhere", 7, "known_length");

    expect(result).toEqual({ ok: false, error: "Not authorized" });
    expect(setMissionPurpose).not.toHaveBeenCalled();
  });

  it("refreshes a stale token before spending it", async () => {
    auth.mockResolvedValue({ ...SIGNED_IN, accessTokenExpiresAt: 0 });
    unstable_update.mockResolvedValue({ ...SIGNED_IN, accessToken: "fresh" });

    await setPortAction("lab", 7, "flat", "");

    expect(setMissionPort).toHaveBeenCalledWith("fresh", "lab", 7, "flat", null);
  });
});

describe("values are checked before any request", () => {
  it.each([
    ["a port", () => setPortAction("lab", 7, "pinhole", "")],
    ["a purpose", () => setPurposeAction("lab", 7, "fishing")],
    ["a mission number", () => setPortAction("lab", 1.5, "flat", "")],
    ["a deployment number", () => setDeploymentAction("lab", 7, "3/../x")],
    ["a tenant", () => setPortAction("../admin", 7, "flat", "")],
  ])("%s", async (_, act) => {
    const result = await act();

    expect(result.ok).toBe(false);
    expect(getMyTenants).not.toHaveBeenCalled();
  });
});

describe("the writes", () => {
  it("states a port with the note, and refreshes the mission's page", async () => {
    const result = await setPortAction("lab", 7, "flat", "  pool test ");

    expect(result).toEqual({ ok: true });
    expect(setMissionPort).toHaveBeenCalledWith(
      "alice-token",
      "lab",
      7,
      "flat",
      "pool test",
    );
    expect(revalidatePath).toHaveBeenCalledWith("/missions/lab/7");
  });

  it("brings back the API's refusal in its words", async () => {
    setMissionPort.mockRejectedValue(
      new Error("mission 7's port is corrective_optic by the owner's rule"),
    );

    const result = await setPortAction("lab", 7, "flat", "");

    expect(result).toEqual({
      ok: false,
      error: "mission 7's port is corrective_optic by the owner's rule",
    });
  });

  it("clears a purpose with an empty value", async () => {
    await setPurposeAction("lab", 7, "");

    expect(setMissionPurpose).toHaveBeenCalledWith("alice-token", "lab", 7, null);
  });

  it("moves a mission out of its deployment with an empty value", async () => {
    await setDeploymentAction("lab", 7, "3");
    await setDeploymentAction("lab", 7, "");

    expect(setMissionDeployment.mock.calls).toEqual([
      ["alice-token", "lab", 7, 3],
      ["alice-token", "lab", 7, null],
    ]);
  });

  it("creates a deployment, empty fields as null", async () => {
    const result = await createDeploymentAction("lab", {
      name: " 2026.03.02.FishSense.Curacao ",
      started_on: "2026-03-02",
      ended_on: "",
      notes: "",
    });

    expect(result).toEqual({ ok: true });
    expect(createDeployment).toHaveBeenCalledWith("alice-token", "lab", {
      name: "2026.03.02.FishSense.Curacao",
      started_on: "2026-03-02",
      ended_on: null,
      notes: null,
    });
    expect(revalidatePath).toHaveBeenCalledWith("/deployments");
  });

  it("refuses a deployment without a name or with a malformed date", async () => {
    const unnamed = await createDeploymentAction("lab", {
      name: "  ",
      started_on: "",
      ended_on: "",
      notes: "",
    });
    const undated = await createDeploymentAction("lab", {
      name: "trip",
      started_on: "March",
      ended_on: "",
      notes: "",
    });

    expect(unnamed.ok).toBe(false);
    expect(undated.ok).toBe(false);
    expect(createDeployment).not.toHaveBeenCalled();
  });
});
